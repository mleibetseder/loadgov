"""Principal detection and the limit ops through Daemon.dispatch, against fixture /proc trees."""

import json
from pathlib import Path

import pytest
from conftest import parity_config

from loadgov.cli import loadctl
from loadgov.daemon.main import Daemon
from loadgov.daemon.principal import owner_denial
from loadgov.host.procfs import HostFS
from loadgov.protocol import VERSION, validate_status
from loadgov.sample import Sampler

T0 = 1_762_800_000.0


class Clock:
    t = T0

    def __call__(self):
        return self.t


def add_proc(root, pid, cgroup, cmdline, ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    add_proc(root, 1, "/init.scope", "init")
    add_proc(root, 500, "/user.slice/session-1.scope", "bash", 1)  # a plain interactive session
    add_proc(root, 501, "/user.slice/session-1.scope", "loadctl set", 500)
    add_proc(root, 600, "/loop-a", "claude -p", 1)
    add_proc(root, 610, "/build-work-proj-a.slice/x.scope", "claude", 1)
    add_proc(root, 620, "/build-work.slice/proj-a.slice/y.scope", "claude", 1)
    add_proc(root, 700, "/user.slice/session-1.scope", "bash /opt/proj-a/dispatch.sh run", 1)
    add_proc(root, 701, "/user.slice/session-1.scope", "claude", 700)
    add_proc(root, 702, "/user.slice/session-1.scope", "loadctl", 701)
    add_proc(root, 710, "/user.slice/session-1.scope", "bash /opt/proj-a/job-queue.sh", 1)
    add_proc(root, 711, "/user.slice/session-1.scope", "loadctl", 710)
    add_proc(root, 720, "/user.slice/session-1.scope", "bash /tmp/loop-a-snap.abc/run", 1)
    add_proc(root, 721, "/user.slice/session-1.scope", "loadctl", 720)
    return root


@pytest.fixture
def daemon(tmp_path, proc):
    clock = Clock()
    clock.t = T0
    fs = HostFS(proc_root=str(proc), cgroup_root=str(tmp_path / "cg"), lock_root=str(tmp_path / "lock"))
    d = Daemon(
        fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"),
        env_file=str(tmp_path / "load-governor.env"), config=parity_config(), clock=clock,
    )  # fmt: skip
    return d


def call(d, op, pid=501, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args}, pid)


def audit(d):
    return [json.loads(line) for line in open(d.audit.path)]


@pytest.mark.parametrize("pid", [600, 610, 620, 702, 711, 721])
def test_owner_denied_in_governed_cgroups_and_under_dispatchers(daemon, pid):
    assert owner_denial(daemon.fs, pid, daemon.config.principal) is not None
    r = call(daemon, "set", pid, owner=True, max_cores=2.0, reason="x")
    assert r["error"] == "owner_denied"
    assert audit(daemon)[-1]["result"] == "refused"
    assert daemon.limits.effective()["max_cores"] == 3.5


def test_owner_allowed_from_plain_session(daemon):
    assert owner_denial(daemon.fs, 501) is None
    r = call(daemon, "set", 501, owner=True, max_cores=2.0, reason="fan")
    assert r["ok"] and r["principal"] == "owner"


def test_owner_denied_for_unknown_caller(daemon):
    assert (
        call(daemon, "ceiling_set", None, owner=True, profile="silent", reason="x")["error"] == "owner_denied"
    )
    assert call(daemon, "set", 9999, owner=True, max_cores=2.0, reason="x")["error"] == "owner_denied"


def test_ceiling_ops_need_the_owner_claim(daemon):
    assert call(daemon, "ceiling_set", profile="silent", reason="x")["error"] == "owner_denied"
    assert call(daemon, "ceiling_clear")["error"] == "owner_denied"


def test_agent_raise_refused_under_ceiling_with_pid_and_cgroup_in_audit(daemon):
    assert call(daemon, "ceiling_set", owner=True, profile="silent", reason="TV playing")["ok"]
    r = call(daemon, "set", 600, max_cores=4.0, reason="need cores", project="proj-a")
    assert r == {
        "ok": False,
        "refused": True,
        "restrictedBy": "owner",
        "reason": "TV playing",
        "ceiling": "silent",
    }
    assert daemon.limits.effective()["max_cores"] == 2.0
    line = audit(daemon)[-1]
    assert (line["result"], line["pid"], line["cgroup"], line["principal"]) == (
        "refused",
        600,
        "/loop-a",
        "agent",
    )
    assert line["project"] == "proj-a" and line["reason"] == "need cores" and line["cmdline"] == "claude -p"
    assert line["request"]["max_cores"] == 4.0 and line["after"]["max_cores"] == 2.0


def test_agent_raise_accepted_without_restriction_then_resets(daemon):
    r = call(daemon, "set", 600, max_cores=4.0, reason="build")
    assert r["ok"] and r["ttl_s"] == 7200
    assert "MAX_CORES=4\n" in Path(daemon.env_file).read_text()
    daemon.clock.t += 7201
    daemon._expire()
    assert "MAX_CORES=3.5\n" in Path(daemon.env_file).read_text()
    assert [a["result"] for a in audit(daemon)] == ["accepted", "expired"]


def test_owner_ceiling_cancels_agent_raise_in_audit(daemon):
    call(daemon, "set", 600, max_cores=4.0, reason="build")
    call(daemon, "ceiling_set", owner=True, profile="silent", reason="movie")
    assert [a["result"] for a in audit(daemon)] == ["accepted", "cancelled", "accepted"]
    assert "OWNER_CEILING=silent\n" in Path(daemon.env_file).read_text()


def test_restart_keeps_profile_overrides_and_ceiling(daemon, tmp_path):
    call(daemon, "set", owner=True, profile="boost", reason="away")
    call(daemon, "ceiling_set", owner=True, profile="silent", reason="movie", ttl="1h")
    call(daemon, "set", 600, load_max=1.5, reason="quiet")
    again = Daemon(
        daemon.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"),
        env_file=None, clock=daemon.clock, config=parity_config(),
    )  # fmt: skip
    snap = again.limits.snapshot()
    assert snap["profile"] == "boost" and snap["ceiling"]["profile"] == "silent"
    assert [(o["param"], o["value"]) for o in snap["overrides"]] == [("load_max", 1.5)]


def test_invalid_args_and_missing_reason(daemon):
    assert call(daemon, "set", max_cores=4.0)["error"] == "invalid_args"
    assert call(daemon, "set", max_cores=1.2, reason="x")["error"] == "invalid_args"
    assert call(daemon, "set", reason="x")["error"] == "invalid_args"
    assert call(daemon, "set", max_cores=2.0, ttl="1h", until="07:00", reason="x")["error"] == "invalid_args"
    assert call(daemon, "set", max_cores=2.0, ttl="1d", reason="x")["error"] == "invalid_args"


def test_limits_log_and_status_ops(daemon):
    call(daemon, "set", 600, max_cores=2.0, reason="quiet")
    lim = call(daemon, "limits")["limits"]
    assert lim["effective"]["max_cores"] == 2.0 and lim["restriction"]["active"] is False
    assert [x["result"] for x in call(daemon, "log", n=5)["lines"]] == ["accepted"]
    assert call(daemon, "log", n=-1)["error"] == "invalid_args"


def test_status_carries_limits_and_validates(daemon, fake_host):
    host = fake_host("calm")
    sampler = Sampler(host.fs)
    sampler.prime(host.prev_stat)
    s = sampler.sample(now=T0)
    s["daemon"] = {
        "version": "t",
        "pid": 1,
        "started_at": "t",
        "mode": "observe",
        "enforce": {"governor": False, "slots": False, "memguard": False, "reaper": False},
        "tick_s": 5.0,
    }
    s["limits"] = daemon.limits_summary()
    s["rest"] = daemon.rest.status()
    s["slots"] = daemon.slots.summary()
    s["jobs"] = daemon.jobs.summary()
    s["alerts"], s["ha_push"] = daemon.watch.summary(), daemon.ha.status()
    s["governor"] = daemon.governor.status()
    s["memguard"], s["capabilities"] = daemon.memguard.status(), ["freeze", "reap"]
    s["headroom"] = daemon.headroom_status()
    s["backstop"] = daemon.backstop.status()
    assert validate_status(s) == []
    assert s["limits"]["effective"]["load_hold"] == 5.0


def test_loadctl_exit_codes_and_one_line_results(daemon, monkeypatch, capsys):
    def fake_request(_sock, op, args=None, **_k):
        return daemon.dispatch({"v": VERSION, "op": op, "args": args or {}}, 600)

    monkeypatch.setattr(loadctl, "request", fake_request)
    assert loadctl.main(["set", "--max-cores", "4", "--reason", "r"]) == 0
    assert "ok (agent)" in capsys.readouterr().out
    assert loadctl.main(["set", "--max-cores", "2", "--owner", "--reason", "r"]) == 4  # 600 is in /loop-a
    assert "owner denied" in capsys.readouterr().err
    assert loadctl.main(["set", "--max-cores", "1.2", "--reason", "r"]) == 5
    capsys.readouterr()
    daemon.limits.ceiling_set("silent", "TV playing")
    assert loadctl.main(["set", "--max-cores", "5", "--reason", "r"]) == 4
    assert capsys.readouterr().err.strip() == "refused: owner restriction active (silent ceiling: TV playing)"
    assert loadctl.main(["limits"]) == 0 and "restriction: ceiling" in capsys.readouterr().out
    assert loadctl.main(["limits", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"] == "balanced"
    assert loadctl.main(["log", "-n", "3"]) == 0
    assert loadctl.main(["ceiling", "set", "silent", "--reason", "x"]) == 4  # 600 may not be owner


def test_reset_is_owner_only_audited_and_wired_through_loadctl(daemon, monkeypatch, capsys):
    call(daemon, "set", owner=True, max_cores=4.0, reason="x")  # 4.0 > 3.5: no restriction
    call(daemon, "set", owner=True, max_cores=2.0, reason="quiet")
    assert call(daemon, "set", 600, max_cores=3.0, reason="x")["refused"]
    assert call(daemon, "reset", 600, params=["max_cores"], owner=True, reason="r")["error"] == "owner_denied"
    assert call(daemon, "reset", params=["max_cores"], reason="r")["error"] == "owner_denied"
    assert call(daemon, "reset", owner=True, reason="r")["error"] == "invalid_args"
    assert call(daemon, "reset", owner=True, params="max_cores", reason="r")["error"] == "bad_request"
    r = call(daemon, "reset", params=["max_cores"], owner=True, reason="done")
    assert r["ok"] and daemon.limits.restriction() is None
    assert audit(daemon)[-1]["op"] == "reset" and audit(daemon)[-1]["result"] == "accepted"
    assert call(daemon, "set", 600, max_cores=3.0, reason="x")["ttl_s"] == 7200

    monkeypatch.setattr(loadctl, "request", lambda _s, op, args=None, **_k: call(daemon, op, **(args or {})))
    assert loadctl.main(["reset", "--all", "--owner", "--reason", "r"]) == 0
    assert capsys.readouterr().out.startswith("reset: removed")
    assert loadctl.main(["reset", "max_cores", "--reason", "r"]) == 4


def test_ancestor_walk_fails_closed(daemon, proc):
    add_proc(proc, 800, "/user.slice/session-1.scope", "loadctl", 801)  # parent 801 does not exist
    assert "unreadable" in owner_denial(daemon.fs, 800)
    add_proc(proc, 810, "/user.slice/session-1.scope", "bash", 1)
    add_proc(proc, 811, "/user.slice/session-1.scope", "loadctl", 810)
    (proc / "811" / "status").write_text("Name:\tx\n")  # no PPid line
    assert "unreadable" in owner_denial(daemon.fs, 811)
    for i in range(70):  # a chain deeper than the walk limit
        add_proc(proc, 900 + i + 1, "/user.slice/session-1.scope", "bash", 900 + i)
    add_proc(proc, 900, "/user.slice/session-1.scope", "bash", 1)
    assert "deep" in owner_denial(daemon.fs, 970)


@pytest.mark.parametrize(
    "args",
    [
        {"profile": 5, "reason": "x"},
        {"profile": ["silent"], "reason": "x"},
        {"max_cores": "4", "reason": "x"},
        {"max_cores": True, "reason": "x"},
        {"max_cores": 2.0, "reason": 7},
        {"max_cores": 2.0, "reason": "x", "ttl": 60},
        {"max_cores": 2.0, "reason": "x", "owner": "yes"},
        {"max_cores": 2.0, "reason": "x", "project": {}},
    ],
)
def test_wrongly_typed_args_are_bad_request(daemon, args):
    assert call(daemon, "set", **args)["error"] == "bad_request"
    if "owner" not in args:
        assert call(daemon, "ceiling_set", **{**args, "owner": True})["error"] == "bad_request"
    assert daemon.limits.effective()["max_cores"] == 3.5


def test_non_object_args_are_bad_request(daemon):
    r = daemon.dispatch({"v": VERSION, "op": "set", "args": [1]}, 501)
    assert r["error"] == "bad_request"


def test_exact_expiry_applies_before_a_request_without_a_tick(daemon):
    assert call(daemon, "ceiling_set", owner=True, profile="silent", reason="movie", ttl="1s")["ok"]
    assert call(daemon, "set", 600, max_cores=4.0, reason="x")["refused"]
    daemon.clock.t += 2  # no tick ran
    assert call(daemon, "set", 600, max_cores=4.0, reason="x")["ok"]
    assert call(daemon, "limits")["limits"]["ceiling"] is None


def test_state_and_env_writes_fsync_file_and_directory(daemon, monkeypatch):
    synced = []
    monkeypatch.setattr("os.fsync", lambda fd: synced.append(fd))
    call(daemon, "set", 600, max_cores=2.0, reason="quiet")
    assert len(synced) >= 4  # state.json + dir, env + dir


def test_limits_summary_lists_the_overrides(daemon):
    assert daemon.limits_summary()["overrides"] == []
    call(daemon, "set", 501, owner=True, max_cores=1.0, reason="fan", ttl="15m")
    (o,) = daemon.limits_summary()["overrides"]
    assert (o["param"], o["value"], o["principal"], o["reason"]) == ("max_cores", 1.0, "owner", "fan")
    assert set(o) == {"param", "value", "principal", "reason", "set_at", "expires_at"}
    assert o["set_at"] and o["expires_at"]
