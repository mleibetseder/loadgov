"""Heavy/compile slot arbiter: queue, holder lifetime, legacy interop, enforce, restart, hold.

Real short-lived subprocesses stand in for holders and legacy bash holders; locks live in tmp dirs.
Only processes this test started are ever killed.
"""

import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest
from conftest import parity_config

from loadgov.cli import loadctl
from loadgov.cli.client import request
from loadgov.config import Config, ConfigError, parse_config
from loadgov.daemon.main import Daemon
from loadgov.engines.slots import SlotManager, pid_alive
from loadgov.host.procfs import HostFS
from loadgov.protocol import VERSION, validate_status

OWNER, AGENT_IN_LOOP = 501, 600


def add_proc(root, pid, cgroup, cmdline, ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


@pytest.fixture
def procs():
    started = []

    def spawn(*cmd):
        p = subprocess.Popen(cmd or ["sleep", "60"])
        started.append(p)
        return p

    yield spawn
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


def stop(p):
    p.kill()
    p.wait()


def mk_daemon(fake_host, tmp_path, enforce=False, name="calm", state="state", lock_root=None):
    host = fake_host(name)
    if lock_root:
        host.fs.lock_root = lock_root
    add_proc(__import__("pathlib").Path(host.proc), 1, "/init.scope", "init")
    add_proc(__import__("pathlib").Path(host.proc), OWNER, "/user.slice/s.scope", "loadctl", 1)
    add_proc(__import__("pathlib").Path(host.proc), AGENT_IN_LOOP, "/loop-a", "claude", 1)
    d = Daemon(
        host.fs, state_dir=str(tmp_path / state), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=parity_config(slots_enforce=enforce),
    )  # fmt: skip
    os.makedirs(d.run_dir, exist_ok=True)
    d.sampler.prime(host.prev_stat)
    return d, host


def call(d, op, peer=OWNER, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args}, peer)


def acq(d, pid, slot="heavy", label="gate", project="proj-a", wait=True, **kw):
    kw.setdefault("client_lock", d.slots.enforce)
    return call(d, "slot_acquire", slot=slot, project=project, label=label, pid=pid, wait=wait, **kw)


def audit(d):
    with open(d.audit.path) as f:
        return [json.loads(line) for line in f]


def holder_file(host, name="host-heavy-slot.holder"):
    with open(os.path.join(host.lock, name)) as f:
        return f.read()


def route(monkeypatch, d):
    def req(_s, op, args=None, **_k):
        if op == "slot_release" and args and "owner" not in args:
            # the CLI runs as a child of the holder's shell: peer 501 gets that shell (--pid) as its parent
            add_proc(
                __import__("pathlib").Path(d.fs.proc_root),
                OWNER,
                "/user.slice/s.scope",
                "loadctl",
                args["pid"],
            )
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, OWNER)

    monkeypatch.setattr(loadctl, "request", req)


def lock_free(path):
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def cli_acquire(slot, pid, fd, label, *extra):
    return loadctl.main(
        [
            "slot",
            "acquire",
            slot,
            "--project",
            "proj-a",
            "--label",
            label,
            "--pid",
            str(pid),
            "--fd",
            str(fd),
            *extra,
        ]
    )


def test_two_acquirers_serialize_the_client_holds_the_lock_and_the_holder_file_is_legacy_format(
    fake_host, tmp_path, procs, monkeypatch
):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    route(monkeypatch, d)
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    a, b = procs(), procs()
    fa, fb = os.open(lock, os.O_RDWR | os.O_APPEND), os.open(lock, os.O_RDWR | os.O_APPEND)
    assert cli_acquire("heavy", a.pid, fa, "one") == 0
    assert re.fullmatch(
        rf"proj-a one pid={a.pid} since=\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ\n", holder_file(host)
    )
    assert not lock_free(lock)  # held through the client's descriptor...
    assert d.slots.slots["heavy"].fd is None  # ...never the daemon's
    assert cli_acquire("heavy", b.pid, fb, "two") == 1  # busy, no --wait
    assert acq(d, b.pid, label="two")["status"] == "queued"
    assert loadctl.main(["slot", "release", "heavy", "--pid", str(a.pid), "--fd", str(fa)]) == 0
    assert lock_free(lock)  # release with --fd unlocks the shell's descriptor
    assert cli_acquire("heavy", b.pid, fb, "two") == 0
    assert holder_file(host).startswith(f"proj-a two pid={b.pid} since=")
    os.close(fa)
    os.close(fb)


def test_enforce_requires_an_fd_and_checks_it(fake_host, tmp_path, procs, monkeypatch, capsys):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    route(monkeypatch, d)
    a = procs()
    r = call(d, "slot_acquire", slot="heavy", project="p", label="l", pid=a.pid)
    assert r["error"] == "invalid_args" and "requires --fd N or loadctl run" in r["message"]
    other = os.open(str(tmp_path / "other"), os.O_RDWR | os.O_CREAT)
    assert cli_acquire("heavy", a.pid, other, "l") == loadctl.EXIT_ERROR  # fd is on another file
    assert "is not open on" in capsys.readouterr().err
    assert d.slots.summary()["heavy"]["state"] == "free"  # the grant was given back
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    legacy = os.open(lock, os.O_RDWR)
    mine = os.open(lock, os.O_RDWR)
    fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a legacy holder won the race
    assert cli_acquire("heavy", a.pid, mine, "l") == 1
    assert d.slots.summary()["heavy"]["holder"] is None and d.slots.summary()["heavy"]["source"] == "legacy"
    for fd in (other, legacy, mine):
        os.close(fd)


def test_client_lock_outlives_the_daemon(fake_host, tmp_path, procs):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    lock, sock = os.path.join(host.lock, "host-heavy-slot.lock"), str(tmp_path / "s.sock")
    ready, box = threading.Event(), {}

    def serve():
        async def main():
            srv = await asyncio.start_unix_server(d.handle, path=sock)
            box["loop"], box["stop"] = asyncio.get_running_loop(), asyncio.Event()
            ready.set()
            await box["stop"].wait()
            srv.close()

        asyncio.run(main())

    th = threading.Thread(target=serve)
    th.start()
    ready.wait(10)
    done = tmp_path / "done"
    script = (
        f"exec 9>>{lock}; {sys.executable} -m loadgov.cli.loadctl --socket {sock} slot acquire heavy "
        f"--project p --label l --fd 9 || exit 7; touch {done}; exec sleep 30"
    )
    bash = procs("bash", "-c", script)
    for _ in range(200):
        if done.exists() or bash.poll() is not None:
            break
        time.sleep(0.05)
    assert done.exists(), f"bash exited {bash.poll()}"
    assert not lock_free(lock)  # the bash process holds it; loadctl has long exited
    assert holder_file(host).startswith(f"p l pid={bash.pid} since=")
    box["loop"].call_soon_threadsafe(box["stop"].set)  # the daemon dies...
    th.join()
    d.slots = None  # ...with its SlotManager, without releasing anything
    assert not lock_free(lock)  # a legacy waiter's `flock -n` still fails while the gate runs
    stop(bash)
    assert lock_free(lock)


def test_busy_without_wait_and_idempotent_regrant(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    a, b = procs(), procs()
    assert acq(d, a.pid)["status"] == "granted"
    assert acq(d, a.pid)["status"] == "granted"
    r = acq(d, b.pid, wait=False)
    assert r["status"] == "busy" and f"pid={a.pid}" in r["holder"]
    assert call(d, "slot_status")["queues"]["heavy"] == []  # a busy caller does not stay queued


def test_holder_pid_exit_releases_within_one_tick_but_dropped_connection_does_not(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    a, b = procs(), procs()
    sock = str(tmp_path / "s.sock")

    async def go():
        server = await asyncio.start_unix_server(d.handle, path=sock)
        loop = asyncio.get_running_loop()
        args = {"slot": "heavy", "project": "proj-a", "label": "x", "pid": a.pid, "wait": True}
        r = await loop.run_in_executor(None, request, sock, "slot_acquire", args)
        server.close()  # the connection is gone; the holder pid is not
        return r

    assert asyncio.run(go())["status"] == "granted"
    d.tick()
    assert d.status["slots"]["heavy"]["state"] == "held"
    assert acq(d, b.pid, label="y")["status"] == "queued"
    stop(a)
    d.tick()
    assert d.status["slots"]["heavy"]["holder"].startswith(f"proj-a y pid={b.pid}")  # handed on in one tick
    assert any(e["reason"] == "released: holder exited" for e in audit(d))
    assert acq(d, b.pid, label="y")["status"] == "granted"


def legacy_holder(procs, lock, holder=None, sleep="30"):
    """The legacy bash protocol: exec 9>>lock; flock -n 9; write the holder line; keep the shell."""
    line = f'echo "legacy gate pid=$$ since=$(date -u +%FT%TZ)" > {holder};' if holder else ""
    open(lock, "a").close()
    p = procs("bash", "-c", f"exec 9>>{lock}; flock -n 9 || exit 1; {line} exec sleep {sleep}")
    for _ in range(100):  # wait until it has the lock
        probe = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return p
        finally:
            os.close(probe)
        time.sleep(0.05)
    raise AssertionError("legacy holder did not take the lock")


@pytest.mark.parametrize("with_holder_line", [True, False])
def test_legacy_holder_blocks_and_next_waiter_gets_the_slot(tmp_path, procs, with_holder_line):
    lock, holder = str(tmp_path / "host-heavy-slot.lock"), str(tmp_path / "host-heavy-slot.holder")
    fs = HostFS(proc_root="/proc", lock_root=str(tmp_path))
    m = SlotManager(fs, enforce=True)
    old = legacy_holder(procs, lock, holder if with_holder_line else None)
    a = procs()
    r = m.acquire("heavy", "proj-a", "gate", a.pid, True)
    assert r["status"] == "queued"
    s = m.summary()["heavy"]
    assert (s["state"], s["source"]) == ("held", "legacy")
    if with_holder_line:  # named through the holder line; otherwise only the failed flock shows it
        assert f"pid={old.pid}" in s["holder"] and f"pid={old.pid}" in r["holder"]
    assert m.acquire("heavy", "proj-a", "gate", a.pid, True)["status"] == "queued"
    stop(old)
    m.tick()
    assert m.acquire("heavy", "proj-a", "gate", a.pid, True)["status"] == "granted"
    assert m.summary()["heavy"]["source"] == "loadgov"
    m.release("heavy", a.pid)


def test_enforce_off_takes_no_lock_and_writes_no_holder_file(fake_host, tmp_path, procs):
    d, host = mk_daemon(fake_host, tmp_path, enforce=False)
    a = procs()
    assert acq(d, a.pid, slot="compile", label="rustc")["status"] == "granted"
    assert acq(d, a.pid)["status"] == "granted"
    assert not os.path.exists(os.path.join(host.lock, "host-compile-slot.lock"))  # never even created
    assert not os.path.exists(os.path.join(host.lock, "host-heavy-slot.holder"))
    assert not os.path.exists(os.path.join(host.lock, "host-compile-slot.holder"))
    fd = os.open(os.path.join(host.lock, "host-heavy-slot.lock"), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # nobody holds it
    os.close(fd)
    would = [e for e in audit(d) if e["op"] == "slot_lock"]
    assert len(would) == 2 and all(e["result"] == "would" and "WOULD flock" in e["reason"] for e in would)


def test_restart_readopts_live_holder_and_drops_dead_without_touching_the_lock(fake_host, tmp_path, procs):
    d1, host = mk_daemon(fake_host, tmp_path, enforce=True)
    live, dead = procs(), procs()
    assert acq(d1, live.pid, slot="heavy", label="live")["status"] == "granted"
    assert acq(d1, dead.pid, slot="compile", label="dead")["status"] == "granted"
    stop(dead)
    d2, _ = mk_daemon(fake_host, tmp_path, enforce=True, lock_root=host.lock)  # same state and lock dirs
    d2._after_change({})
    assert d2.slots.summary()["heavy"]["holder"].startswith(f"proj-a live pid={live.pid}")
    assert d2.slots.summary()["compile"]["state"] == "free"
    assert all(s.fd is None for s in d2.slots.slots.values())
    assert lock_free(os.path.join(host.lock, "host-heavy-slot.lock"))  # the client's lock is its own
    results = {(e["op"], e["result"]) for e in audit(d2)}
    assert ("slot_acquire", "adopted") in results and ("slot_release", "released") in results


def stat_text(pid, start):
    return f"{pid} (x) S " + "0 " * 18 + f"{start} 0\n"


def test_pid_reuse_is_a_dead_holder(tmp_path):
    table = {4242: 100}
    reader = lambda pid: stat_text(pid, table[pid]) if pid in table else None  # noqa: E731
    fs = HostFS(proc_root=str(tmp_path), lock_root=str(tmp_path))
    m = SlotManager(fs, stat_reader=reader)
    assert m.acquire("heavy", "proj-a", "gate", 4242, True)["status"] == "granted"
    saved = m.to_state()
    assert saved["heavy"]["holder"]["start"] == 100  # persisted
    m.tick()
    assert m.summary()["heavy"]["state"] == "held"
    table[4242] = 999  # the pid was reused by another process
    m.drain_events()
    m.tick()
    assert m.summary()["heavy"]["state"] == "free"
    assert any(e["reason"] == "released: holder exited" for e in m.drain_events())
    m2 = SlotManager(fs, stat_reader=reader)  # a restart with the saved start time 100 sees 999
    m2.restore(saved)
    assert m2.summary()["heavy"]["state"] == "free"
    table[4242] = 100
    m2.restore(saved)
    assert m2.summary()["heavy"]["state"] == "held"


def test_daemon_owned_hold_is_flocked_by_the_daemon_under_enforce(fake_host, tmp_path):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    call(d, "hold", state="on", slot="heavy", reason="update", owner=True)
    assert d.slots.summary()["heavy"]["state"] == "held-by-hold"
    assert not lock_free(lock)
    assert holder_file(host).startswith(f"home-assistant hold pid={os.getpid()} since=")
    call(d, "hold", state="off", slot="heavy", owner=True)
    assert lock_free(lock)


def test_hold_waits_for_holder_blocks_grants_sets_flag_and_hold_off_clears_both(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    a, b = procs(), procs()
    assert acq(d, a.pid)["status"] == "granted"
    r = call(d, "hold", state="on", slot="heavy", reason="drain for update", owner=True)
    assert (
        r["ok"]
        and r["slots"]["heavy"]["state"] == "held"
        and r["slots"]["heavy"]["hold"] == "drain for update"
    )
    assert d.slots.slots["heavy"].holder.pid == a.pid  # never preempts
    assert acq(d, b.pid, label="two")["status"] == "queued"
    refused = call(d, "set", max_cores=6.0, reason="more", project="proj-a")  # an agent loosening
    assert refused["refused"] is True and refused["reason"] == "drain for update"
    call(d, "slot_release", pid=a.pid, slot="heavy", owner=True)
    s = d.slots.summary()["heavy"]
    assert s["state"] == "held-by-hold" and s["holder"].startswith("home-assistant hold pid=")
    assert s["queue"] == 1 and acq(d, b.pid, label="two")["status"] == "queued"  # still blocked
    assert call(d, "slot_status")["slots"]["compile"]["state"] == "free"  # only heavy was held
    off = call(d, "hold", state="off", slot="heavy", owner=True)
    assert off["slots"]["heavy"]["hold"] is None
    assert acq(d, b.pid, label="two")["status"] == "granted"
    assert d.limits.snapshot()["hold"] is None and not d.limits.snapshot()["restriction"]["active"]
    assert call(d, "set", max_cores=6.0, reason="more", project="proj-a")["ok"]
    assert {"hold"} <= {e["op"] for e in audit(d)}


def test_hold_on_all_survives_restart_and_is_owner_only(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    for pid, extra in ((AGENT_IN_LOOP, {"owner": True}), (OWNER, {})):
        r = call(d, "hold", pid, state="on", reason="x", **extra)
        assert r["error"] == "owner_denied"
    assert audit(d)[-1]["result"] == "refused"
    r = call(d, "hold", state="on", reason="maintenance", owner=True)
    assert {s["state"] for s in r["slots"].values()} == {"held-by-hold"}
    d2, _ = mk_daemon(fake_host, tmp_path)  # restart with the same state dir
    d2._after_change({})
    assert d2.slots.summary()["compile"]["hold"] == "maintenance"
    assert d2.limits.snapshot()["hold"] == "maintenance"
    assert d2.slots.summary()["heavy"]["state"] == "held-by-hold"


def test_drain_profile_holds_heavy_slot_and_leaving_drain_releases_it(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    assert call(d, "set", profile="drain", owner=True, reason="night")["ok"]
    assert d.slots.summary()["heavy"]["state"] == "held-by-hold"
    assert d.slots.summary()["compile"]["state"] == "free"
    assert d.limits.snapshot()["hold"] is None  # the profile is the restriction, not the flag
    call(d, "hold", state="off", owner=True)  # an owner hold-off does not end the profile's hold
    assert d.slots.summary()["heavy"]["state"] == "held-by-hold"
    assert call(d, "set", profile="balanced", owner=True, reason="morning")["ok"]
    assert d.slots.summary()["heavy"]["state"] == "free"


def test_stuck_flag_after_45_minutes(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    t = [time.time()]
    d.slots.clock = d.clock = lambda: t[0]
    a = procs()
    acq(d, a.pid)
    assert d.slots.summary()["heavy"]["stuck"] is False
    t[0] += 46 * 60
    s = d.tick()["slots"]["heavy"]
    assert s["stuck"] is True and s["held_since"]


def test_stale_and_timed_out_waiters_are_dropped(fake_host, tmp_path, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    t = [time.time()]
    d.slots.clock = d.clock = lambda: t[0]
    a, b, c = procs(), procs(), procs()
    acq(d, a.pid)
    acq(d, b.pid, label="b")
    acq(d, c.pid, label="c", timeout_s=10)
    assert d.slots.summary()["heavy"]["queue"] == 2
    t[0] += 11
    assert acq(d, c.pid, label="c", timeout_s=10)["status"] == "timeout"
    t[0] += 61
    d.tick()
    assert d.slots.summary()["heavy"]["queue"] == 0
    stop(b)


@pytest.mark.parametrize(
    ("op", "args", "error"),
    [
        ("slot_acquire", {"slot": "light", "project": "p", "label": "l", "pid": 5}, "invalid_args"),
        ("slot_acquire", {"slot": 3, "project": "p", "label": "l", "pid": 5}, "bad_request"),
        ("slot_acquire", {"slot": "heavy", "project": "p", "label": "l"}, "invalid_args"),
        ("slot_acquire", {"slot": "heavy", "project": "p", "label": "l", "pid": "5"}, "bad_request"),
        ("slot_acquire", {"slot": "heavy", "project": "p", "label": "l", "pid": True}, "bad_request"),
        ("slot_acquire", {"slot": "heavy", "project": "p\nq", "label": "l", "pid": 5}, "invalid_args"),
        ("slot_acquire", {"slot": "heavy", "project": 1, "label": "l", "pid": 5}, "bad_request"),
        (
            "slot_acquire",
            {"slot": "heavy", "project": "p", "label": "l", "pid": 5, "wait": "y"},
            "bad_request",
        ),
        (
            "slot_acquire",
            {"slot": "heavy", "project": "p", "label": "l", "pid": 5, "timeout_s": 0},
            "invalid_args",
        ),
        (
            "slot_acquire",
            {"slot": "heavy", "project": "p", "label": "l", "pid": 2**22 + 12345},
            "invalid_args",
        ),
        ("slot_release", {"slot": "heavy"}, "invalid_args"),
        ("hold", {"state": "maybe", "owner": True}, "invalid_args"),
        ("hold", {"state": "on", "owner": True}, "invalid_args"),  # reason required
        ("hold", {"state": "on", "owner": "yes", "reason": "x"}, "bad_request"),
        ("hold", {"state": "on", "slot": "all2", "owner": True, "reason": "x"}, "invalid_args"),
    ],
)
def test_bad_arguments(fake_host, tmp_path, op, args, error):
    d, _ = mk_daemon(fake_host, tmp_path)
    assert call(d, op, **args)["error"] == error
    assert not pid_alive(2**22 + 12345)


def test_status_has_valid_slots_section(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    s = d.tick()
    assert validate_status(s) == []
    assert s["slots"]["heavy"] == {
        "state": "free", "holder": None, "source": None, "queue": 0, "held_since": None, "stuck": False,
        "hold": None, "hold_origin": None,
    }  # fmt: skip
    assert call(d, "status")["status"]["slots"]["compile"]["state"] == "free"


def test_loadctl_slot_acquire_prints_legacy_strings_and_polls(
    fake_host, tmp_path, procs, monkeypatch, capsys
):
    d, _ = mk_daemon(fake_host, tmp_path)
    a, b = procs(), procs()
    acq(d, a.pid, label="first")
    route(monkeypatch, d)
    base = [
        "slot",
        "acquire",
        "heavy",
        "--project",
        "proj-a",
        "--label",
        "second",
        "--pid",
        str(b.pid),
        "--no-lock",
    ]
    assert loadctl.main(base) == 1  # busy without --wait
    err = capsys.readouterr().err
    assert re.fullmatch(
        rf"HEAVY_SLOT_WAIT proj-a second: held by proj-a first pid={a.pid} since=\S+ waited=0s\n", err
    )
    sleeps = []

    def fake_sleep(n):
        sleeps.append(n)
        call(d, "slot_release", pid=a.pid, slot="heavy", owner=True)

    monkeypatch.setattr(loadctl.time, "sleep", fake_sleep)
    assert loadctl.main([*base, "--wait"]) == 0
    cap = capsys.readouterr()
    assert (
        cap.out == "HEAVY_SLOT_OK proj-a second waited=0s (no lock: arbitration only)\n"
        and cap.err.startswith("HEAVY_SLOT_WAIT proj-a second")
    )
    assert sleeps == [loadctl.SLOT_POLL_S]
    assert loadctl.main(["slot", "status"]) == 0
    assert "heavy: held by proj-a second" in capsys.readouterr().out
    assert loadctl.main(["slot", "release", "heavy", "--pid", str(b.pid)]) == 0
    compile_args = [
        "slot",
        "acquire",
        "compile",
        "--project",
        "p",
        "--label",
        "rustc",
        "--pid",
        str(b.pid),
        "--no-lock",
    ]
    assert loadctl.main(compile_args) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("COMPILE_SLOT_OK p rustc")


def test_loadctl_slot_acquire_timeout_leaves_the_queue(fake_host, tmp_path, procs, monkeypatch, capsys):
    d, _ = mk_daemon(fake_host, tmp_path)
    a, b = procs(), procs()
    acq(d, a.pid)
    route(monkeypatch, d)
    monkeypatch.setattr(loadctl.time, "sleep", lambda _n: None)
    t = iter(range(0, 1000, 400))
    monkeypatch.setattr(loadctl.time, "monotonic", lambda: next(t))
    rc = loadctl.main(["slot", "acquire", "heavy", "--project", "p", "--label", "l", "--pid", str(b.pid),
                       "--no-lock", "--wait", "--timeout", "300"])  # fmt: skip
    assert rc == 1 and d.slots.summary()["heavy"]["queue"] == 0


def test_loadctl_hold_exit_codes(fake_host, tmp_path, monkeypatch, capsys):
    d, _ = mk_daemon(fake_host, tmp_path)
    who = [AGENT_IN_LOOP]
    monkeypatch.setattr(loadctl, "request", lambda _s, op, args=None, **_k: d.dispatch(
        {"v": VERSION, "op": op, "args": args or {}}, who[0]))  # fmt: skip
    assert loadctl.main(["hold", "on", "--owner", "--reason", "x"]) == 4
    who[0] = OWNER
    assert loadctl.main(["hold", "on", "--owner", "--reason", "x", "--slot", "heavy"]) == 0
    assert "heavy: held-by-hold" in capsys.readouterr().out
    assert loadctl.main(["hold", "off", "--owner"]) == 0


def test_config_slots_enforce():
    assert Config().slots_enforce is False
    assert parse_config("[slots]\nenforce = true\n").slots_enforce is True
    for bad in ("[slots]\nenforce = 1\n", "[slots]\nother = true\n"):
        with pytest.raises(ConfigError):
            parse_config(bad)


def test_slot_acquire_cli_refuses_without_fd_or_no_lock(fake_host, tmp_path, monkeypatch, capsys, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    route(monkeypatch, d)
    p = procs()
    base = ["slot", "acquire", "heavy", "--project", "b", "--label", "g", "--wait", "--pid", str(p.pid)]
    assert loadctl.main(base) == 2
    err = capsys.readouterr().err
    assert "use --fd N (see compat/heavy-slot.sh) or --no-lock for arbitration-only testing" in err
    assert d.slots.summary()["heavy"]["state"] == "free"  # nothing was granted


def test_slot_acquire_no_lock_says_so_on_the_ok_line(fake_host, tmp_path, monkeypatch, capsys, procs):
    d, _ = mk_daemon(fake_host, tmp_path)
    route(monkeypatch, d)
    p = procs()
    rc = loadctl.main(
        ["slot", "acquire", "heavy", "--project", "b", "--label", "g", "--pid", str(p.pid), "--no-lock"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "HEAVY_SLOT_OK b g waited=0s (no lock: arbitration only)"


def test_release_is_for_the_holder_its_descendants_or_an_owner(fake_host, tmp_path, procs):
    d, host = mk_daemon(fake_host, tmp_path)
    root = __import__("pathlib").Path(host.proc)
    a = procs()
    assert acq(d, a.pid)["status"] == "granted"
    other = 700
    add_proc(root, other, "/user.slice/x.scope", "stranger", 1)
    denied = call(d, "slot_release", peer=other, pid=a.pid, slot="heavy")
    assert denied["error"] == "owner_denied" and "only the holder" in denied["message"]
    assert d.slots.slots["heavy"].holder.pid == a.pid
    assert audit(d)[-1]["result"] == "refused" and audit(d)[-1]["op"] == "slot_release"
    # an owner claim alone is not enough from inside a governed cgroup
    add_proc(root, AGENT_IN_LOOP, "/loop-a", "claude", 1)
    assert (
        call(d, "slot_release", peer=AGENT_IN_LOOP, pid=a.pid, slot="heavy", owner=True)["error"]
        == "owner_denied"
    )
    assert (
        call(d, "slot_release", peer=other, pid=a.pid, slot="heavy", owner="yes")["error"] == "owner_denied"
    )
    # the holder's own process, and a loadctl child of the holder's shell, may release
    add_proc(root, 701, "/user.slice/x.scope", "loadctl", a.pid)
    assert call(d, "slot_release", peer=701, pid=a.pid, slot="heavy")["released"] is True
    assert acq(d, a.pid)["status"] == "granted"
    assert call(d, "slot_release", peer=a.pid, pid=a.pid, slot="heavy")["released"] is True
    # a pid that holds nothing is a harmless no-op for anyone
    assert call(d, "slot_release", peer=other, pid=a.pid, slot="heavy")["released"] is False
    # an owner may release another's slot; a stranger may not drop someone from the queue either
    b = procs()
    assert acq(d, a.pid)["status"] == "granted" and acq(d, b.pid, wait=True)["status"] == "queued"
    assert call(d, "slot_release", peer=other, pid=b.pid, slot="heavy")["error"] == "owner_denied"
    assert d.slots.summary()["heavy"]["queue"] == 1
    assert call(d, "slot_release", pid=a.pid, slot="heavy", owner=True)["released"] is True
