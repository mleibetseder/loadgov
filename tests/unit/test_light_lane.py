"""The light lane: small checks under hard scope limits; admission, limits, overrun, alert, status."""

import dataclasses
import json
import os
import subprocess

import pytest
from conftest import parity_config
from test_headroom import GB, hr, make_daemon
from test_run import Host
from test_run import make_daemon as run_daemon
from test_run import wire as run_wire

from loadgov.cli import loadctl, run
from loadgov.config import (
    BUILTIN_PROFILES,
    KINDS,
    ConfigError,
    HeadroomConfig,
    LightConfig,
    parse_config,
    thresholds,
)
from loadgov.daemon.main import Daemon
from loadgov.engines.jobs import Job
from loadgov.engines.watch import Watch
from loadgov.protocol import VERSION, validate_status

BUSY = {"load1": 9.0, "load5": 9.0, "cpu_psi": (0.0, 80.0), "mem_psi": (0.0, 30.0, 0.0)}


@pytest.fixture
def procs():
    started = []

    def spawn():
        p = subprocess.Popen(["sleep", "60"])
        started.append(p)
        return p

    yield spawn
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


def add_jobs(d, n, project="web-app", kind="light"):
    for i in range(n):
        unit = f"loadgov-{kind}-{project}-{len(d.jobs.jobs)}-{i}.scope"
        d.jobs.jobs[unit] = Job(
            kind, project, "l", unit, f"/x/{unit}", 1000 + i, 1, 1000 + i, None, d.clock()
        )


# ---- config, profiles, limits ---------------------------------------------------------------------


def test_kind_profile_values_and_range():
    assert "light" in KINDS
    assert {n: p.max_light for n, p in BUILTIN_PROFILES.items()} == {
        "silent": 2, "balanced": 4, "boost": 6, "drain": 0,
    }  # fmt: skip
    assert parse_config("[profiles.balanced]\nmax_light = 8\n").profiles["balanced"].max_light == 8
    for bad in ("9", "-1", "2.5", "true"):
        with pytest.raises(ConfigError, match="max_light"):
            parse_config(f"[profiles.balanced]\nmax_light = {bad}\n")


def test_light_default_floor_is_2gb():
    h = HeadroomConfig()
    assert thresholds(h, "light", None, 6).mem_min_gb == 2


def test_light_section_defaults_and_validation():
    c = parse_config("").light
    assert c == LightConfig(100, 1024, 600, 2)
    ok = parse_config("[light]\ncpu_quota_pct = 50\nmemory_max_mb = 512\nmax_runtime_s = 60\n")
    assert (ok.light.cpu_quota_pct, ok.light.memory_max_mb, ok.light.max_runtime_s) == (50, 512, 60)
    for key, bad in (
        ("cpu_quota_pct", 9), ("cpu_quota_pct", 201), ("memory_max_mb", 127), ("memory_max_mb", 4097),
        ("max_runtime_s", 29), ("max_runtime_s", 1801), ("max_light_per_project", 0),
        ("max_light_per_project", 9), ("max_light_per_project", "2"), ("max_light_per_project", True),
    ):  # fmt: skip
        with pytest.raises(ConfigError, match=f"light.{key}"):
            parse_config(f"[light]\n{key} = {json.dumps(bad)}\n")
    with pytest.raises(ConfigError, match="light:"):
        parse_config("[light]\nfoo = 1\n")


def test_agent_raise_refused_under_a_human_restriction_lower_is_free(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    assert d.limits.set("agent", {"max_light": 2}, "less")["accepted"]
    d.limits.reset("r", all_=True)
    d.limits.ceiling_set("silent", "owner at home")
    r = d.limits.set("agent", {"max_light": 3}, "more")
    assert r["refused"] is True and r["restrictedBy"] == "owner"
    assert d.limits.effective()["max_light"] == 2


def test_set_op_carries_max_light_and_status_line(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    r = d.dispatch({"v": VERSION, "op": "set", "args": {"max_light": 3, "reason": "t"}})
    assert r["ok"] and r["effective"]["max_light"] == 3
    assert d.dispatch({"v": VERSION, "op": "set", "args": {"max_light": 9, "reason": "t"}})["ok"] is False
    assert "light 3" in loadctl._lim(d.limits.effective())


# ---- admission ------------------------------------------------------------------------------------


def test_light_passes_on_a_busy_host_without_a_slot_and_names_the_lane(fake_host, tmp_path):
    d = make_daemon(fake_host("busy", **BUSY), tmp_path)
    assert hr(d, "impl")["decision"]["pass"] is False
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is True and r["reasons"] == []
    assert r["reason"].startswith("light lane: 0 of 4 running (web-app 0 of 2), ")


def test_light_memory_floor(fake_host, tmp_path):
    low = make_daemon(fake_host("calm", avail_mb=3 * GB - 1), tmp_path)  # floor = ram_floor_gb 3
    r = hr(low, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"][0].startswith("mem available 3.0 GB < 3 GB")
    assert hr(make_daemon(fake_host("calm", avail_mb=3 * GB), tmp_path), "light", "web-app")["decision"][
        "pass"
    ]


def test_light_global_cap_counts_only_light_jobs(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    for p in ("a", "b", "c", "d"):
        add_jobs(d, 1, project=p)
    add_jobs(d, 5, kind="review")
    r = hr(d, "light", "e")["decision"]
    assert r["pass"] is False and r["reasons"] == ["light lane: 4 of 4 running"]
    del d.jobs.jobs[next(u for u, j in d.jobs.jobs.items() if j.kind == "light")]
    ok = hr(d, "light", "e")["decision"]
    assert ok["pass"] is True and ok["reason"].startswith("light lane: 3 of 4 running (e 0 of 2), ")


def test_light_per_project_cap(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 2, project="web-app")
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"] == ["light lane: web-app 2 of 2 running"]
    assert hr(d, "light", "other")["decision"]["pass"] is True
    d.config = dataclasses.replace(d.config, light=LightConfig(max_light_per_project=3))
    assert hr(d, "light", "web-app")["decision"]["pass"] is True


def test_pause_marker_freeze_and_memory_guard_block_light_but_hold_does_not(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.limits.set_flag("hold", "gate running")
    assert hr(d, "light", "web-app")["decision"]["pass"] is True
    d.limits.set_flag("freeze", "owner away")
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"] == ["paused: owner freeze: owner away"]
    d.limits.set_flag("freeze", None)
    d.memguard.engine.paused = [object()]
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"] == ["memory guard is pausing work"]


def test_pause_marker_file_blocks_light(fake_host, tmp_path):
    cfg = parse_config("[memguard]\npause_marker = { file = 'guard.paused', project = 'web-app' }\n")
    d = make_daemon(
        fake_host("calm"), tmp_path, config=dataclasses.replace(parity_config(), memguard=cfg.memguard)
    )
    os.makedirs(d.fs.lock_root, exist_ok=True)
    with open(os.path.join(d.fs.lock_root, "guard.paused"), "w") as f:
        f.write("paused\n")
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"][0].startswith("paused: host guard pause file")


def test_drain_blocks_light_and_silent_allows_two(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.limits.set("owner", {}, "t", profile="drain")
    r = hr(d, "light", "web-app")["decision"]
    assert r["pass"] is False and r["reasons"] == ["light lane: 0 of 0 running"]
    d.limits.set("owner", {}, "t", profile="silent")
    add_jobs(d, 1, project="a")
    assert hr(d, "light", "b")["decision"]["pass"] is True
    add_jobs(d, 1, project="b")
    assert hr(d, "light", "c")["decision"]["pass"] is False


def test_other_kinds_are_unchanged(fake_host, tmp_path):
    d = make_daemon(fake_host("busy", **BUSY), tmp_path)
    for kind in ("impl", "gate", "compile"):
        r = hr(d, kind, "other")["decision"]
        assert r["pass"] is False and r["reason"] == ""


# ---- status ---------------------------------------------------------------------------------------


def test_status_carries_the_light_lane_and_jobs(fake_host, tmp_path):
    host = fake_host("calm")
    d = Daemon(
        host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=parity_config(),
    )  # fmt: skip
    os.makedirs(d.run_dir)
    d.sampler.prime(host.prev_stat)
    first = d.tick()
    assert validate_status(first) == []
    assert first["light"]["running"] == 0 and first["light"]["max"] == 4
    add_jobs(d, 2, project="web-app")
    st = d.dispatch({"v": VERSION, "op": "status"})["status"]
    assert st["light"] == {
        "running": 2, "max": 4, "per_project_max": 2, "cpu_quota_pct": 100, "memory_max_mb": 1024,
        "max_runtime_s": 600,
    }  # fmt: skip
    assert [j["kind"] for j in st["jobs"]] == ["light", "light"]
    assert validate_status(st) == []
    assert "  light 2/4 (per project max 2)" in loadctl.format_status(st)


# ---- loadctl run: scope limits, runtime kill, overrun ---------------------------------------------


def test_argv_has_the_limits_for_light_only():
    lim = {"cpu_quota_pct": 50, "memory_max_mb": 512, "max_runtime_s": 120}
    argv = run.systemd_run_argv("u", "s.slice", "light", ["a"], light=lim)
    pre = argv[: argv.index("--")]
    assert pre[pre.index("CPUQuota=50%") - 1 : pre.index("CPUQuota=50%") + 1] == ["-p", "CPUQuota=50%"]
    for prop in ("MemoryMax=512M", "MemorySwapMax=0", "RuntimeMaxSec=150"):
        assert pre[pre.index(prop) - 1] == "-p"
    default = run.systemd_run_argv("u", "s.slice", "light", ["a"])
    assert "CPUQuota=100%" in default and "MemoryMax=1024M" in default and "RuntimeMaxSec=630" in default
    for kind in ("impl", "review", "gate", "compile"):
        assert "-p" not in run.systemd_run_argv("u", "s.slice", kind, ["a"], light=lim)


def clock_stepping(step):
    box = [-step]

    def now():
        box[0] += step
        return 1_762_800_000.0 + box[0]

    return now


def light_run(monkeypatch, fake_host, tmp_path, procs, label="lint", config=None, **proc_kw):
    host = fake_host("calm")
    d = run_daemon(host, tmp_path)
    if config:
        d.config = config
    ops = []
    run_wire(monkeypatch, d, ops)
    h = Host(host, procs().pid, **proc_kw)
    return host, d, h, ops


def go(h, *extra):
    a = loadctl._parser().parse_args(
        ["run", "--kind", "light", "--project", "web-app", "--label", "lint", *extra, "--", "ruff", "check"]
    )
    return run.run_command(a, h.deps)


def test_light_job_runs_with_the_configured_limits_and_passes_the_exit_code(
    fake_host,
    tmp_path,
    monkeypatch,
    procs,
    capsys,
):
    cfg = dataclasses.replace(parity_config(), light=LightConfig(50, 512, 90, 2))
    _, d, h, ops = light_run(monkeypatch, fake_host, tmp_path, procs, code=3)
    d.config = cfg
    h.deps.oom_kills = lambda cg: None
    assert go(h) == 3
    argv = h.launched[0][0]
    for prop in ("CPUQuota=50%", "MemoryMax=512M", "MemorySwapMax=0", "RuntimeMaxSec=120"):
        assert prop in argv
    assert ops == ["status", "headroom", "headroom", "status", "job_start", "job_end"]
    assert "LIGHT_OVERRUN" not in capsys.readouterr().err
    assert not [e for e in d.audit.tail(50) if e["op"] == "light_overrun"]


def test_runtime_limit_kills_the_scope_and_exits_with_the_overrun_code(
    fake_host,
    tmp_path,
    monkeypatch,
    procs,
    capsys,
):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, timeouts=5)
    h.deps.now = clock_stepping(400)  # unit stamp, deadline, then 1200 s on the first poll
    h.deps.oom_kills = lambda cg: None
    assert go(h) == run.EXIT_LIGHT_OVERRUN == 76
    kills = [r for r in h.ran if r[:2] == ["systemctl", "kill"]]
    assert kills and kills[0][2] == "--signal=SIGKILL" and kills[0][3].startswith("loadgov-light-lint-")
    assert "LIGHT_OVERRUN lint: runtime 600 s exceeded" in capsys.readouterr().err
    log = d.audit.tail(50)
    over = [e for e in log if e["op"] == "light_overrun"]
    assert len(over) == 1 and over[0]["reason"] == "light_overrun: runtime 600 s exceeded"
    end = [e for e in log if e["op"] == "job_end"][-1]
    assert end["reason"] == "light_overrun: runtime 600 s exceeded"
    assert d.jobs.summary() == []


def test_a_job_within_its_runtime_is_not_killed(fake_host, tmp_path, monkeypatch, procs):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, timeouts=3, code=0)
    h.deps.now = clock_stepping(10)
    h.deps.oom_kills = lambda cg: None
    assert go(h) == 0
    kills = [r for r in h.ran if r[:2] == ["systemctl", "kill"]]
    assert len(kills) == 1  # only the cleanup of the own, finished scope, no runtime kill


def test_oom_kill_in_the_scope_is_a_memory_overrun(fake_host, tmp_path, monkeypatch, procs, capsys):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=-9)
    h.deps.oom_kills = lambda cg: 1
    assert go(h) == 76
    assert "LIGHT_OVERRUN lint: memory limit 1024 MB hit" in capsys.readouterr().err
    end = [e for e in d.audit.tail(50) if e["op"] == "job_end"][-1]
    assert end["reason"] == "light_overrun: memory limit 1024 MB hit"


def test_oom_kill_is_found_when_the_scope_and_its_memory_events_are_gone(
    fake_host, tmp_path, monkeypatch, procs, capsys
):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=137)
    h.deps.oom_kills = lambda cg: None  # the cgroup vanished with the single killed process
    h.deps.unit_result = lambda unit: "oom-kill"  # but the uncollected scope still has systemd's result
    assert go(h) == 76
    assert "LIGHT_OVERRUN lint: memory limit 1024 MB hit" in capsys.readouterr().err
    assert [e for e in d.audit.tail(50) if e["op"] == "light_overrun"]


def test_light_scope_is_not_collected_and_is_stopped_by_loadctl_itself(
    fake_host, tmp_path, monkeypatch, procs
):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=0)
    h.deps.oom_kills = lambda cg: None
    h.deps.unit_result = lambda unit: "success"
    assert go(h) == 0
    assert "--collect" not in h.launched[0][0]
    unit = next(a for a in h.launched[0][0] if a.startswith("--unit=")).removeprefix("--unit=") + ".scope"
    cleanup = [r for r in h.ran if r[0] == "systemctl" and r[-1] == unit]
    assert cleanup == [
        ["systemctl", "kill", "--signal=SIGKILL", unit],
        ["systemctl", "stop", unit],
        ["systemctl", "reset-failed", unit],
    ]
    assert all(r[-1] == unit for r in h.ran if r[0] == "systemctl")
    assert "--collect" in run.systemd_run_argv("u", "s.slice", "impl", ["a"])


def test_job_end_rejects_a_bad_overrun_and_ignores_it_for_other_kinds(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    unit = "loadgov-impl-x-1.scope"
    d.jobs.jobs[unit] = Job("impl", "web-app", "x", unit, f"/x/{unit}", 1000, 1, 1000, None, d.t)
    bad = d.dispatch({"v": VERSION, "op": "job_end", "args": {"unit": unit, "overrun": "cpu"}})
    assert bad["ok"] is False and bad["error"] == "invalid_args"
    assert d.dispatch({"v": VERSION, "op": "job_end", "args": {"unit": unit, "overrun": "runtime"}})["ok"]
    assert not [e for e in d.audit.tail(50) if e["op"] == "light_overrun"]


def test_a_killed_light_job_is_recorded_in_job_stats(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    d.jobs.end(next(iter(d.jobs.jobs)))
    assert [(s["project"], s["kind"], s["count"]) for s in d.jobs.stats.summary()] == [
        ("web-app", "light", 1)
    ]


# ---- the overrun alert ----------------------------------------------------------------------------


class Clock:
    t = 1_762_800_000.0

    def __call__(self):
        return self.t


def sample():
    free = {"state": "free", "holder": None, "held_since": None, "stuck": False}
    return {
        "memory": {"avail_mb": 12000}, "swap": {"used_mb": 0}, "disk": {"free_gb": 48.0},
        "slots": {"heavy": dict(free), "compile": dict(free)}, "heavy_slot": {"stuck": False},
    }  # fmt: skip


def evaluate(w, c, step=30):
    c.t += step
    return w.evaluate(sample(), 10, 65, "balanced", None, [])


def test_three_overruns_in_24h_raise_a_warning_and_it_clears_after_24h_quiet():
    c = Clock()
    w = Watch(c)
    w.note_overrun("web-app", "lint")
    c.t += 60
    w.note_overrun("web-app", "shellcheck")
    assert evaluate(w, c) == []
    c.t += 60
    w.note_overrun("web-app", "fmt")
    out = evaluate(w, c)
    assert [(e["type"], e["key"], e["level"]) for e in out] == [("alert", "light_overrun:web-app", "warning")]
    assert "web-app" in out[0]["message"] and "fmt" in out[0]["message"]
    other = Watch(c)
    other.note_overrun("web-app", "a")
    other.note_overrun("proj-b", "b")
    other.note_overrun("web-app", "c")
    assert evaluate(other, c) == []  # per project, not host-wide
    c.t += 20 * 3600  # still inside 24 h of the last overrun: stays active
    assert [e for e in evaluate(w, c) if e["type"] == "resolved"] == []
    assert "light_overrun:web-app" in w.active
    c.t += 4 * 3600 + 120  # more than 24 h after the last overrun
    out = evaluate(w, c) + evaluate(w, c)
    assert [(e["type"], e["key"]) for e in out if e["type"] == "resolved"] == [
        ("resolved", "light_overrun:web-app")
    ]
    assert "light_overrun:web-app" not in w.active


def test_overruns_outside_the_24h_window_do_not_count():
    c = Clock()
    w = Watch(c)
    w.note_overrun("web-app", "a")
    w.note_overrun("web-app", "b")
    c.t += 25 * 3600
    w.note_overrun("web-app", "c")
    assert evaluate(w, c) == []


def test_daemon_job_end_with_overrun_feeds_the_alert(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    for _ in range(3):
        add_jobs(d, 1, project="web-app")
        unit = next(u for u, j in d.jobs.jobs.items() if j.kind == "light")
        r = d.dispatch({"v": VERSION, "op": "job_end", "args": {"unit": unit, "overrun": "runtime"}})
        assert r["ok"] and r["ended"]
    out = d.watch.evaluate(sample(), 10, 65, "balanced", None, [])
    assert [e["key"] for e in out] == ["light_overrun:web-app"]
