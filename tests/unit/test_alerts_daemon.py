"""Alerts through the daemon: status sections, the legacy tee, owner-change events, cancel-holder.

Holders are subprocesses this test starts; nothing else is ever signalled.
"""

import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import parity_config

from loadgov.config import HaConfig
from loadgov.daemon.main import Daemon
from loadgov.protocol import VERSION, validate_status

OWNER, AGENT_IN_LOOP = 501, 600
T0 = 1_762_800_000.0


class Clock:
    t = T0

    def __call__(self):
        return self.t


def add_proc(root, pid, cgroup, cmdline, ppid=1):
    d = Path(root) / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


@pytest.fixture
def spawn():
    started = []

    def go(*cmd):
        p = subprocess.Popen(cmd or [sys.executable, "-c", "import time; time.sleep(60)"])
        started.append(p)
        return p

    yield go
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


def make(fake_host, tmp_path, *, enforce=False, ha=False, tee=(), name="calm"):
    host = fake_host(name)
    add_proc(host.proc, 1, "/init.scope", "init")
    add_proc(host.proc, OWNER, "/user.slice/s.scope", "loadctl", 1)
    add_proc(host.proc, AGENT_IN_LOOP, "/loop-a", "claude", 1)
    base = parity_config()
    cfg = replace(
        base,
        slots_enforce=enforce,
        ha=HaConfig(ha, "http://127.0.0.1:9"),
        compat=replace(base.compat, alert_log_paths=tuple(tee)),
    )
    d = Daemon(
        host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=cfg, clock=Clock(),
        tokens_file=str(tmp_path / "tokens.env"),
    )  # fmt: skip
    os.makedirs(d.run_dir, exist_ok=True)
    d.sampler.prime(host.prev_stat)
    return d, host


def call(d, op, peer=OWNER, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args}, peer)


def audit(d):
    with open(d.audit.path) as f:
        return [json.loads(line) for line in f]


def test_status_has_alerts_and_ha_push_and_validates(fake_host, tmp_path):
    d, _ = make(fake_host, tmp_path)
    s = d.tick()
    assert validate_status(s) == []
    assert s["alerts"] == {"active": [], "events": []}
    assert s["ha_push"]["state"] == "disabled"
    assert call(d, "status")["status"]["alerts"] == s["alerts"]


def test_evaluation_runs_every_sixth_tick_and_alerts_on_three_busy_ones(fake_host, tmp_path):
    d, _ = make(fake_host, tmp_path, ha=True)
    base = d.sampler.sample()
    for i in range(1, 19):
        d.clock.t += 5
        d._watch_step({**base, "cpu": {**base["cpu"], "busy_pct": 82.0}})
        assert d.watch.summary()["events"] == [] or i == 18  # nothing before the third evaluation
    ev = d.watch.summary()["events"]
    assert [(e["type"], e["key"]) for e in ev] == [("alert", "cpu")]
    assert d.ha.status()["queued"] == 1
    assert ev[0]["profile"] == "balanced"


def test_interval_top3_is_used_for_alerts(fake_host, tmp_path):
    d, host = make(fake_host, tmp_path)
    base = d.sampler.sample()
    d._prev_procs = (d.clock.t, {101: (0, 0, "rustc", 6800), 102: (0, 900_000, "service", 3000)})
    d.clock.t += 30
    d.sampler.proc_ticks = {101: (0, 2000, "rustc", 6800), 102: (0, 900_100, "service", 3000),
                            103: (5, 600, "node", 900)}  # fmt: skip
    d._ticks = d._watch_every - 1  # the next step is the 30 s evaluation
    seen = []
    d.watch.evaluate = lambda *a, **k: seen.append(a[5]) or []
    d._watch_step(base)
    assert [(t["comm"], t["pcpu"]) for t in seen[0]] == [("rustc", 66.7), ("node", 20.0), ("service", 3.3)]


def test_legacy_tee_appends_the_host_watch_line(fake_host, tmp_path):
    log = tmp_path / "host-alerts.log"
    d, _ = make(fake_host, tmp_path, tee=[str(log)])
    base = d.sampler.sample()
    s = {**base, "cpu": {**base["cpu"], "busy_pct": 82.0, "load1": 5.4, "psi_some_avg10": 48.0}}
    top = [{"pid": 101, "comm": "rustc", "pcpu": 97.1, "rss_mb": 6800}]
    d.tee.evaluate(82, 12000, 0), d.tee.evaluate(82, 12000, 0)
    d._tee(s, 82.0, top)
    assert log.read_text() == (
        "ALERT cpu 18:40:00 load=5.40 cpu_some10=48.00 mem_avail_mb=12000 swap_used_mb=0 busy=82% "
        "top: rustc(97.1%,6800MB) \n"
    )


def test_tee_does_nothing_without_paths_and_survives_a_bad_path(fake_host, tmp_path):
    d, _ = make(fake_host, tmp_path)
    base = d.sampler.sample()
    for _ in range(3):
        d._tee({**base, "memory": {**base["memory"], "avail_mb": 100}}, 10.0, [])
    d2, _ = make(fake_host, tmp_path, tee=[str(tmp_path / "no-dir" / "a.log")])
    d2._tee({**base, "memory": {**base["memory"], "avail_mb": 100}}, 10.0, [])  # logs a warning only


def test_owner_change_via_socket_queues_an_event_but_rest_does_not(fake_host, tmp_path):
    d, _ = make(fake_host, tmp_path, ha=True)
    r = call(d, "set", owner=True, max_cores=2.0, reason="fan is loud")
    assert r["ok"]
    ev = [e for e in d.watch.summary()["events"] if e["key"] == "owner_change"]
    assert len(ev) == 1 and ev[0]["type"] == "info" and d.ha.status()["queued"] == 1
    assert ev[0]["who"]["pid"] == OWNER and ev[0]["who"]["cmdline"] == "loadctl"
    assert ev[0]["who"]["cgroup"] == "/user.slice/s.scope"
    assert ev[0]["what"] == "set" and ev[0]["reason"] == "fan is loud"
    assert call(d, "set", max_cores=1.5, reason="agent")["ok"]  # an agent request is not owner-level
    assert d.ha.status()["queued"] == 1
    assert call(d, "set", AGENT_IN_LOOP, owner=True, max_cores=1.5, reason="x")["error"] == "owner_denied"
    assert d.ha.status()["queued"] == 1
    for op, args in (
        ("set", {"max_cores": 1.0}),
        ("ceiling_set", {"profile": "silent"}),
        ("reset", {"all": True}),
    ):
        assert d.rest_call(op, {**args, "reason": "from HA"}, "198.51.100.9")["ok"]
    assert d.ha.status()["queued"] == 1  # REST changes are not echoed to the phone
    assert call(d, "hold", state="on", owner=True, reason="tv")["ok"]
    assert call(d, "ceiling_set", owner=True, profile="silent", reason="r")["ok"]
    assert call(d, "reset", owner=True, all=True, reason="r")["ok"]
    assert d.ha.status()["queued"] == 4


def test_guard_hook_reaches_the_queue(fake_host, tmp_path):
    d, _ = make(fake_host, tmp_path, ha=True)
    ev = d.emit_event("pause", {"message": "paused implementer 4242"})
    assert ev["actions"] == ["unfreeze"] and d.ha.status()["queued"] == 1


# ---- cancel_holder -------------------------------------------------------------------------------


def hold(d, p, slot="heavy", lock_fd=True):
    r = call(
        d, "slot_acquire", slot=slot, project="proj-a", label="gate", pid=p.pid, wait=False, client_lock=True
    )
    assert r["status"] == "granted", r
    if lock_fd:  # the client holds the lock on an fd, as a gate shell does under enforce
        fd_dir = Path(d.fs.proc_root) / str(p.pid) / "fd"
        fd_dir.mkdir(parents=True, exist_ok=True)
        lock = {"heavy": "host-heavy-slot.lock", "compile": "host-compile-slot.lock"}[slot]
        (fd_dir / "9").symlink_to(d.fs.lock_path(lock))
    return r


def test_cancel_holder_needs_confirm_slot_and_the_owner(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    p = spawn()
    hold(d, p)
    assert call(d, "cancel_holder", owner=True, slot="heavy")["error"] == "bad_request"
    assert call(d, "cancel_holder", owner=True, slot="heavy", confirm="yes")["error"] == "bad_request"
    assert call(d, "cancel_holder", owner=True, confirm=True)["error"] == "invalid_args"
    assert call(d, "cancel_holder", owner=True, slot="x", confirm=True)["error"] == "invalid_args"
    assert call(d, "cancel_holder", slot="heavy", confirm=True)["error"] == "owner_denied"
    denied = call(d, "cancel_holder", AGENT_IN_LOOP, owner=True, slot="heavy", confirm=True)
    assert denied["error"] == "owner_denied"
    assert audit(d)[-1]["result"] == "refused" and audit(d)[-1]["cgroup"] == "/loop-a"
    assert p.poll() is None  # nothing was signalled


def test_cancel_holder_terms_a_test_started_holder_and_is_audited(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True, ha=True)
    p = spawn()
    hold(d, p)
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True, reason="stuck gate")
    assert r["ok"] and r["signalled"] is True and r["pid"] == p.pid and r["source"] == "loadgov"
    assert p.wait(timeout=5) == -signal.SIGTERM
    row = [a for a in audit(d) if a["op"] == "cancel_holder"][-1]
    assert row["result"] == "signalled" and row["principal"] == "owner" and row["reason"] == "stuck gate"
    assert row["after"] == f"SIGTERM pid {p.pid}" and row["pid"] == OWNER
    assert any(e["what"] == "cancel_holder heavy" for e in d.watch.events)  # the phone hears about it


def test_cancel_holder_without_enforce_signals_nothing(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=False)
    p = spawn()
    hold(d, p)
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] and r["signalled"] is False
    assert p.poll() is None and audit(d)[-1]["result"] == "would"


def test_cancel_holder_refuses_a_dead_holder_and_a_free_slot(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    assert call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)["error"] == "invalid_args"
    p = spawn()
    hold(d, p)
    p.kill()
    p.wait()
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "no holder" in r["message"]  # the exited holder was released first


def test_cancel_holder_refuses_a_reused_pid(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    p = spawn()
    hold(d, p)
    d.slots.slots["heavy"].holder.start += 1  # the pid now names a different process
    d.slots.tick = lambda: False  # (the tick would release it; the check itself must also refuse)
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "no longer" in r["message"]
    assert p.poll() is None


def test_cancel_holder_never_signals_the_daemon_or_an_owner_hold(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    assert call(d, "hold", state="on", owner=True, slot="heavy", reason="tv")["ok"]
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "owner hold" in r["message"]
    d.slots.slots["heavy"].holder.pid = os.getpid()
    d.slots.slots["heavy"].holder.kind = "job"
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "refusing" in r["message"]


def test_cancel_holder_over_rest_is_owner_and_audited_with_the_peer(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    p = spawn()
    hold(d, p, slot="compile")
    assert d.rest_call("cancel_holder", {"slot": "compile"}, "198.51.100.9")["error"] == "bad_request"
    r = d.rest_call("cancel_holder", {"slot": "compile", "confirm": True}, "198.51.100.9")
    assert r["ok"] and r["signalled"] and p.wait(timeout=5) == -signal.SIGTERM
    row = [a for a in audit(d) if a["op"] == "cancel_holder"][-1]
    assert row["source"] == "rest" and row["peer"] == "198.51.100.9" and row["principal"] == "owner"
    assert not any(e["key"] == "owner_change" for e in d.watch.events)


def test_cancel_holder_refuses_a_holder_without_a_start_time(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    p = spawn()
    hold(d, p)
    d.slots.slots["heavy"].holder.start = None
    d.slots.tick = lambda: False
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "start time" in r["message"] and p.poll() is None


def test_cancel_holder_under_enforce_needs_the_lock_file_open(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    p = spawn()
    hold(d, p, lock_fd=False)
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "no longer holds the lock" in r["message"] and p.poll() is None


def test_cancel_holder_works_for_a_loadctl_run_gate_bound_to_its_scope(fake_host, tmp_path, spawn):
    """The loadctl-run pid holds the lock fd (the job inherits it): TERM goes to loadctl run, which
    forwards it to the scope."""
    d, _ = make(fake_host, tmp_path, enforce=True)
    run_pid, job = spawn(), spawn()
    hold(d, run_pid)
    unit = "loadgov-gate-g-1.scope"
    cg = f"/build.slice/build-work.slice/build-work-proj-a.slice/{unit}"
    procs = Path(d.fs.cgroup_root) / cg.lstrip("/")
    procs.mkdir(parents=True)
    (procs / "cgroup.procs").write_text(f"{job.pid}\n")
    r = call(d, "job_start", kind="gate", project="proj-a", label="gate", unit=unit, cgroup=cg,
             pid=job.pid, owner_pid=run_pid.pid, slot="heavy")  # fmt: skip
    assert r["ok"] and r["bound"]
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True, reason="stuck")
    assert r["ok"] and r["signalled"] is True and r["pid"] == run_pid.pid and r["source"] == "loadgov"
    assert run_pid.wait(timeout=5) == -signal.SIGTERM
    assert job.poll() is None  # only loadctl run is signalled; it kills the scope itself


def bound_gate(d, spawn, scope_pids=True):
    run_pid, job = spawn(), spawn()
    hold(d, run_pid)
    unit = "loadgov-gate-g-2.scope"
    cg = f"/build.slice/build-work.slice/build-work-proj-a.slice/{unit}"
    cgdir = Path(d.fs.cgroup_root) / cg.lstrip("/")
    cgdir.mkdir(parents=True)
    (cgdir / "cgroup.procs").write_text(f"{job.pid}\n" if scope_pids else "")
    fd_dir = Path(d.fs.proc_root) / str(job.pid) / "fd"  # the job inherited the lock descriptor
    fd_dir.mkdir(parents=True, exist_ok=True)
    (fd_dir / "9").symlink_to(d.fs.lock_path("host-heavy-slot.lock"))
    assert call(d, "job_start", kind="gate", project="proj-a", label="gate", unit=unit, cgroup=cg,
                pid=job.pid, owner_pid=run_pid.pid, slot="heavy")["bound"]  # fmt: skip
    return run_pid, job, unit


def test_cancel_holder_targets_the_scope_when_loadctl_run_is_gone(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    ran = []
    d.runner = lambda argv: ran.append(argv) or 0
    run_pid, job, unit = bound_gate(d, spawn)
    run_pid.kill()
    run_pid.wait()
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True, reason="stuck")
    assert r["ok"] and r["signalled"] is True and r["unit"] == unit
    assert ran == [["systemctl", "kill", "--signal=SIGTERM", unit]]  # nothing but that unit
    assert job.poll() is None and audit(d)[-1]["after"] == f"SIGTERM {unit}"


def test_cancel_holder_scope_path_is_confirm_owner_and_enforce_gated(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    ran = []
    d.runner = lambda argv: ran.append(argv) or 0
    run_pid, _job, _unit = bound_gate(d, spawn)
    run_pid.kill()
    run_pid.wait()
    assert call(d, "cancel_holder", owner=True, slot="heavy")["error"] == "bad_request"
    assert call(d, "cancel_holder", slot="heavy", confirm=True)["error"] == "owner_denied"
    d.slots.enforce = False
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] and r["signalled"] is False and ran == []  # observe: would only


def test_cancel_holder_refuses_an_empty_scope_and_a_scope_without_the_lock(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    d.runner = lambda argv: pytest.fail("must not run")
    run_pid, job, unit = bound_gate(d, spawn)
    run_pid.kill()
    run_pid.wait()
    (Path(d.fs.proc_root) / str(job.pid) / "fd" / "9").unlink()
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False and "holds the lock" in r["message"]
    (
        Path(d.fs.cgroup_root) / f"build.slice/build-work.slice/build-work-proj-a.slice/{unit}/cgroup.procs"
    ).write_text("")
    r = call(d, "cancel_holder", owner=True, slot="heavy", confirm=True)
    assert r["ok"] is False


def test_job_end_keeps_the_slot_while_the_scope_has_processes(fake_host, tmp_path, spawn):
    d, _ = make(fake_host, tmp_path, enforce=True)
    run_pid, job, unit = bound_gate(d, spawn)
    r = call(d, "job_end", unit=unit)
    assert r["ended"] is True and r["released"] is False
    assert d.slots.slots["heavy"].holder is not None
    cg = next(Path(d.fs.cgroup_root).rglob("cgroup.procs"))
    cg.write_text("")  # the scope empties
    d.fast_tick()
    assert d.slots.slots["heavy"].holder is None
