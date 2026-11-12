"""Headroom engine, the `headroom` op and `loadctl wait`, against fixture host trees."""

import re
import time
from pathlib import Path

import pytest
from conftest import parity_config

from loadgov.cli import loadctl
from loadgov.config import Config, ConfigError, HeadroomConfig, parse_config, thresholds
from loadgov.daemon.main import Daemon
from loadgov.protocol import VERSION
from loadgov.sample import Sampler

GB = 1024


T0 = 1_762_800_000.0


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def make_daemon(host, tmp_path, config=None):
    box = []
    d = Daemon(
        host.fs,
        clock=lambda: box[0].t,
        state_dir=str(tmp_path / "state"),
        log_dir=str(tmp_path / "log"),
        env_file=None,
        config=config or parity_config(),
    )
    box.append(d)
    d.t = T0
    set_sample(d, host)
    return d


def set_sample(d, host):
    s = Sampler(host.fs)
    s.prime(host.prev_stat)
    d.status = s.sample(now=d.t)


def hr(d, kind="impl", project=None):
    args = {"kind": kind, **({"project": project} if project else {})}
    return d.dispatch({"v": VERSION, "op": "headroom", "args": args})


# ---- the checks ---------------------------------------------------------------------------------


def test_calm_passes_busy_fails(fake_host, tmp_path):
    assert hr(make_daemon(fake_host("calm"), tmp_path))["decision"]["pass"] is True
    r = hr(make_daemon(fake_host("busy"), tmp_path))["decision"]
    assert r["pass"] is False
    assert any(x.startswith("load1 5.40 >= 4.00") for x in r["reasons"])
    assert any(x.startswith("cpu psi") for x in r["reasons"])


def test_swap_rule_both_branches(fake_host, tmp_path):
    # 2000 MB swap used; mem_min 4 GB -> needs 8 GB available to ignore the swap
    low = hr(make_daemon(fake_host("calm", avail_mb=7 * GB, swap_used_mb=2000), tmp_path))["decision"]
    assert low["pass"] is False and low["reasons"] == ["swap used 2000 MB > 1024 MB"]
    high = hr(make_daemon(fake_host("calm", avail_mb=8 * GB, swap_used_mb=2000), tmp_path))["decision"]
    assert high["pass"] is True
    ok = hr(make_daemon(fake_host("calm", avail_mb=5 * GB, swap_used_mb=1024), tmp_path))["decision"]
    assert ok["pass"] is True  # swap <= max is allowed (not strict)


def test_mem_and_mem_psi_and_strict_boundaries(fake_host, tmp_path):
    d = hr(make_daemon(fake_host("calm", avail_mb=4 * GB - 1), tmp_path))["decision"]
    assert [r.split()[0:2] for r in d["reasons"]] == [["mem", "available"]]
    assert hr(make_daemon(fake_host("calm", avail_mb=4 * GB), tmp_path))["decision"]["pass"] is True
    assert hr(make_daemon(fake_host("calm", mem_psi=(0.0, 5.0, 0.0)), tmp_path))["decision"]["pass"] is False
    assert hr(make_daemon(fake_host("calm", load1=4.0), tmp_path))["decision"]["pass"] is False
    # load5 < nproc-1.5 = 4.5 is checked for the default project
    assert hr(make_daemon(fake_host("calm", load5=4.5), tmp_path))["decision"]["pass"] is False


def test_proj_a_impl_thresholds_differ_from_proj_b(fake_host, tmp_path):
    host = fake_host("calm", load1=5.5, load5=5.9, cpu_psi=(0.0, 45.0), avail_mb=int(6.5 * GB))
    d = make_daemon(host, tmp_path)
    assert hr(d, "impl", "proj-a")["decision"]["pass"] is True
    r = hr(d, "impl", "proj-b")["decision"]
    assert r["pass"] is False and len(r["reasons"]) >= 3
    # proj-a review: load1 < nproc (6), mem 6; gate: mem 4
    assert hr(d, "review", "proj-a")["decision"]["pass"] is True
    assert hr(d, "gate", "proj-a")["decision"]["pass"] is True


def test_io_psi_gate_off_reports_would_block(fake_host, tmp_path):
    host = fake_host("calm", io_psi=(0.0, 40.0, 0.0))
    r = hr(make_daemon(host, tmp_path))["decision"]
    assert r["pass"] is True and r["would_block_io"] is True
    cfg = Config(headroom=HeadroomConfig(io_psi_gate=True))
    r = hr(make_daemon(host, tmp_path, cfg))["decision"]
    assert r["pass"] is False and r["reasons"] == ["io psi 40.0% >= 25%"]
    assert hr(make_daemon(fake_host("calm"), tmp_path))["decision"]["would_block_io"] is False


# ---- profiles -----------------------------------------------------------------------------------


def test_silent_profile_scales_load_threshold(fake_host, tmp_path):
    d = make_daemon(fake_host("calm", load1=3.0, load5=0.5), tmp_path)
    assert hr(d, "impl", "proj-b")["decision"]["pass"] is True  # 3.0 < 4
    d.limits.set("owner", {}, "quiet", None, None, "silent", None)
    r = hr(d, "impl", "proj-b")
    assert r["thresholds"]["load1_max"] == pytest.approx(4 * 2.0 / 3.0)
    assert r["factor"]["load"] == pytest.approx(0.6667, abs=1e-4)
    assert r["decision"]["pass"] is False  # 3.0 >= 2.67


def test_boost_scales_up_and_ram_floor_raises_mem_min(fake_host, tmp_path):
    d = make_daemon(fake_host("calm", load1=5.0), tmp_path)
    d.limits.set("owner", {"ram_floor_gb": 5.0}, "mem", None, None, None, None)
    r = hr(d)
    assert r["thresholds"]["mem_min_gb"] == 6.0 and r["factor"]["mem_add_gb"] == 2.0
    assert r["thresholds"]["psi_max"] == 20  # psi unchanged by profiles
    d.limits.set("owner", {}, "fast", None, None, "boost", None)
    assert hr(d)["thresholds"]["load1_max"] == pytest.approx(4 * 5.0 / 3.0)


# ---- paused -------------------------------------------------------------------------------------


def test_paused_file_hold_and_freeze(fake_host, tmp_path):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    paused = Path(host.lock) / "proj-b-guard.paused"
    paused.write_text("")
    assert hr(d)["decision"]["pass"] is True  # empty file does not pause
    paused.write_text("paused by guard\n")
    r = hr(d, "impl", "proj-a")["decision"]  # host-wide: any project
    assert r["pass"] is False and r["paused"] and r["reasons"][0].startswith("paused: host guard")
    paused.unlink()
    d.limits.set_flag("hold", "maintenance")
    assert hr(d, "impl")["decision"]["pass"] is True
    assert hr(d, "review")["decision"]["pass"] is True
    for kind in ("gate", "compile"):
        r = hr(d, kind)["decision"]
        assert r["pass"] is False and r["paused"] == ["owner hold: maintenance"]
    d.limits.set_flag("freeze", "incident")
    for kind in ("impl", "review", "gate", "compile"):
        assert hr(d, kind)["decision"]["pass"] is False
    d.limits.set_flag("freeze", None)
    d.limits.set_flag("hold", None)
    assert hr(d, "gate")["decision"]["pass"] is True


# ---- op validation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [{}, {"kind": "nope"}, {"kind": "impl", "project": 3}, {"kind": ["impl"]}, [1]]
)
def test_headroom_bad_args(fake_host, tmp_path, args):
    d = make_daemon(fake_host("calm"), tmp_path)
    r = d.dispatch({"v": VERSION, "op": "headroom", "args": args})
    assert r["ok"] is False and r["error"] == "bad_request"


def test_headroom_not_ready(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.status = None
    assert hr(d)["error"] == "not_ready"


def test_stale_sample_is_not_ready(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.t += 15  # exactly 3 x tick_s: still fine
    assert hr(d)["ok"] is True
    d.t += 1
    r = hr(d)
    assert r["ok"] is False and r["error"] == "not_ready" and r["message"].startswith("stale sample")


# ---- config -------------------------------------------------------------------------------------


def test_thresholds_defaults_and_nproc_expressions():
    h = parity_config().headroom
    t = thresholds(h, "impl", None, 8)
    assert (t.load1_max, t.load5_max, t.psi_max, t.mem_min_gb) == (6, 6.5, 20, 4)
    assert (t.confirm_s, t.poll_s, t.max_wait_s) == (60, 30, 7200)
    b = thresholds(h, "impl", "proj-a", 8)
    assert (b.confirm_s, b.poll_s, b.max_wait_s) == (60, 120, 0)
    assert (b.load1_max, b.load5_max, b.psi_max, b.mem_min_gb) == (6, None, 50, 6)
    assert thresholds(h, "review", "proj-a", 8).load1_max == 8
    assert thresholds(h, "compile", "proj-a", 8) == thresholds(h, "compile", None, 8)


def test_config_overrides_and_validation():
    cfg = parse_config(
        """
[headroom]
io_psi_gate = true
[headroom.kinds.gate]
confirm_s = 30
load1_max = "nproc*0.5"
[projects.proj-a.kinds.gate]
mem_min_gb = 5
psi_max = 50
load1_max = "nproc"
[projects.other.kinds.impl]
psi_max = 35
"""
    )
    assert cfg.headroom.io_psi_gate is True
    g = thresholds(cfg.headroom, "gate", "proj-a", 8)
    assert (g.confirm_s, g.load1_max, g.mem_min_gb, g.psi_max) == (30, 8, 5, 50)  # project beats kind
    assert thresholds(cfg.headroom, "gate", None, 8).load1_max == 4
    assert thresholds(cfg.headroom, "impl", "other", 8).psi_max == 35
    for bad in (
        "[headroom]\nx = 1",
        "[headroom.kinds.nope]\npsi_max = 1",
        '[headroom.kinds.impl]\nload1_max = "cores-2"',
        '[headroom.kinds.impl]\npsi_max = "nproc"',
        "[projects.p.kinds.impl]\npsi_max = -1",
        "[projects.p.kinds.impl]\nbogus = 1",
        '[headroom]\nio_psi_gate = "yes"',
    ):
        with pytest.raises(ConfigError):
            parse_config(bad)


EXAMPLE = Path(__file__).resolve().parents[2] / "packaging/config/loadgov.toml.example"


def test_example_config_parses():
    assert parse_config(EXAMPLE.read_text()).headroom.io_psi_gate is False


def test_example_config_with_every_commented_block_enabled_loads():
    toml_line = re.compile(r"^# (\[|\w+ = )")
    text = "\n".join(line[2:] if toml_line.match(line) else line for line in EXAMPLE.read_text().splitlines())
    cfg = parse_config(text)
    t = thresholds(cfg.headroom, "impl", None, 6)  # the documented nproc expressions resolve on a real host
    assert (t.load1_max, t.load5_max) == (4, 4.5)
    assert thresholds(cfg.headroom, "impl", "example-project", 6).load1_max == 6


def test_nproc_expression_validated_by_shape_not_by_value():
    cfg = parse_config('[headroom.kinds.impl]\nload1_max = "nproc-2"\nload5_max = "nproc-1.5"\n')
    assert thresholds(cfg.headroom, "impl", None, 2).load1_max == 0  # resolves <= 0 on 2 cores
    assert thresholds(cfg.headroom, "impl", None, 8).load5_max == 6.5


def test_nonpositive_load_limit_fails_the_decision_with_a_reason(fake_host, tmp_path):
    cfg = parse_config('[headroom.kinds.impl]\nload1_max = "nproc-6"\n')  # 6 CPUs in the fixture -> 0
    r = hr(make_daemon(fake_host("calm"), tmp_path, cfg))["decision"]
    assert r["pass"] is False and r["reasons"][0] == "misconfigured: load1_max resolves to 0 on 6 CPUs"


# ---- loadctl wait -------------------------------------------------------------------------------


def wire(monkeypatch, d):
    """A fake daemon whose clock and sample time advance 1 s per request (a new sample every query)."""

    def req(_sock, op, args):
        d.t += 1
        d.status["at"] = stamp(d.t)
        d.status["at_ms"] = int(d.t * 1000)
        return d.dispatch({"v": VERSION, "op": op, "args": args})

    monkeypatch.setattr(loadctl, "request", req)


class Sleeper:
    def __init__(self, after=None):
        self.calls, self.after = [], after

    def __call__(self, s):
        self.calls.append(s)
        if self.after:
            self.after(len(self.calls))


def test_wait_calm_returns_ok_after_two_samples(fake_host, tmp_path, monkeypatch, capsys):
    wire(monkeypatch, make_daemon(fake_host("calm"), tmp_path))
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "impl", "--label", "dispatch-1"], sleep=sl) == 0
    out = capsys.readouterr().out
    assert out.startswith("HEADROOM_OK dispatch-1 load=0.5/0.5 (max 4.00/4.50, 6 CPUs)")
    assert out.rstrip().endswith("waited=60s")
    assert sl.calls == [60]


def test_wait_busy_prints_wait_then_times_out(fake_host, tmp_path, monkeypatch, capsys):
    wire(monkeypatch, make_daemon(fake_host("busy"), tmp_path))
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "1"], sleep=sl) == 1
    c = capsys.readouterr()
    assert c.out == ""
    assert c.err.startswith("HEADROOM_WAIT job load=5.4/4.8")
    assert "HEADROOM_TIMEOUT job load=5.4/4.8" in c.err and c.err.rstrip().endswith("after 1s: not started")
    assert sl.calls == [1]  # the poll is cut to the remaining budget


def test_wait_confirm_sleep_is_never_shortened_by_the_budget(fake_host, tmp_path, monkeypatch, capsys):
    wire(monkeypatch, make_daemon(fake_host("calm"), tmp_path))
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "1"], sleep=sl) == 0
    assert sl.calls == [60]  # remaining budget 1 s < confirm_s: the full gap is still waited
    assert capsys.readouterr().out.rstrip().endswith("waited=60s")


def test_wait_pass_exactly_at_the_deadline_still_confirms(fake_host, tmp_path, monkeypatch, capsys):
    wire(monkeypatch, make_daemon(fake_host("calm"), tmp_path))
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "60", "--confirm", "60"], sleep=sl) == 0
    assert sl.calls == [60]


def test_wait_wait_lines_at_most_every_300s(fake_host, tmp_path, monkeypatch, capsys):
    wire(monkeypatch, make_daemon(fake_host("busy"), tmp_path))
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "600"], sleep=Sleeper()) == 1
    err = capsys.readouterr().err
    assert err.count("HEADROOM_WAIT") == 2  # at 0 and 300; 600 times out first
    assert "waited=0s" in err and "waited=300s" in err and "HEADROOM_TIMEOUT" in err


def test_wait_pass_then_fail_on_confirm_is_not_ok(fake_host, tmp_path, monkeypatch, capsys):
    calm, busy = fake_host("calm"), fake_host("busy")
    d = make_daemon(calm, tmp_path)
    wire(monkeypatch, d)
    sl = Sleeper(after=lambda n: set_sample(d, busy) if n == 1 else None)
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "1"], sleep=sl) == 1
    c = capsys.readouterr()
    assert "HEADROOM_OK" not in c.out + c.err
    assert "HEADROOM_TIMEOUT" in c.err and "after 60s" in c.err


def test_wait_succeeds_when_host_calms_down(fake_host, tmp_path, monkeypatch, capsys):
    calm = fake_host("calm")
    d = make_daemon(fake_host("busy"), tmp_path)
    wire(monkeypatch, d)
    sl = Sleeper(after=lambda n: set_sample(d, calm) if n == 2 else None)
    assert loadctl.main(["wait", "--kind", "impl", "--confirm", "5", "--poll", "10"], sleep=sl) == 0
    assert sl.calls == [10, 10, 5]  # poll, poll (calm now), confirm
    assert capsys.readouterr().out.rstrip().endswith("waited=25s")


def test_wait_same_sample_twice_is_never_ok(monkeypatch, fake_host, tmp_path, capsys):
    d = make_daemon(fake_host("calm"), tmp_path)
    monkeypatch.setattr(
        loadctl, "request", lambda _s, op, args: d.dispatch({"v": VERSION, "op": op, "args": args})
    )  # the daemon never takes a new sample
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "100"], sleep=Sleeper()) == 1
    c = capsys.readouterr()
    assert "HEADROOM_OK" not in c.out and "HEADROOM_TIMEOUT" in c.err


@pytest.mark.parametrize("flag", ["--timeout", "--confirm", "--poll"])
def test_wait_negative_durations_are_usage_errors(flag, capsys):
    assert loadctl.main(["wait", "--kind", "impl", flag, "-1"], sleep=Sleeper()) == 2
    assert "must be >= 0" in capsys.readouterr().err


def test_wait_proj_a_waits_forever_proj_b_gives_up_at_7200(fake_host, tmp_path, monkeypatch, capsys):
    busy, calm = fake_host("calm", load1=7.0), fake_host("calm")
    d = make_daemon(busy, tmp_path)
    wire(monkeypatch, d)
    assert loadctl.main(["wait", "--kind", "impl", "--project", "proj-b"], sleep=Sleeper()) == 1
    assert "after 7200s: not started" in capsys.readouterr().err
    slept = []

    def calm_after_8000s(n):
        slept.append(n)
        if sum(slept) > 8000:
            set_sample(d, calm)

    sl = Sleeper(after=lambda n: calm_after_8000s(sl.calls[-1]))
    assert loadctl.main(["wait", "--kind", "impl", "--project", "proj-a"], sleep=sl) == 0
    assert set(sl.calls) == {120, 60} and sum(sl.calls) > 8000
    assert "HEADROOM_OK job" in capsys.readouterr().out


def test_wait_timeout_zero_waits_forever(fake_host, tmp_path, monkeypatch, capsys):
    d = make_daemon(fake_host("busy"), tmp_path)
    wire(monkeypatch, d)
    sl = Sleeper(after=lambda n: set_sample(d, fake_host("calm")) if n == 500 else None)
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "0"], sleep=sl) == 0
    assert len(sl.calls) == 501


def test_wait_daemon_unreachable_exits_3(tmp_path, capsys):
    rc = loadctl.main(["--socket", str(tmp_path / "nope.sock"), "wait", "--kind", "gate"], sleep=Sleeper())
    assert rc == 3 and "not reachable" in capsys.readouterr().err


def test_wait_daemon_error_exits_5(monkeypatch, capsys):
    monkeypatch.setattr(loadctl, "request", lambda *_a: {"ok": False, "error": "internal", "message": "x"})
    assert loadctl.main(["wait", "--kind", "gate"], sleep=Sleeper()) == 5


def test_wait_rejects_unknown_kind():
    with pytest.raises(SystemExit) as e:
        loadctl.main(["wait", "--kind", "nope"])
    assert e.value.code == 2


def test_wait_retries_not_ready_inside_the_budget(fake_host, tmp_path, monkeypatch, capsys):
    d = make_daemon(fake_host("calm"), tmp_path)
    wire(monkeypatch, d)
    real, left = loadctl.request, [3]

    def flaky(sock, op, args):
        if left[0] > 0:
            left[0] -= 1
            return {"ok": False, "error": "not_ready", "message": "no sample yet"}
        return real(sock, op, args)

    monkeypatch.setattr(loadctl, "request", flaky)
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "100"], sleep=sl) == 0
    assert sl.calls == [5, 5, 5, 60]  # three startup retries, then the confirm gap
    assert "HEADROOM_OK job" in capsys.readouterr().out


def test_wait_not_ready_forever_times_out_with_exit_1_not_5(monkeypatch, capsys):
    monkeypatch.setattr(loadctl, "request", lambda *_a: {"ok": False, "error": "not_ready", "message": "x"})
    sl = Sleeper()
    assert loadctl.main(["wait", "--kind", "gate", "--timeout", "12"], sleep=sl) == 1
    assert sl.calls == [5, 5, 2]  # the last nap is cut to the budget
    assert "HEADROOM_TIMEOUT job daemon not ready after 12s" in capsys.readouterr().err


def test_wait_goes_not_ready_mid_wait_and_recovers(fake_host, tmp_path, monkeypatch, capsys):
    d = make_daemon(fake_host("busy"), tmp_path)
    wire(monkeypatch, d)
    real, state = loadctl.request, {"n": 0}

    def flaky(sock, op, args):
        state["n"] += 1
        if state["n"] == 2:  # the daemon restarts between two polls
            return {"ok": False, "error": "not_ready", "message": "no sample yet"}
        return real(sock, op, args)

    monkeypatch.setattr(loadctl, "request", flaky)
    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "30", "--poll", "10"], sleep=Sleeper()) == 1
    assert "HEADROOM_TIMEOUT job load=" in capsys.readouterr().err


def test_wait_compares_samples_at_sub_second_resolution(fake_host, tmp_path, monkeypatch, capsys):
    d = make_daemon(fake_host("calm"), tmp_path)
    box = {"ms": 1_000}

    def req(_s, op, args):
        box["ms"] += 400  # two samples inside one second: the same `sample_at`
        d.status["at"] = "2025-11-10T20:00:00Z"
        d.status["at_ms"] = box["ms"]
        return d.dispatch({"v": VERSION, "op": op, "args": args})

    monkeypatch.setattr(loadctl, "request", req)
    assert loadctl.main(["wait", "--kind", "impl"], sleep=Sleeper()) == 0
    assert "HEADROOM_OK" in capsys.readouterr().out


def test_wait_failed_confirm_rechecks_before_sleeping_a_poll(fake_host, tmp_path, monkeypatch):
    calm, busy = fake_host("calm"), fake_host("busy")
    d = make_daemon(calm, tmp_path)
    wire(monkeypatch, d)
    real, seen = loadctl.request, []

    def spy(sock, op, args):
        r = real(sock, op, args)
        seen.append(("q", r["decision"]["pass"]))
        return r

    monkeypatch.setattr(loadctl, "request", spy)
    sl = Sleeper(after=lambda n: set_sample(d, busy) if n == 1 else None)
    orig = sl.__call__

    def tracking(s):
        seen.append(("sleep", s))
        orig(s)

    assert loadctl.main(["wait", "--kind", "impl", "--timeout", "100", "--poll", "7"], sleep=tracking) == 1
    # first query passes, confirm sleep 60, confirm query fails, an immediate re-check, then the poll sleep
    assert seen[:5] == [("q", True), ("sleep", 60), ("q", False), ("q", False), ("sleep", 7)]


def test_io_would_block_is_counted_per_decision_and_published(fake_host, tmp_path):
    d = make_daemon(fake_host("calm", io_psi=(0.0, 40.0, 0.0)), tmp_path)
    assert d.dispatch({"v": VERSION, "op": "status"})["status"]["headroom"] == {
        "io_would_block_count": 0,
        "io_would_block_last": None,
    }
    d.t += 5
    assert hr(d)["decision"]["would_block_io"] is True and hr(d)["decision"]["pass"] is True  # gate is off
    st = d.dispatch({"v": VERSION, "op": "status"})["status"]["headroom"]
    assert st == {"io_would_block_count": 2, "io_would_block_last": stamp(d.t)}
    set_sample(d, fake_host("calm"))
    hr(d)
    assert d.dispatch({"v": VERSION, "op": "status"})["status"]["headroom"]["io_would_block_count"] == 2


def test_sample_key_compares_replies_of_different_daemon_versions():
    old = {"sample_at": "2025-11-10T20:00:00Z"}  # no sample_at_ms
    new = {"sample_at": "2025-11-10T20:00:01Z", "sample_at_ms": 1_762_804_801_400}
    assert isinstance(loadctl.sample_key(old), int) and isinstance(loadctl.sample_key(new), int)
    assert loadctl.sample_key(new) > loadctl.sample_key(old)
    assert loadctl.sample_key(
        {"sample_at": "2025-11-10T20:00:00Z", "sample_at_ms": None}
    ) == loadctl.sample_key(old)
