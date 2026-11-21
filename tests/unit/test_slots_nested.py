"""Nested (re-entrant) slot grants: a request from a descendant of the holder is granted at once.

Real short-lived processes give a real /proc ancestry (a holder shell and its child); the cgroup route
uses a fake /proc tree. Only processes this test started are ever killed.
"""

import os
import subprocess
import time

import pytest
from test_slots import acq, add_proc, audit, call, cli_acquire, lock_free, mk_daemon, procs, route, stop  # noqa: F401

from loadgov.cli import loadctl
from loadgov.engines.slots import SlotManager
from loadgov.host.procfs import HostFS


@pytest.fixture
def tree(procs, request):  # noqa: F811
    """() -> (holder shell, pid of its real child): the child descends from the holder through /proc."""

    def make():
        sh = procs("sh", "-c", "sleep 60 & sleep 60 & wait")
        kids = f"/proc/{sh.pid}/task/{sh.pid}/children"
        kid = []
        for _ in range(100):
            kid = open(kids).read().split()
            if len(kid) == 2:
                break
            time.sleep(0.05)
        for k in kid:
            request.addfinalizer(lambda k=int(k): os.path.exists(f"/proc/{k}") and os.kill(k, 9))
        child = int(kid[0])
        return sh, child

    return make


def nested_rows(d):
    return [e for e in audit(d) if e.get("result") == "nested"]


def test_descendant_is_granted_nested_without_a_lock_and_release_keeps_the_slot(fake_host, tmp_path, tree):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    outer, inner = tree()
    assert acq(d, outer.pid, slot="compile", label="build")["status"] == "granted"
    r = acq(d, inner, slot="compile", label="rustc", wait=False)
    assert r["status"] == "granted" and r["nested"] is True
    assert not {"lock_path", "holder_path", "holder_line"} & set(r)  # no new flock, no holder line
    again = acq(d, inner, slot="compile", label="rustc", wait=False)  # a repeat is the same grant
    assert again["nested"] is True
    assert d.slots.summary()["compile"]["nested"] == 1
    assert call(d, "slot_status")["slots"]["compile"]["nested"] == 1
    rows = nested_rows(d)
    assert len(rows) == 1  # audited once
    assert rows[0]["op"] == "slot_acquire" and f"pid={outer.pid}" in rows[0]["reason"]
    assert "proj-a build" in rows[0]["reason"]
    # releasing the nested grant never releases the real slot
    out = call(d, "slot_release", slot="compile", pid=inner)
    assert out["released"] is True and out["nested"] is True
    s = d.slots.summary()["compile"]
    assert s["nested"] == 0 and s["state"] == "held" and f"pid={outer.pid}" in s["holder"]
    assert call(d, "slot_release", slot="compile", pid=inner)["released"] is False  # not released twice
    assert d.slots.slots["compile"].holder.pid == outer.pid


def test_outer_holder_ending_frees_the_slot_and_drops_the_nested_bookkeeping(fake_host, tmp_path, tree):
    d, _ = mk_daemon(fake_host, tmp_path)
    outer, inner = tree()
    acq(d, outer.pid, slot="compile")
    assert acq(d, inner, slot="compile", label="rustc")["nested"] is True
    stop(outer)  # the holder is gone while the nested job still runs
    d.tick()
    s = d.slots.summary()["compile"]
    assert s["state"] == "free" and s["nested"] == 0
    assert call(d, "slot_release", slot="compile", pid=inner)["released"] is False  # no double release
    ends = [e for e in audit(d) if e["op"] == "slot_release"]
    assert [e["reason"] for e in ends] == ["released: holder exited"]


def test_a_nested_grant_whose_process_is_gone_is_dropped_by_the_tick(fake_host, tmp_path, tree):
    d, _ = mk_daemon(fake_host, tmp_path)
    outer, inner = tree()
    acq(d, outer.pid, slot="compile")
    acq(d, inner, slot="compile", label="rustc")
    os.kill(inner, 9)
    for _ in range(100):  # reaped by the shell
        if not os.path.exists(f"/proc/{inner}"):
            break
        time.sleep(0.05)
    d.tick()
    assert d.slots.summary()["compile"]["nested"] == 0
    assert d.slots.slots["compile"].holder.pid == outer.pid


def test_a_different_slot_inside_still_waits(fake_host, tmp_path, tree, procs):  # noqa: F811
    d, _ = mk_daemon(fake_host, tmp_path)
    outer, inner = tree()
    other = procs()
    assert acq(d, other.pid, slot="heavy")["status"] == "granted"
    acq(d, outer.pid, slot="compile")
    r = acq(d, inner, slot="heavy", label="gate", wait=False)
    assert r["status"] == "busy" and "nested" not in r
    assert d.slots.summary()["heavy"]["nested"] == 0 and nested_rows(d) == []


def test_an_owner_hold_is_not_a_holder_to_nest_under(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    assert call(d, "hold", state="on", slot="heavy", reason="update", owner=True)["ok"]
    h = d.slots.slots["heavy"].holder
    assert h.kind == "hold" and h.pid == os.getpid()  # the hold's pid is the daemon: this very process
    sub = subprocess.Popen(["sleep", "60"])  # a real descendant of the hold's pid
    try:
        r = acq(d, sub.pid, slot="heavy", label="x", wait=False)
        assert r["status"] == "busy" and "nested" not in r
    finally:
        sub.kill()
        sub.wait()
    assert d.slots.summary()["heavy"]["nested"] == 0


def test_pid_reuse_is_not_nested(fake_host, tmp_path, tree):
    d, _ = mk_daemon(fake_host, tmp_path)
    outer, inner = tree()
    acq(d, outer.pid, slot="compile")
    d.slots.slots["compile"].holder.start += 1  # the recorded start time belongs to another process
    r = acq(d, inner, slot="compile", label="rustc", wait=False)
    assert r["status"] == "busy" and "nested" not in r
    assert d.slots.summary()["compile"]["nested"] == 0


def sline(pid, ppid, start):
    return f"{pid} (x) S {ppid} " + "0 " * 17 + f"{start} 0\n"


def test_a_process_in_the_holders_job_scope_is_nested_by_cgroup(tmp_path):
    cg = "/build.slice/build-work.slice/build-work-proj-a.slice/loadgov-compile-x-1.scope"
    table = {100: (1, 10), 200: (1, 20), 300: (1, 30), 400: (1, 40)}
    reader = lambda pid: sline(pid, *table[pid]) if pid in table else None  # noqa: E731
    fs = HostFS(proc_root=str(tmp_path), lock_root=str(tmp_path))
    add_proc(tmp_path, 300, cg + "/sub", "x")
    add_proc(tmp_path, 400, "/user.slice/other.scope", "x")
    m = SlotManager(fs, stat_reader=reader)
    assert m.acquire("compile", "proj-a", "build", 100, True)["status"] == "granted"
    assert m.bind_scope("compile", 100, "loadgov-compile-x-1.scope", cg, 200)
    assert m.acquire("compile", "proj-a", "c", 300, False)["nested"] is True  # a process in the scope
    assert m.acquire("compile", "proj-a", "c", 200, False)["nested"] is True  # the job pid itself
    r = m.acquire("compile", "proj-a", "c", 400, False)  # outside the scope, unrelated
    assert r["status"] == "busy" and "nested" not in r
    ev = [e for e in m.drain_events() if e["result"] == "nested"]
    assert (
        len(ev) == 2 and "loadgov-compile-x-1.scope" in ev[0]["reason"] and "job pid 200" in ev[1]["reason"]
    )
    assert m.summary()["compile"]["nested"] == 2


def test_nested_release_with_fd_never_unlocks_the_holders_descriptor(
    fake_host, tmp_path, tree, monkeypatch, capsys
):
    d, host = mk_daemon(fake_host, tmp_path, enforce=True)
    route(monkeypatch, d)
    outer, inner = tree()
    lock = os.path.join(host.lock, "host-heavy-slot.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    try:
        assert cli_acquire("heavy", outer.pid, fd, "gate") == 0  # the outer holder takes the real lock on fd
        assert not lock_free(lock)
        assert cli_acquire("heavy", inner, fd, "inner") == 0  # the inherited fd: same open file description
        assert "nested under" in capsys.readouterr().out
        assert loadctl.main(["slot", "release", "heavy", "--pid", str(inner), "--fd", str(fd)]) == 0
        assert not lock_free(lock)  # the nested release left the real lock alone
        assert d.slots.summary()["heavy"]["nested"] == 0
    finally:
        os.close(fd)


def test_the_holder_asking_twice_releases_nested_grants_first(fake_host, tmp_path, tree):
    d, _ = mk_daemon(fake_host, tmp_path)
    outer, _inner = tree()
    assert acq(d, outer.pid, slot="compile", label="one")["status"] == "granted"
    assert acq(d, outer.pid, slot="compile", label="two")["nested"] is True  # same shell, second request
    add_proc(__import__("pathlib").Path(d.fs.proc_root), 501, "/user.slice/s.scope", "loadctl", outer.pid)
    first = call(d, "slot_release", slot="compile", pid=outer.pid)
    assert first["released"] is True and first["nested"] is True
    assert d.slots.slots["compile"].holder.pid == outer.pid  # the real slot is still held
    second = call(d, "slot_release", slot="compile", pid=outer.pid)
    assert second["released"] is True and "nested" not in second
    assert d.slots.slots["compile"].holder is None
