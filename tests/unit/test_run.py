"""loadctl run, the job_start/job_end ops and scope-bound slot lifetime. No systemd involved:
the launcher, systemctl and pkill are injected fakes; processes are real short `sleep`s we started."""

import json
import os
import signal
import subprocess
import time

import pytest
from conftest import parity_config

from loadgov.cli import loadctl, run
from loadgov.cli.client import DaemonUnavailable
from loadgov.daemon.main import Daemon
from loadgov.protocol import VERSION
from loadgov.sample import Sampler

T0 = 1_762_800_000.0
UNIT = "loadgov-impl-x-1.scope"
CG = "/build.slice/build-work.slice/build-work-demo.slice/" + UNIT


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


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


def make_daemon(host, tmp_path, enforce=False):
    d = Daemon(
        host.fs, clock=lambda: d.t, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"),
        env_file=None, config=parity_config(slots_enforce=enforce),
    )  # fmt: skip
    d.t = T0
    s = Sampler(host.fs)
    s.prime(host.prev_stat)
    d.status = s.sample(now=d.t)
    return d


def call(d, op, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args})


def set_procs(host, cg, pids):
    path = os.path.join(host.cgroup, cg.lstrip("/"))
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "cgroup.procs"), "w") as f:
        f.write("".join(f"{p}\n" for p in pids))


def wire(monkeypatch, d, log=None):
    def req(_sock, op, args=None, **_k):
        d.t += 1  # every request sees a newer sample, so the headroom confirm can pass
        d.status["at"] = stamp(d.t)
        d.status["at_ms"] = int(d.t * 1000)
        if log is not None:
            log.append(op)
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, os.getpid())  # `loadctl run` itself

    monkeypatch.setattr(loadctl, "request", req)


# ---- pure helpers -------------------------------------------------------------------------------


def test_names_and_argv_are_pure():
    assert run.sanitize("my label/x y") == "my-label-x-y"
    assert run.sanitize("///") == ""
    assert run.slice_name("proj-a") == "build-work-proj-a.slice"
    assert run.slice_name("a b") == "build-work-a-b.slice"
    assert run.slice_name("!!") == "build-work-default.slice"
    assert run.unit_name("gate", "job 229", "2026") == "loadgov-gate-job-229-2026"
    # systemd nests a slice under every dash-prefix: build-work-alpha.slice is below build.slice/build-work.slice
    bw = "/build.slice/build-work.slice/build-work-alpha.slice/u.scope"
    assert run.scope_cgroup("build-work-alpha.slice", "u") == bw
    assert run.scope_cgroup("build-work-grp-beta.slice", "u") == (
        "/build.slice/build-work.slice/build-work-grp.slice/build-work-grp-beta.slice/u.scope"
    )
    assert run.scope_cgroup("loadgov-test.slice", "u") == "/loadgov.slice/loadgov-test.slice/u.scope"
    head = [
        "systemd-run",
        "--scope",
        "--quiet",
        "--collect",
        "--slice=build-work-proj-a.slice",
        "--unit=u",
        "--",
    ]
    assert run.systemd_run_argv("u", "build-work-proj-a.slice", "impl", ["a", "b"]) == [
        *head, "nice", "-n", "10", "ionice", "-c", "2", "-n", "7", "a", "b"
    ]  # fmt: skip
    gate = run.systemd_run_argv("u", "s.slice", "gate", ["a"], rustc_running=True)
    assert gate[7:] == ["nice", "-n", "10", "ionice", "-c", "3", "a"]
    idle_free = run.systemd_run_argv("u", "s.slice", "compile", ["a"])
    assert idle_free[7:] == ["nice", "-n", "10", "ionice", "-c", "2", "-n", "7", "a"]
    review = run.systemd_run_argv("u", "s.slice", "review", ["a"], rustc_running=True)
    assert review[7:] == idle_free[7:]  # impl and review never use the idle class


EFF = {"cargo_build_jobs": 2, "vitest_max_workers": 2, "gate_shard_budget": 2}


def test_job_env_from_effective_limits():
    env = run.job_env({"PATH": "/bin"}, EFF, "impl", 20000, "u.scope")
    assert env == {
        "PATH": "/bin", "CARGO_BUILD_JOBS": "2", "VITEST_MAX_WORKERS": "2", "GATE_SHARD_BUDGET": "2",
        "GATE_SHARD_THREADS": "2", "LOADGOV_JOB": "u.scope",
    }  # fmt: skip
    gate3 = {"GATE_JOBS": "3"}
    assert run.job_env(gate3, EFF, "gate", 8192, "u")["CARGO_BUILD_JOBS"] == "3"
    assert run.job_env(gate3, EFF, "gate", 8191, "u")["CARGO_BUILD_JOBS"] == "2"
    assert run.job_env(gate3, EFF, "impl", 20000, "u")["CARGO_BUILD_JOBS"] == "2"
    assert run.job_env(gate3, {**EFF, "cargo_build_jobs": 1}, "gate", 20000, "u")["CARGO_BUILD_JOBS"] == "1"
    assert run.job_env({"GATE_SHARD_THREADS": "4"}, EFF, "gate", 1, "u")["GATE_SHARD_THREADS"] == "4"


# ---- daemon: job_start / job_end and the scope-bound slot ------------------------------------------


def grant_gate(d, owner):
    r = call(d, "slot_acquire", slot="heavy", project="proj-a", label="g", pid=owner, wait=True)
    assert r["status"] == "granted"


def start(d, job, owner, slot="heavy", unit=UNIT, cgroup=CG, **kw):
    args = {"kind": "gate", "project": "proj-a", "label": "g", "unit": unit, "cgroup": cgroup, "pid": job,
            "owner_pid": owner, **({"slot": slot} if slot else {}), **kw}  # fmt: skip
    return call(d, "job_start", **args)


def test_scope_binds_the_slot_and_outlives_the_client(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job = procs(), procs()
    grant_gate(d, client.pid)
    assert start(d, job.pid, client.pid)["bound"] is True
    set_procs(host, CG, [job.pid])
    jobs = call(d, "status")["status"]["jobs"]
    assert [(j["unit"], j["slot"], j["pid"]) for j in jobs] == [(UNIT, "heavy", job.pid)]
    # kill the client, keep the scope: the slot stays and an event is raised, once
    client.kill()
    client.wait()
    d.fast_tick()
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is not None
    rows = [json.loads(x) for x in open(d.audit.path)]
    assert [r["op"] for r in rows].count("slot_holder_orphaned") == 1
    # the scope empties: released on the next tick, job dropped, though job_end never came
    set_procs(host, CG, [])
    job.kill()
    job.wait()
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is None
    assert call(d, "status")["status"]["jobs"] == []
    assert "released: holder exited" in [r["reason"] for r in map(json.loads, open(d.audit.path))]


def test_scope_with_processes_keeps_the_slot_even_if_the_job_pid_is_gone(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job, child = procs(), procs(), procs()
    grant_gate(d, client.pid)
    start(d, job.pid, client.pid)
    set_procs(host, CG, [child.pid])
    job.kill()
    job.wait()
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is not None  # a child still runs in the scope


def test_cgroup_gone_falls_back_to_the_job_pid(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job = procs(), procs()
    grant_gate(d, client.pid)
    start(d, job.pid, client.pid)  # no cgroup.procs file: unknown cgroup
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is not None
    job.kill()
    job.wait()
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is None


def test_job_end_releases_the_slot_at_once(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job = procs(), procs()
    grant_gate(d, client.pid)
    start(d, job.pid, client.pid)
    set_procs(host, CG, [])  # the scope is empty: job_end frees the slot at once
    r = call(d, "job_end", unit=UNIT)
    assert (r["ended"], r["released"]) == (True, True)
    assert d.slots.slots["heavy"].holder is None
    assert call(d, "job_end", unit=UNIT)["ended"] is False


def test_job_without_slot_is_listed_and_dropped(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    job = procs()
    assert start(d, job.pid, os.getpid(), slot=None, kind="impl")["bound"] is False
    set_procs(host, CG, [job.pid])
    d.fast_tick()
    assert len(d.jobs.summary()) == 1
    set_procs(host, CG, [])
    job.kill()
    job.wait()
    d.fast_tick()
    assert d.jobs.summary() == []


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"unit": "evil.scope"}, "loadgov-*.scope"),
        ({"cgroup": "/x/../y/" + UNIT}, "cgroup must be"),
        ({"cgroup": "/a/other.scope"}, "cgroup must be"),
        ({"kind": "nope"}, "kind must be"),
        ({"slot": "bogus"}, "slot must be"),
        ({"pid": 999999999}, "not running"),
    ],
)
def test_job_start_validation(fake_host, tmp_path, procs, kw, msg):
    d = make_daemon(fake_host("calm"), tmp_path)
    job = procs()
    r = start(d, job.pid, os.getpid(), **{"slot": None, **kw})
    assert r["ok"] is False and msg in r["message"]


def test_job_start_refuses_a_slot_the_owner_does_not_hold(fake_host, tmp_path, procs):
    d = make_daemon(fake_host("calm"), tmp_path)
    job = procs()
    r = start(d, job.pid, os.getpid())
    assert r["ok"] is False and "does not hold" in r["message"]


def test_restart_readopts_a_live_scope(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job = procs(), procs()
    grant_gate(d, client.pid)
    start(d, job.pid, client.pid)
    set_procs(host, CG, [job.pid])
    d2 = make_daemon(host, tmp_path)  # same state dir
    assert [j["unit"] for j in d2.jobs.summary()] == [UNIT]
    h = d2.slots.slots["heavy"].holder
    assert h is not None and h.scope == UNIT
    set_procs(host, CG, [])
    job.kill()
    job.wait()
    d3 = make_daemon(host, tmp_path)
    assert d3.jobs.summary() == [] and d3.slots.slots["heavy"].holder is None


# ---- loadctl run ----------------------------------------------------------------------------------


class FakeProc:
    def __init__(self, pid, code=0, on_wait=None, timeouts=0):
        self.pid, self.code, self.on_wait, self.timeouts = pid, code, on_wait, timeouts
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.on_wait:
            self.on_wait(self)
        if self.timeouts > 0 and timeout is not None:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("job", timeout)
        return self.code


class Host:
    """Deps whose launcher records the call and returns a FakeProc standing in for the job."""

    def __init__(self, host, job_pid, **proc_kw):
        self.launched, self.ran, self.proc_kw, self.host = [], [], proc_kw, host
        self.job_pid = job_pid
        self.deps = run.Deps(
            popen=self.popen, run=lambda argv: self.ran.append(argv) or 0, sleep=lambda _s: None,
            now=lambda: T0, avail_mb=lambda: 1234, rustc_running=lambda: False,
            find_cgroup=lambda pid, unit, slice_: CG.replace(UNIT, unit),
        )  # fmt: skip

    def popen(self, argv, env=None, pass_fds=()):
        self.launched.append((argv, env, pass_fds))
        self.proc = FakeProc(self.job_pid, **self.proc_kw)
        return self.proc


def go(monkeypatch, d, deps, *argv):
    a = loadctl._parser().parse_args(["run", *argv])
    return run.run_command(a, deps)


def test_impl_run_passes_the_exit_code_and_registers_the_job(fake_host, tmp_path, monkeypatch, procs, capsys):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ops = []
    wire(monkeypatch, d, ops)
    job = procs()
    h = Host(host, job.pid, code=7)
    seen = {}
    h.proc_kw["on_wait"] = lambda _p: seen.update(jobs=d.jobs.summary())
    rc = go(
        monkeypatch, d, h.deps, "--kind", "impl", "--project", "demo", "--label", "my x", "--", "make", "-j2"
    )
    assert rc == 7
    argv, env, fds = h.launched[0]
    assert argv[:7] == ["systemd-run", "--scope", "--quiet", "--collect", "--slice=build-work-demo.slice",
                        argv[5], "--"]  # fmt: skip
    assert argv[5].startswith("--unit=loadgov-impl-my-x-") and argv[-2:] == ["make", "-j2"]
    assert fds == () and env["CARGO_BUILD_JOBS"] == "2" and env["LOADGOV_JOB"].endswith(".scope")
    assert [j["label"] for j in seen["jobs"]] == ["my x"] and seen["jobs"][0]["slot"] is None
    assert ops == ["status", "headroom", "headroom", "status", "job_start", "job_end"]
    assert d.jobs.summary() == []
    assert "HEADROOM_OK" in capsys.readouterr().err  # stdout stays the job's


def test_gate_takes_the_slot_before_the_headroom_wait_and_releases_it(
    fake_host, tmp_path, monkeypatch, procs
):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ops, held = [], {}
    wire(monkeypatch, d, ops)
    job = procs()
    h = Host(host, job.pid)
    h.proc_kw["on_wait"] = lambda _p: held.update(holder=d.slots.slots["heavy"].holder)
    rc = go(monkeypatch, d, h.deps, "--kind", "gate", "--project", "proj-a", "--label", "job-1", "--", "true")
    assert rc == 0
    assert ops.index("slot_acquire") < ops.index("headroom")
    assert held["holder"].scope and held["holder"].job_pid == job.pid  # bound to the scope while it runs
    assert d.slots.slots["heavy"].holder is None
    assert ops[-2:] == ["job_start", "job_end"]  # bound to the scope: no explicit slot_release


def test_gate_waits_in_the_slot_queue_behind_a_holder(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    other = procs()
    grant_gate(d, other.pid)
    h = Host(host, procs().pid)
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "gate",
        "--project",
        "proj-a",
        "--label",
        "g2",
        "--no-wait",
        "--",
        "true",
    )
    assert rc == run.EXIT_BUSY and h.launched == []
    assert d.slots.slots["heavy"].holder.pid == other.pid


def test_no_wait_without_room_exits_75(fake_host, tmp_path, monkeypatch, procs, capsys):
    host = fake_host("busy")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    h = Host(host, procs().pid)
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "impl",
        "--project",
        "proj-b",
        "--label",
        "x",
        "--no-wait",
        "--",
        "true",
    )
    assert rc == 75 and h.launched == []
    assert "HEADROOM_WAIT x" in capsys.readouterr().err


def test_headroom_timeout_gives_the_slot_back(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    monkeypatch.setattr(loadctl, "wait_headroom", lambda ns, sleep, out=None: 1)
    h = Host(host, procs().pid)
    rc = go(monkeypatch, d, h.deps, "--kind", "gate", "--project", "proj-a", "--label", "g", "--", "true")
    assert rc == 1 and h.launched == []
    assert d.slots.slots["heavy"].holder is None


def test_daemon_down_exits_3_and_starts_nothing(monkeypatch, fake_host, tmp_path, procs):
    def down(*_a, **_k):
        raise DaemonUnavailable("nope")

    monkeypatch.setattr(loadctl, "request", down)
    h = Host(fake_host("calm"), procs().pid)
    assert (
        go(monkeypatch, None, h.deps, "--kind", "impl", "--project", "p", "--label", "l", "--", "true") == 3
    )
    assert h.launched == []


def test_usage_errors(monkeypatch, fake_host, procs):
    h = Host(fake_host("calm"), procs().pid)
    assert go(monkeypatch, None, h.deps, "--kind", "impl", "--project", "p", "--label", "l") == 2
    assert (
        go(
            monkeypatch,
            None,
            h.deps,
            "--kind",
            "impl",
            "--project",
            "p",
            "--label",
            "l",
            "--floor-mb",
            "0",
            "--",
            "x",
        )
        == 2
    )


def test_killed_by_signal_is_128_plus_n(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    h = Host(host, procs().pid, code=-15)
    assert go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "p", "--label", "l", "--", "x") == 143


def test_floor_kill_kills_only_the_scope_and_the_jvm_daemons(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)

    def drop(p):
        if p.waits == 2:
            d.status["memory"]["avail_mb"] = 1000

    h = Host(host, procs().pid, on_wait=drop, timeouts=5)
    rc = go(monkeypatch, d, h.deps, "--kind", "compile", "--project", "tv", "--label", "guarded", "--floor-mb", "2560",
            "--on-floor", "kill", "--", "./gradlew", "build")  # fmt: skip
    unit = h.launched[0][1]["LOADGOV_JOB"]
    assert rc == 137
    assert h.ran == [["systemctl", "kill", "--signal=SIGKILL", unit], ["pkill", "-f", run.JVM_PATTERN]]
    assert d.slots.slots["compile"].holder is None


def test_floor_uses_meminfo_when_the_daemon_is_down(monkeypatch):
    def down(*_a, **_k):
        raise DaemonUnavailable("x")

    monkeypatch.setattr(loadctl, "request", down)
    a = loadctl._parser().parse_args(["run", "--kind", "impl", "--project", "p", "--label", "l", "--", "x"])
    assert run._floor_avail(a, run.Deps(avail_mb=lambda: 4321)) == 4321


def test_sigterm_is_forwarded_to_the_scope(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda s, h: handlers.setdefault(s, h) and None)

    h = Host(host, procs().pid)
    h.proc_kw["on_wait"] = lambda _p: handlers[signal.SIGTERM](signal.SIGTERM, None)
    go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "p", "--label", "l", "--", "x")
    unit = h.launched[0][1]["LOADGOV_JOB"]
    assert h.ran == [["systemctl", "kill", "--signal=SIGTERM", unit]]
    assert signal.SIGINT in handlers


def test_enforce_run_opens_and_inherits_the_lock(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path, enforce=True)
    wire(monkeypatch, d)
    h = Host(host, procs().pid)
    seen = {}

    def during(_p):
        lock = os.path.realpath(os.path.join(host.lock, "host-heavy-slot.lock"))
        fd = next(
            int(n) for n in os.listdir("/proc/self/fd") if os.path.realpath(f"/proc/self/fd/{n}") == lock
        )
        seen["inheritable"] = os.get_inheritable(fd)
        seen["holder"] = open(os.path.join(host.lock, "host-heavy-slot.holder")).read()
        probe = os.open(os.path.join(host.lock, "host-heavy-slot.lock"), os.O_RDWR)
        try:
            import fcntl

            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen["locked"] = False
        except BlockingIOError:
            seen["locked"] = True
        finally:
            os.close(probe)

    h.proc_kw["on_wait"] = during
    rc = go(monkeypatch, d, h.deps, "--kind", "gate", "--project", "proj-a", "--label", "g", "--", "true")
    assert rc == 0 and seen["inheritable"] and seen["locked"]
    assert seen["holder"].startswith("proj-a g pid=")


# ---- other loadctl pieces ---------------------------------------------------------------------------


def test_wait_override_flags_replace_thresholds_for_one_call(fake_host, tmp_path, monkeypatch, capsys):
    host = fake_host("busy")  # load 5.4: fails the default 4.0 for proj-b impl
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    sl = lambda _s: None  # noqa: E731
    base = ["wait", "--kind", "impl", "--project", "proj-b", "--timeout", "1"]
    over = [
        "--load1-max",
        "9",
        "--load5-max",
        "9",
        "--psi-max",
        "60",
        "--mem-min-gb",
        "1",
        "--swap-max-mb",
        "9999",
        "--mem-psi-max",
        "50",
    ]
    assert loadctl.main(base, sleep=sl) == 1
    assert loadctl.main(base + over, sleep=sl) == 0
    assert "max 9.00/9.00" in capsys.readouterr().out


def test_headroom_rejects_bad_overrides(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    for over in ({"nope": 1}, {"psi_max": -1}, {"psi_max": True}, "x"):
        r = call(d, "headroom", kind="impl", overrides=over)
        assert r["error"] == "bad_request", over


def test_level_wait_needs_two_samples_below_limit(monkeypatch, capsys):
    lv = iter(["LIMIT", "OK", "BUSY"])
    st = {"host": {"cores": 1}, "cpu": {"busy_pct": 0, "psi_some_avg60": 0, "load1": 0}, "memory": {"avail_mb": 1, "total_mb": 1},
          "swap": {"used_pct": 0, "used_mb": 0}, "disk": {"free_gb": 1, "paths": ["/"]}, "at": "2026-01-01T00:00:00Z"}  # fmt: skip

    def req(*_a, **_k):
        level = next(lv)
        return {
            "ok": True,
            "status": {**st, "level": level, "levels": dict.fromkeys(("cpu", "ram", "swap", "disk"), level)},
        }

    monkeypatch.setattr(loadctl, "request", req)
    ns = loadctl._parser().parse_args(["level-wait"])
    assert loadctl.level_wait(ns, sleep=lambda _s: None) == 0
    assert capsys.readouterr().out.startswith("hwmon 2026-01-01 00:00:00  level BUSY")


def test_level_wait_times_out_at_limit(monkeypatch, capsys):
    st = {"level": "LIMIT", "levels": dict.fromkeys(("cpu", "ram", "swap", "disk"), "LIMIT"), "at": "t", "host": {"cores": 1},
          "cpu": {"busy_pct": 0, "psi_some_avg60": 0, "load1": 0}, "memory": {"avail_mb": 1, "total_mb": 1},
          "swap": {"used_pct": 0, "used_mb": 0}, "disk": {"free_gb": 1, "paths": ["/"]}}  # fmt: skip
    monkeypatch.setattr(loadctl, "request", lambda *_a, **_k: {"ok": True, "status": st})
    clock = iter([0.0, 5.0, 11.0])
    ns = loadctl._parser().parse_args(["level-wait", "10"])
    assert loadctl.level_wait(ns, sleep=lambda _s: None, clock=lambda: next(clock)) == 1
    assert "hwmon wait: still at LIMIT after 10 s" in capsys.readouterr().out


def test_main_parses_run_with_the_command_after_double_dash(monkeypatch):
    seen = {}
    monkeypatch.setattr(run, "run_command", lambda a: seen.update(a=a) or 9)
    rc = loadctl.main(
        ["run", "--kind", "gate", "--project", "p", "--label", "l", "--no-wait", "--", "sh", "-c", "x -h"]
    )
    a = seen["a"]
    assert rc == 9 and a.cmd == "run" and a.no_wait and a.slice is None and a.floor_mb is None
    assert a.job_cmd == ["--", "sh", "-c", "x -h"] or a.job_cmd == ["sh", "-c", "x -h"]


def test_headroom_none_takes_the_slot_but_never_asks_for_headroom(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host(
        "busy"
    )  # no room at all; the compile slot is free (only the heavy one has a legacy holder)
    d = make_daemon(host, tmp_path)
    ops = []
    wire(monkeypatch, d, ops)
    h = Host(host, procs().pid)
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "compile",
        "--project",
        "proj-a",
        "--label",
        "c",
        "--headroom",
        "none",
        "--",
        "x",
    )
    assert rc == 0 and "headroom" not in ops and ops.index("slot_acquire") < ops.index("job_start")


def level_status(d, levels):
    """Make the daemon's status report the next level from `levels` (after that: OK)."""
    real = d.dispatch

    def dispatch(req, peer=None):
        r = real(req, peer)
        if req["op"] == "status" and r.get("ok"):
            lv = next(levels, "OK")
            r["status"] = {
                **r["status"],
                "level": lv,
                "levels": dict.fromkeys(("cpu", "ram", "swap", "disk"), lv),
            }
        return r

    d.dispatch = dispatch


def test_headroom_level_waits_for_the_hwmon_level_not_the_headroom(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ops = []
    wire(monkeypatch, d, ops)
    # status 1 is run's own first check, then LIMIT, LIMIT, OK, BUSY from the level wait, then the env refresh
    level_status(d, iter(["OK", "LIMIT", "BUSY", "OK", "OK"]))
    h = Host(host, procs().pid)
    sleeps = []
    h.deps.sleep = sleeps.append
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "compile",
        "--project",
        "tv",
        "--label",
        "guarded",
        "--headroom",
        "level",
        "--",
        "x",
    )
    assert rc == 0 and "headroom" not in ops
    assert sleeps == [
        15.0,
        15.0,
        15.0,
    ]  # LIMIT, BUSY (not OK: guarded waits for level OK), OK (1 of 2), OK (2 of 2)


def test_headroom_level_no_wait_at_limit_exits_75(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    level_status(d, iter(["OK", "LIMIT"]))
    h = Host(host, procs().pid)
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "impl",
        "--project",
        "p",
        "--label",
        "l",
        "--headroom",
        "level",
        "--no-wait",
        "--",
        "x",
    )
    assert rc == 75 and h.launched == []


def test_headroom_full_is_the_default():
    a = loadctl._parser().parse_args(["run", "--kind", "gate", "--project", "p", "--label", "l", "--", "x"])
    assert a.headroom == "full"


# ---- inherited descriptors, enforce off, signal ordering -----------------------------------------


def test_spawn_output_lists_only_the_pipe(tmp_path, capfd):
    r, w = os.pipe()
    os.set_inheritable(w, True)
    import socket

    sock = socket.socket(socket.AF_UNIX)
    try:
        probe = f"[ -e /proc/self/fd/{w} ] && echo pipe; [ -e /proc/self/fd/{sock.fileno()} ] && echo sock"
        run._spawn(["sh", "-c", probe], dict(os.environ)).wait()
    finally:
        sock.close()
        os.close(r)
        os.close(w)
    assert capfd.readouterr().out.split() == ["pipe"]


def lock_is_held(path):
    return subprocess.run(["flock", "-n", path, "true"]).returncode != 0


def test_gate_run_takes_the_real_lock_even_with_enforce_off(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path, enforce=False)
    wire(monkeypatch, d)
    h = Host(host, procs().pid)
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    seen = {}

    def during(_p):
        seen["held"] = lock_is_held(lock)
        seen["holder"] = open(os.path.join(host.lock, "host-heavy-slot.holder")).read()

    h.proc_kw["on_wait"] = during
    rc = go(monkeypatch, d, h.deps, "--kind", "gate", "--project", "proj-a", "--label", "g", "--", "true")
    assert rc == 0 and seen["held"] and seen["holder"].startswith("proj-a g pid=")
    assert not lock_is_held(lock)  # loadctl run closed its descriptor


def test_slot_acquire_fd_takes_the_real_lock_with_enforce_off(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path, enforce=False)
    wire(monkeypatch, d)
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    try:
        rc = loadctl.main(
            [
                "slot",
                "acquire",
                "heavy",
                "--project",
                "proj-a",
                "--label",
                "g",
                "--fd",
                str(fd),
                "--pid",
                str(procs().pid),
            ]
        )
        assert rc == 0 and lock_is_held(lock)
    finally:
        os.close(fd)
    assert not lock_is_held(lock)


def test_a_legacy_flock_holder_still_blocks_a_client_lock_with_enforce_off(
    fake_host, tmp_path, monkeypatch, procs
):
    import fcntl

    host = fake_host("calm")
    d = make_daemon(host, tmp_path, enforce=False)
    wire(monkeypatch, d)
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    legacy = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(legacy, fcntl.LOCK_EX)
    try:
        r = call(
            d,
            "slot_acquire",
            slot="heavy",
            project="b",
            label="g",
            pid=procs().pid,
            wait=False,
            client_lock=True,
        )
        assert r["status"] == "busy"
    finally:
        os.close(legacy)


def test_without_a_client_lock_enforce_off_stays_arbitration_only(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path, enforce=False)
    r = call(d, "slot_acquire", slot="heavy", project="b", label="g", pid=procs().pid, wait=False)
    assert r["status"] == "granted" and "lock_path" not in r


def test_signal_handlers_are_installed_before_the_launcher_starts(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    order = []
    handlers = {}

    def fake_signal(sig, h):
        order.append(f"signal {sig.name}")
        handlers[sig] = h
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    h = Host(host, procs().pid)
    real_popen = h.popen
    h.deps.popen = lambda argv, env=None, **kw: order.append("popen") or real_popen(argv, env, **kw)
    go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "p", "--label", "l", "--", "x")
    assert order[:3] == ["signal SIGINT", "signal SIGTERM", "popen"]


def test_a_signal_that_came_before_the_scope_existed_is_sent_again(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    handlers = {}
    monkeypatch.setattr(signal, "signal", lambda s, hd: handlers.setdefault(s, hd) and None)
    h = Host(host, procs().pid)
    real_popen = h.popen

    def popen(argv, env=None, **kw):
        handlers[signal.SIGTERM](signal.SIGTERM, None)  # TERM arrives while the launcher starts: no scope yet
        return real_popen(argv, env, **kw)

    h.deps.popen = popen
    go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "p", "--label", "l", "--", "x")
    unit = h.launched[0][1]["LOADGOV_JOB"]
    assert h.ran == [["systemctl", "kill", "--signal=SIGTERM", unit]] * 2  # the handler, then the repeat


def test_guarded_level_mode_needs_level_ok_in_no_wait(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    level_status(d, iter(["OK", "BUSY"]))
    h = Host(host, procs().pid)
    rc = go(
        monkeypatch,
        d,
        h.deps,
        "--kind",
        "impl",
        "--project",
        "p",
        "--label",
        "l",
        "--headroom",
        "level",
        "--no-wait",
        "--",
        "x",
    )
    assert rc == 75 and h.launched == []


def test_unreadable_scope_counts_as_alive_and_is_retried(fake_host, tmp_path, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    client, job = procs(), procs()
    grant_gate(d, client.pid)
    start(d, job.pid, client.pid)
    path = os.path.join(host.cgroup, CG.lstrip("/"), "cgroup.procs")
    os.makedirs(path)  # reading a directory fails with EISDIR: neither empty nor missing
    job.kill()
    job.wait()
    client.kill()
    client.wait()
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is not None and d.jobs.summary() != []  # fail safe: still held
    os.rmdir(path)
    set_procs(host, CG, [])  # readable again, and empty
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is None and d.jobs.summary() == []


def test_the_job_runs_in_its_own_session(monkeypatch):
    seen = {}
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    run._spawn(["true"], {})
    assert seen["start_new_session"] is True
