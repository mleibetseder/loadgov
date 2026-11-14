"""Stray-process guard: detection over fake /proc trees, exclusions, orphan logic, the owner kill."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from memguard_rig import AGENT, OWNER, Rig
from test_rest import TOKEN, http, run_rest, tokens

from loadgov.config import ConfigError, RestConfig, StraysConfig, parse_config
from loadgov.daemon.main import Daemon
from loadgov.daemon.stray_ops import StrayRuntime
from loadgov.protocol import validate_status

CG = "/user.slice/user-0.slice/session-c1.scope"
STRAY = 700
DEAD = 4242  # a pid that does not exist in any fixture tree


def make(tmp_path, fake_host, **cfg) -> Rig:
    """A rig whose daemon runs the guard with a 60 s check, 120 s minimum age and a fake pidfd layer."""
    rig = Rig(tmp_path, fake_host, enforce=False)
    cfg = {"check_s": 60, "min_age_s": 120, "enforce": True, **cfg}
    rig.cfg = replace(rig.cfg, strays=StraysConfig(**cfg))
    d = rig.new_daemon()
    k = rig.kernel
    d.strays = StrayRuntime(d, opener=k.opener, sender=k.sender, closer=k.closer)
    return rig


def burn(rig: Rig, pid: int, ticks: int) -> None:
    """Add `ticks` of CPU (utime) to a fixture process: 6000 ticks = 100 % of a core over 60 s at 100 Hz."""
    s = rig.specs[pid]
    s.ticks = getattr(s, "ticks", 0) + ticks
    zeros = ["0"] * 17
    zeros[9] = str(s.ticks)  # utime = field 14
    (rig.proc / str(pid) / "stat").write_text(
        f"{pid} ({s.comm}) {s.state} {s.ppid} " + " ".join(zeros) + f" {s.start} 0 0\n"
    )


def set_environ(rig: Rig, pid: int, **env) -> None:
    raw = b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items())
    (rig.proc / str(pid) / "environ").write_bytes(raw)


def add_stray(rig: Rig, pid=STRAY, comm="yes", ppid=1, cgroup=CG, cwd="/srv/work/proj-b/app", **kw) -> int:
    rig.add(pid, comm, ppid, cgroup=cgroup, cwd=cwd, **kw)
    set_environ(rig, pid, CLAUDE_PID=DEAD, CLAUDE_CODE_SESSION_ID="sess-1", PATH="/usr/bin")
    return pid


def interval(rig: Rig, hot: tuple = (STRAY,), n: int = 1, ticks: int = 6000) -> dict:
    """`n` checks 60 s apart, the `hot` processes burning a full core each time; the last status."""
    st = {}
    for _ in range(n):
        for pid in hot:
            burn(rig, pid, ticks)
        rig.step(dt=60)
        st = rig.d.status["strays"]
    return st


def reach_stray(rig: Rig, **kw) -> dict:
    """Baseline check, then hot for 120 s: the process is a stray at the third check."""
    rig.step(dt=60)  # baseline: no earlier snapshot, so nothing can be hot
    return interval(rig, n=2, **kw)


def pids(st: dict) -> list[int]:
    return [i["pid"] for i in st["items"]]


# ---- detection ---------------------------------------------------------------------------------


def test_detection_over_checks_and_hot_since_reset(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    rig.step(dt=60)  # baseline at 18:41
    st = rig.d.status["strays"]
    assert st["items"] == [] and st["last_check"] == "2025-11-10T18:41:00Z"
    assert (st["enabled"], st["enforce"], st["check_s"]) == (True, True, 60)
    assert pids(interval(rig)) == []  # 18:42: hot for 60 s only
    assert pids(interval(rig, hot=())) == []  # 18:43: a cool interval, hot_since is dropped
    assert pids(interval(rig)) == []  # 18:44: hot again, 60 s (without the reset it would be 180 s)
    st = interval(rig)  # 18:45: hot for 120 s
    assert pids(st) == [STRAY]
    it = st["items"][0]
    assert it["age_s"] == 120 and it["cpu_pct"] == 100.0 and it["orphan"] is True
    assert it["hot_since"] == "2025-11-10T18:43:00Z"  # the start of the interval that began the new run


def test_stray_record_fields(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig, cmd="yes " + "x" * 300)
    it = reach_stray(rig)["items"][0]
    assert it["pid"] == STRAY and it["start_time"] == 1000 + STRAY
    assert (it["comm"], it["cwd"], it["cgroup"]) == ("yes", "/srv/work/proj-b/app", CG)
    assert it["project"] == "proj-b" and len(it["cmdline"]) == 200
    assert it["session"] == {"claude_pid": DEAD, "session_id": "sess-1"}
    assert it["hot_since"] == "2025-11-10T18:41:00Z" and it["age_s"] == 120
    rig.step(dt=5)  # the daemon tick is faster than check_s: no new check
    assert rig.d.status["strays"]["last_check"] == "2025-11-10T18:43:00Z"


def test_project_is_null_outside_known_cwds(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig, cwd="/home/someone/scratch")
    it = reach_stray(rig)["items"][0]
    assert it["project"] is None and it["cwd"] == "/home/someone/scratch"


def test_a_process_below_the_cpu_threshold_is_not_hot(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    rig.step(dt=60)
    assert pids(interval(rig, n=4, ticks=2900)) == []  # 48 % of a core, below 50
    assert pids(interval(rig, n=1, ticks=3000)) == []  # exactly 50 % is hot (at least min_cpu_pct) ...
    assert pids(interval(rig, n=1, ticks=3000)) == [STRAY]  # ... and 120 s of it makes a stray


def test_pid_reuse_is_a_new_process(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    rig.step(dt=60)
    interval(rig, n=2)
    rig.remove(STRAY)
    add_stray(rig, start=777)  # same pid, new start time
    assert pids(interval(rig, n=1)) == []  # no baseline for (pid, 777), so no hot interval yet


def test_disabled_guard_reads_nothing(tmp_path, fake_host):
    rig = make(tmp_path, fake_host, enabled=False)
    add_stray(rig)
    rig.step(dt=60)
    st = interval(rig, n=3)
    assert st["items"] == [] and st["last_check"] is None and st["enabled"] is False


# ---- exclusions --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cgroup",
    [
        "/system.slice/example.service",
        "/system.slice/docker-abc123.scope",
        "/docker/abc123",
        "/user.slice/user-0.slice/user@0.service/app.slice/libpod-abc123.scope",
        "/machine.slice/containerd-abc123.scope",
        "/init.scope",
        "/user.slice/keep-this/session-c9.scope",  # matched by exclude_cgroups below
    ],
)
def test_excluded_cgroups_are_never_strays(tmp_path, fake_host, cgroup):
    rig = make(tmp_path, fake_host, exclude_cgroups=("/user.slice/keep-*/*",))
    add_stray(rig, pid=701, cgroup=cgroup)
    add_stray(rig, pid=702)  # the control: a plain stray is found in the same checks
    assert pids(reach_stray(rig, hot=(701, 702))) == [702]


def test_a_registered_job_scope_is_never_a_stray(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig, pid=701)
    add_stray(rig, pid=702)
    rig.scope("job-a.scope", "compile", [701], project="proj-b")
    assert pids(reach_stray(rig, hot=(701, 702))) == [702]


@pytest.mark.parametrize(
    "kind",
    ["claude", "kernel-ppid2", "kernel-empty-cmdline", "daemon-itself"],
)
def test_kernel_threads_the_agent_and_the_daemon_are_never_strays(tmp_path, fake_host, kind):
    rig = make(tmp_path, fake_host)
    add_stray(rig, pid=702)
    if kind == "claude":
        add_stray(rig, pid=701, comm="claude")
    elif kind == "kernel-ppid2":
        add_stray(rig, pid=701, comm="kworker", ppid=2)
    elif kind == "kernel-empty-cmdline":
        add_stray(rig, pid=701, comm="kworker", cmd="")
    else:
        add_stray(rig, pid=701, comm="loadgovd")
        rig.d.strays.detector.self_pid = lambda: 701
    assert pids(reach_stray(rig, hot=(701, 702))) == [702]


# ---- orphan logic ------------------------------------------------------------------------------


def test_orphan_conditions(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    rig.add(800, "bash", 1, cgroup=CG)  # a shell of the user's session
    rig.add(801, "bash", AGENT, cgroup=CG)  # a shell below the live agent
    cases = {
        701: dict(ppid=1),  # reparented to init, the dead-agent case
        702: dict(ppid=800, env={"CLAUDE_PID": DEAD}),  # parent alive, but its CLAUDE_PID is dead
        703: dict(ppid=800, env={"CLAUDE_PID": OWNER}),  # CLAUDE_PID names a pid that is not a claude
        704: dict(ppid=800, env={"CLAUDE_PID": AGENT}),  # CLAUDE_PID names a live claude: not orphaned
        705: dict(ppid=800, env={}),  # an ordinary child of a live shell, no claim: not orphaned
        706: dict(ppid=801, env={"CLAUDE_PID": DEAD}),  # a live claude among its ancestors: not a stray
        707: dict(ppid=1, env=None),  # no environ file at all (unreadable): empty, ppid 1 decides
    }
    for pid, c in cases.items():
        add_stray(rig, pid=pid, ppid=c["ppid"])
        if c.get("env") is None and "env" in c:
            os.remove(rig.proc / str(pid) / "environ")
        elif "env" in c:
            set_environ(rig, pid, **c["env"])
    st = reach_stray(rig, hot=tuple(cases))
    assert pids(st) == [701, 702, 703, 707]
    by = {i["pid"]: i for i in st["items"]}
    assert by[702]["session"] == {"claude_pid": DEAD, "session_id": None}
    assert by[707]["session"] == {"claude_pid": None, "session_id": None}


# ---- events ------------------------------------------------------------------------------------


def events(rig, key=None):
    return [e for e in rig.d.watch.events if key is None or e["key"] == key]


def test_alert_once_then_resolved(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    (alert,) = events(rig, f"stray:{STRAY}")
    start = 1000 + STRAY
    assert (alert["type"], alert["level"], alert["title"]) == ("alert", "warning", "Stray process")
    assert alert["actions"] == [f"kill_stray:{STRAY}:{start}"]
    assert "yes" in alert["message"] and "proj-b" in alert["message"]
    assert "100% CPU" in alert["message"] and "2 min" in alert["message"]
    interval(rig, n=3)  # still a stray: no second alert
    assert len(events(rig, f"stray:{STRAY}")) == 1
    interval(rig, hot=())  # no longer hot
    assert [e["type"] for e in events(rig, f"stray:{STRAY}")] == ["alert", "resolved"]
    rig.remove(STRAY)
    interval(rig, hot=())
    assert len(events(rig, f"stray:{STRAY}")) == 2  # a gone process is not resolved twice
    ops = [a["result"] for a in rig.audit() if a["op"] == "stray"]
    assert ops == ["detected", "resolved"]


def test_a_stray_that_vanishes_is_resolved(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    rig.remove(STRAY)
    interval(rig, hot=())
    assert [e["type"] for e in events(rig, f"stray:{STRAY}")] == ["alert", "resolved"]


# ---- the owner kill ----------------------------------------------------------------------------


def kill(rig, pid=STRAY, start=None, **kw):
    return rig.owner("stray_kill", pid=pid, **({} if start is None else {"start_time": start}), **kw)


def test_kill_sends_term_then_kill_after_30_s(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    reply = kill(rig, start=1000 + STRAY, reason="loop leak")
    assert reply["ok"] and reply["killed"] and reply["signal"] == "SIGTERM" and "SIGKILL" in reply["message"]
    assert rig.kernel.signals(pid=STRAY) == [(STRAY, "SIGTERM")]
    assert rig.d.status is not None and rig.d.strays.status()["items"] == []
    rig.step(dt=20)
    assert rig.kernel.signals("SIGKILL") == []  # not yet
    rig.step(dt=10)  # 30 s after the TERM, process still alive
    assert rig.kernel.signals(pid=STRAY) == [(STRAY, "SIGTERM"), (STRAY, "SIGKILL")]
    rig.step(dt=60)
    assert rig.kernel.signals("SIGKILL") == [(STRAY, "SIGKILL")]  # once
    rows = [a for a in rig.audit() if a["op"] == "stray" and a["principal"] == "owner"]
    assert [r["result"] for r in rows] == ["applied"] and rows[0]["reason"].endswith(": loop leak")
    assert "KILL stray yes" in rows[0]["reason"] and rows[0]["after"] == f"SIGTERM pid {STRAY}"
    tail = [(e["type"], e["key"]) for e in events(rig) if e["key"].endswith(str(STRAY))]
    assert tail == [("alert", f"stray:{STRAY}"), ("info", f"stray_killed:{STRAY}"),
                    ("resolved", f"stray:{STRAY}"), ("info", f"stray_killed:{STRAY}")]  # fmt: skip


def test_kill_that_works_needs_no_sigkill(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    rig.kernel.die_on.add(STRAY)
    assert kill(rig)["ok"]  # start_time omitted: the current stray with that pid
    rig.step(dt=30)
    assert rig.kernel.signals(pid=STRAY) == [(STRAY, "SIGTERM")]
    info = events(rig, f"stray_killed:{STRAY}")
    assert [e["type"] for e in info] == ["info", "info"]
    first, second = (e["message"] for e in info)
    assert "yes" in first and "proj-b" in first and "SIGTERM" in first and "sent" in first
    assert "owner (pid 501, loadctl)" in first and "exited after SIGTERM" in second
    assert info[0]["title"] == "Stray process killed" and info[0]["level"] == "info"


def test_refusals(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    start = 1000 + STRAY

    def refused(reply, text):
        assert reply["ok"] is False and reply["refused"] is True and text in reply["reason"], reply
        assert rig.kernel.sent == []

    refused(kill(rig, pid=DEAD), "not a current stray")
    refused(kill(rig, pid=OWNER), "not a current stray")  # a real process that is not a stray
    refused(kill(rig, start=start + 1), "not a current stray")  # wrong start time
    rig.specs[STRAY].start = start + 5  # the process changed (pid reuse) after the last check
    rig.write(STRAY)
    refused(kill(rig, start=start), "the process changed")
    rig.specs[STRAY].start = start
    rig.specs[STRAY].comm = "claude"  # now the agent
    rig.write(STRAY)
    refused(kill(rig, start=start), "claude")
    rig.specs[STRAY].comm = "yes"
    rig.write(STRAY)
    rig.specs[STRAY].ppid = 800  # adopted by a live claude's shell since the last check: no longer a stray
    rig.add(800, "bash", AGENT, cgroup=CG)
    rig.write(STRAY)
    refused(kill(rig, start=start), "no longer meets the stray conditions")
    rig.remove(STRAY)
    refused(kill(rig, start=start), "gone")
    assert {a["result"] for a in rig.audit() if a["op"] == "stray" and a["principal"] == "owner"} == {
        "refused"
    }


def test_enforce_off_audits_would_kill_and_signals_nothing(tmp_path, fake_host):
    rig = make(tmp_path, fake_host, enforce=False)
    add_stray(rig)
    reach_stray(rig)
    reply = kill(rig, start=1000 + STRAY)
    assert reply["refused"] is True and "enforce is off" in reply["reason"]
    assert rig.kernel.sent == []
    (row,) = [a for a in rig.audit() if a["result"] == "would"]
    assert row["op"] == "stray" and row["reason"].startswith("WOULD KILL stray yes")
    rig.step(dt=60)
    assert rig.kernel.sent == [] and events(rig, f"stray_killed:{STRAY}") == []


def test_agents_cannot_kill_and_bad_args_are_rejected(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    r = rig.call("stray_kill", pid=STRAY)  # no owner claim
    assert r["error"] == "owner_denied" and rig.kernel.sent == []
    r = rig.call("stray_kill", peer=AGENT, owner=True, pid=STRAY)  # a claimed owner inside an agent cgroup
    assert r["error"] == "owner_denied" and rig.kernel.sent == []
    assert rig.owner("stray_kill")["error"] == "invalid_args"  # pid missing
    assert rig.owner("stray_kill", pid="7")["error"] == "bad_request"
    assert rig.owner("stray_kill", pid=True)["error"] == "bad_request"
    assert rig.owner("stray_kill", pid=1)["error"] == "invalid_args"
    assert rig.owner("stray_kill", pid=STRAY, start_time="x")["error"] == "bad_request"
    assert rig.owner("stray_kill", pid=STRAY, extra=1)["error"] == "bad_request"
    assert rig.owner("stray_kill", pid=STRAY, reason="\x00")["error"] == "invalid_args"
    assert rig.kernel.sent == []
    denied = [a for a in rig.audit() if a["op"] == "stray" and a["result"] == "refused"]
    assert len(denied) == 2 and all(a["principal"] == "agent" for a in denied)


def test_the_daemon_never_signals_on_its_own(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    interval(rig, n=12)
    assert pids(rig.d.status["strays"]) == [STRAY] and rig.kernel.sent == []


def test_status_schema_with_items(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    add_stray(rig)
    reach_stray(rig)
    st = rig.d.dispatch({"v": 1, "op": "status"})["status"]
    assert validate_status(st) == [] and len(st["strays"]["items"]) == 1
    assert set(st["strays"]["items"][0]) == {
        "pid", "start_time", "comm", "cmdline", "cwd", "cgroup", "project", "cpu_pct", "hot_since",
        "age_s", "orphan", "session",
    }  # fmt: skip


# ---- REST --------------------------------------------------------------------------------------


def test_rest_kill_route(tmp_path, fake_host):
    rig = make(tmp_path, fake_host)
    cfg = replace(rig.cfg, rest=RestConfig(enabled=True, bind="127.0.0.1", port=0, rate_per_s=1000))
    k = rig.kernel
    d = Daemon(
        rig.host.fs, str(tmp_path / "run" / "s.sock"), str(tmp_path / "run"), None, 0.2, cfg,
        str(tmp_path / "state2"), str(tmp_path / "log2"), None, clock=rig.clock,
        tokens_file=tokens(tmp_path), stray_options={"opener": k.opener, "sender": k.sender, "closer": k.closer},
        rest_options={"own_addresses": set(), "allow_loopback": True, "backoff": (0.05, 0.2)},
        memguard_options={"opener": k.opener, "sender": k.sender, "closer": k.closer},
    )  # fmt: skip
    d.sampler.prime(rig.host.prev_stat)
    add_stray(rig)
    for i in range(4):  # a baseline check, then three hot ones
        if i:
            burn(rig, STRAY, 6000)
        rig.clock.t += 60
        d.tick()
    assert [i["pid"] for i in d.strays.status()["items"]] == [STRAY]

    async def scenario(port):
        post = "/v1/stray/kill"
        assert (await http(port, "POST", post, {"pid": STRAY}, token="x" * 43))[0] == 401
        assert (await http(port, "POST", post, {"pid": STRAY, "owner": True}))[
            0
        ] == 400  # owner is not a field
        assert (await http(port, "POST", post, {"pid": "7"}))[0] == 400
        assert (await http(port, "GET", post))[0] == 405
        c, b, _ = await http(port, "POST", post, {"pid": DEAD, "start_time": 1})
        assert c == 409 and b["refused"] is True and "not a current stray" in b["reason"]
        assert k.sent == []
        c, b, _ = await http(port, "POST", post, {"pid": STRAY, "start_time": 1000 + STRAY, "reason": "leak"})
        assert c == 200 and b["killed"] is True and b["signal"] == "SIGTERM"

    run_rest(d, scenario)
    assert k.signals(pid=STRAY) == [(STRAY, "SIGTERM")]
    rows = [r for r in (json.loads(x) for x in open(d.audit.path)) if r["op"] == "stray"]
    ok = [r for r in rows if r["result"] == "applied"]
    assert ok[0]["principal"] == "owner" and ok[0]["source"] == "rest" and ok[0]["peer"] == "127.0.0.1"
    assert (
        "owner via REST from 127.0.0.1"
        in [e for e in d.watch.events if e["key"] == f"stray_killed:{STRAY}"][0]["message"]
    )
    assert TOKEN not in open(d.audit.path).read()


# ---- config ------------------------------------------------------------------------------------


def test_config_defaults_and_values():
    c = parse_config("").strays
    assert c == StraysConfig(True, False, 300, 1800, 50, ())
    c = parse_config(
        "[strays]\nenabled = false\nenforce = true\ncheck_s = 60\nmin_age_s = 0\nmin_cpu_pct = 12.5\n"
        'exclude_cgroups = ["/user.slice/*/keep-*", "*/x.scope"]\n'
    ).strays
    assert c == StraysConfig(False, True, 60, 0, 12.5, ("/user.slice/*/keep-*", "*/x.scope"))


@pytest.mark.parametrize(
    "body",
    [
        "check_s = 59",
        "check_s = 90.5",
        'check_s = "300"',
        "check_s = true",
        "min_age_s = -1",
        "min_age_s = 1.5",
        "min_cpu_pct = 0",
        "min_cpu_pct = -5",
        'min_cpu_pct = "50"',
        "min_cpu_pct = true",
        "enabled = 1",
        'enforce = "yes"',
        "exclude_cgroups = 'x'",
        'exclude_cgroups = ["relative/*"]',
        "exclude_cgroups = [1]",
        "bogus = 1",
    ],
)
def test_config_rejects_bad_values(body):
    with pytest.raises(ConfigError, match="strays"):
        parse_config("[strays]\n" + body + "\n")
    with pytest.raises(ConfigError):
        parse_config("strays = 3")
