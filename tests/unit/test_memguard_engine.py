"""Memory guard: engine decisions, the daemon runtime on a fake host, crash safety, priority.

Every signal, freeze and docker call goes to `Kernel` in memguard_rig (a recorder over a fixture /proc and
cgroup tree); the only real processes signalled are the ones a test starts itself (test_signals_*).
"""

import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import time

import pytest
from conftest import parity_config
from memguard_rig import AGENT, OWNER, Rig

from loadgov.config import ConfigError, GuardProject, parse_config
from loadgov.daemon import recover
from loadgov.engines import jvm
from loadgov.engines.memguard import MemGuard, Tree, thresholds
from loadgov.host import cgroup
from loadgov.host.docker import Docker
from loadgov.host.procfs import HostFS
from loadgov.host.signals import Signals

FLOORS = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]


@pytest.fixture
def rig(tmp_path, fake_host):
    return Rig(tmp_path, fake_host)


# ---- thresholds and the engine (pure) ---------------------------------------------------------------


@pytest.mark.parametrize("floor", FLOORS)
def test_resume_is_max_6gb_floor_plus_2gb_and_above_the_floor(floor):
    low, high = thresholds(floor)
    assert low == round(floor * 1024)
    assert high == max(6144, low + 2048)
    assert low < high


def test_out_of_range_floors_are_clamped_not_fatal():
    assert thresholds(1.0) == thresholds(2.0)
    assert thresholds(8.0) == thresholds(6.0)


def make_engine(projects=None, override=None):
    t = [0.0]

    def clock():
        return t[0]

    return MemGuard(clock, clock, projects or {}, override), t


def a_tree(key="legacy:1", rss=900, **kw):
    return Tree("legacy", key, "sigstop", f"implementer {key}", "proj-a", rss, [{"pid": 2, "start": 1}], **kw)


@pytest.mark.parametrize("floor", FLOORS)
def test_no_second_pause_within_five_minutes_after_a_resume_for_every_floor(floor):
    """On the low-mem fixture (swap 3000 MB): the resume level is always above the pause level."""
    g, t = make_engine()
    low, high = thresholds(floor)
    tree = a_tree()

    def step(avail):
        t[0] += 5
        return g.step(avail, 3000, floor, True, lambda: [tree], lambda: [])

    assert step(high) == []  # calm baseline
    assert [a.op for a in step(low - 100)] == ["pause"]
    ops = []
    for _ in range(12):  # 15 s quiet, then 3 samples at the resume level
        ops += [a.op for a in step(high)]
    assert ops == ["resume"]
    t0 = t[0]
    wobble = itertools.cycle([high, low, high - 1, low])  # the lowest values that are not pressure
    while t[0] - t0 < 300:
        assert step(next(wobble)) == []


def test_pauses_only_one_tree_per_decision_and_resumes_lifo():
    g, t = make_engine()
    trees = [a_tree("legacy:1", 900), a_tree("legacy:2", 1500)]

    def step(avail):
        t[0] += 5
        return [
            (a.op, a.entry.key if a.entry else None)
            for a in g.step(avail, 0, 3.0, True, lambda: trees, lambda: [])
        ]

    assert step(2000) == [("pause", "legacy:2")]  # the largest
    assert step(2000) == []  # 15 s quiet
    assert step(2000) == []
    assert step(2000) == [("pause", "legacy:1")]  # the next one, after the quiet period
    out = []
    for _ in range(30):
        out += step(7000)
    assert out == [("resume", "legacy:1"), ("resume", "legacy:2")]  # newest pause first


def test_class_order_scopes_before_legacy_before_containers():
    g, t = make_engine()
    scope = Tree("scope", "s", "freeze", "scope s", "test", 200, [{"cgroup": "/x"}])
    cont = Tree("container", "gates-1", "docker", "container gates-1", None, 0, [{"name": "gates-1"}])
    leg = a_tree(rss=5000)
    t[0] += 5
    acts = g.step(2000, 0, 3.0, True, lambda: [cont, leg, scope], lambda: [])
    assert acts[0].entry.key == "s"


def test_a_compile_tree_is_skipped_hold_then_abort_below_crit():
    g, t = make_engine()
    comp = a_tree(big_rustc_mb=2500, rustc=(77, 5))

    def step(avail):
        t[0] += 5
        return g.step(avail, 0, 3.0, True, lambda: [comp], lambda: [])

    assert step(2000) == []
    assert [e["kind"] for e in g.drain_events()] == ["HOLD"]
    acts = step(1400)
    assert [a.op for a in acts] == ["abort"] and acts[0].tree.rustc == (77, 5)
    assert g.paused == []  # nothing was paused: a stopped rustc keeps its memory


def test_deny_list_and_unregistered_classes_are_never_candidates():
    g, t = make_engine()
    g.deny = (*g.deny, "proj-a-test-*")  # a site's own extra glob
    deny = [Tree("container", n, "docker", n, None, 0, [{"name": n}]) for n in
            ("a-test-db-1", "x-review-y", "proj-a-test-q")]  # fmt: skip
    t[0] += 5
    assert g.step(2000, 0, 3.0, True, lambda: deny, lambda: []) == []


# ---- the daemon on a fake host ----------------------------------------------------------------------


def test_low_mem_pauses_the_larger_non_compiling_tree_and_never_claude(rig):
    a = rig.impl_tree(300, rss_work=800)
    b = rig.impl_tree(400, rss_work=1500)
    rig.step(avail=2000)
    stopped = {p for p, _ in rig.kernel.signals("SIGSTOP")}
    assert stopped == {b["work"], b["child"]}
    assert not rig.kernel.signals(pid=a["work"]) and not rig.kernel.signals(pid=a["claude"])
    assert all(p not in (a["claude"], b["claude"], a["shell"], b["shell"]) for p, _ in rig.kernel.sent)
    s = rig.d.status["memguard"]
    assert [p["how"] for p in s["paused"]] == ["sigstop"] and s["last_decision"] == "PAUSE"
    assert [e["principal"] for e in rig.events("PAUSE")] == ["system"]


def test_compile_only_holds_then_aborts_the_largest_rustc_only(rig):
    t = rig.impl_tree(300, rustc_mb=2600)
    rig.step(avail=2000)
    assert rig.kernel.sent == []
    assert [e["request"]["event"] for e in rig.events()] == ["HOLD"]
    rig.step(avail=1400)
    assert rig.kernel.signals() == [(t["work"], "SIGTERM")]  # the rustc; not claude, not the shell
    ev = rig.events("ABORT")
    assert len(ev) == 1 and "below 1500MB" in ev[0]["reason"]
    assert [e["key"] for e in rig.d.watch.summary()["events"]] == ["abort"]


def test_resume_is_lifo_after_three_samples_at_the_resume_level(rig):
    a = rig.impl_tree(300, rss_work=1000)
    b = rig.impl_tree(400, rss_work=1500)
    rig.step(avail=2000)  # pauses b
    for _ in range(3):  # 15 s quiet
        rig.step(avail=2000)
    rig.step(avail=2000)  # pauses a
    assert {rig.state(a["work"]), rig.state(b["work"])} == {"T"}
    for _ in range(3):
        rig.step(avail=7000)
    assert rig.state(a["work"]) == "T"  # still in the 15 s quiet
    ticks = 0
    while rig.state(a["work"]) == "T":
        rig.step(avail=7000)
        ticks += 1
        assert ticks < 10
    assert rig.state(b["work"]) == "T"  # newest pause (a) resumed first; 30 s quiet before the next
    for _ in range(12):
        rig.step(avail=7000)
    assert rig.state(b["work"]) == "S"
    conts = [p for p, s in rig.kernel.sent if s == "SIGCONT"]
    assert conts.index(a["work"]) < conts.index(b["work"])
    assert rig.d.status["memguard"]["paused"] == []


def test_impl_scope_stops_only_the_tree_minus_claude_and_keeps_its_socket(rig):
    a, b = socket.socketpair()  # claude's open connection (fake)
    try:
        rig.add(499, "bash", 1, 5)
        rig.add(500, "claude", 499, 600)
        rig.add(501 + 1000, "cargo", 500, 900)
        rig.add(1502, "rustc", 1501, 300)
        rig.scope("loadgov-impl-x.scope", "impl", [499, 500, 1501, 1502])
        rig.step(avail=2000)
        assert sorted(p for p, _ in rig.kernel.signals("SIGSTOP")) == [1501, 1502]
        assert rig.kernel.signals(pid=500) == [] and rig.kernel.signals(pid=499) == []
        assert rig.freeze_value("loadgov-impl-x.scope") == "0"  # an impl scope is never frozen
        assert rig.state(500) == "S"
        a.sendall(b"still open")
        assert b.recv(16) == b"still open"
    finally:
        a.close()
        b.close()


def test_agent_free_gate_scope_is_frozen_and_thawed_by_the_rule(rig):
    rig.add(700, "cargo", 1, 900)
    rig.add(701, "rustc", 700, 300)
    rig.scope("loadgov-gate-x.scope", "gate", [700, 701], mem_mb=1200)
    rig.step(avail=2000)
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"
    assert rig.kernel.sent == []  # frozen as a cgroup, nobody was signalled
    p = rig.d.status["memguard"]["paused"][0]
    assert (p["how"], p["what"]) == ("freeze", "scope loadgov-gate-x.scope")
    for _ in range(30):
        rig.step(avail=7000)
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"


def test_a_gate_scope_with_an_agent_inside_is_never_frozen(rig):
    rig.add(700, "claude", 1, 900)
    rig.add(701, "cargo", 700, 300)
    rig.scope("loadgov-gate-x.scope", "gate", [700, 701])
    rig.step(avail=2000)
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"
    assert [p for p, _ in rig.kernel.signals("SIGSTOP")] == [701]


def test_observe_mode_calls_nothing_and_writes_would_events(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False, reaper=False)
    r.kernel.containers = ["gates-1"]
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    r.add(800, "java", 1, 900, cmd="/usr/bin/java org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.5")
    t = r.impl_tree(300, rustc_mb=2600)
    r.step(avail=2900)  # the daemon outside every scope goes first
    r.step(avail=2900)  # then the scope
    r.step(avail=1400)  # the abort, once only the compile is left
    for _ in range(40):
        r.step(avail=7000)
    assert r.kernel.sent == [] and r.kernel.mutating_docker() == []
    assert r.freeze_value("loadgov-gate-x.scope") == "0"
    text = [e["reason"] for e in r.events()]
    assert all(x.startswith("WOULD ") for x in text if not x.startswith("HOLD"))
    kinds = {e["request"]["event"] for e in r.events()}
    assert {"REAP", "PAUSE", "RESUME"} <= kinds
    assert not r.d.watch.summary()["events"]  # nothing is pushed to the phone in the shadow
    assert t["work"] and all(e["result"] == "would" for e in r.events("PAUSE"))


def test_observe_mode_abort_is_a_would(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False)
    r.impl_tree(300, rustc_mb=2600)
    r.step(avail=1400)
    assert r.events("ABORT") and r.events("ABORT")[0]["reason"].startswith("WOULD ABORT")
    assert r.kernel.sent == []


def test_a_signal_wrapper_without_enforce_never_calls_the_pidfd_layer(rig, tmp_path):
    calls = []
    s = Signals(rig.host.fs, False, opener=lambda p: calls.append(p) or p)
    assert s.send(300, None, signal.SIGSTOP) == "would" and calls == []
    f = rig.cg / "x" / "cgroup.freeze"
    f.parent.mkdir()
    f.write_text("0\n")
    assert cgroup.write_freeze(rig.host.fs, "/x", True, False) == "would" and f.read_text() == "0\n"
    assert cgroup.write_freeze(rig.host.fs, "/x", True, True) == "ok" and f.read_text() == "1\n"
    assert (
        cgroup.write_freeze(rig.host.fs, "/x", False, False) == "ok" and f.read_text() == "0\n"
    )  # thaw: always
    docker = Docker(lambda argv: calls.append(argv) or (0, ""), False)
    assert docker.pause("gates-1") == "would" and calls == []


def test_signals_refuse_claude_pid1_changed_start_and_gone(rig):
    rig.add(300, "claude", 1)
    rig.add(301, "node", 1)
    s = Signals(
        rig.host.fs, True, opener=rig.kernel.opener, sender=rig.kernel.sender, closer=rig.kernel.closer
    )
    sig = signal
    assert s.send(300, None, sig.SIGSTOP) == "refused" and s.send(300, None, sig.SIGTERM) == "refused"
    assert s.send(300, None, sig.SIGCONT) == "sent"  # the undo is always fine
    assert s.send(1, None, sig.SIGSTOP) == "refused"
    assert s.send(301, 1, sig.SIGSTOP) == "changed"
    assert s.send(999, None, sig.SIGSTOP) == "gone"
    assert s.send(301, rig.specs[301].start, sig.SIGSTOP) == "sent"


def test_signals_work_on_a_real_process_the_test_started():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        fs = HostFS()
        s = Signals(fs, True)
        start = s.stat(p.pid)[2]
        sig = signal
        assert s.send(p.pid, start + 1, sig.SIGSTOP) == "changed"
        assert s.send(p.pid, start, sig.SIGSTOP) == "sent"
        for _ in range(100):  # the stop takes effect when the child is next scheduled
            if s.stat(p.pid)[1] in ("T", "t"):
                break
            time.sleep(0.02)
        assert s.stat(p.pid)[1] in ("T", "t")
        assert Signals(fs, False).send(p.pid, start, sig.SIGCONT) == "sent"  # SIGCONT needs no enforce
        assert s.send(p.pid, start, sig.SIGTERM) == "sent"
        p.wait(timeout=10)
        assert s.send(p.pid, start, sig.SIGTERM) == "gone"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# ---- crash safety --------------------------------------------------------------------


def test_kill_9_leaves_work_paused_restart_adopts_and_resumes_only_by_the_rule(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    r.step(avail=2000)
    assert r.freeze_value("loadgov-gate-x.scope") == "1"
    saved = json.load(open(tmp_path / "state" / "state.json"))["memguard"]["paused"]
    assert [e["how"] for e in saved] == ["freeze"] and saved[0]["principal"] == "system"
    # the old daemon object is simply dropped: nothing thaws on exit
    assert r.freeze_value("loadgov-gate-x.scope") == "1"
    d2 = r.restart()
    d2.jobs.jobs.clear()  # the job table is restored from state by the real daemon; this one is not needed
    assert [p["id"] for p in d2.memguard.status()["paused"]] == [saved[0]["id"]]
    r.step(avail=2000)  # first tick of the new daemon
    assert r.events("ADOPTED") and r.freeze_value("loadgov-gate-x.scope") == "1"
    for _ in range(4):  # low memory: stays paused
        r.step(avail=2500)
    assert r.freeze_value("loadgov-gate-x.scope") == "1"
    for _ in range(12):
        r.step(avail=7000)
    assert r.freeze_value("loadgov-gate-x.scope") == "0"  # the normal resume rule


def test_restart_drops_an_entry_whose_processes_are_gone(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    t = r.impl_tree(300)
    r.step(avail=2000)
    assert r.d.status["memguard"]["paused"]
    r.remove(t["work"])
    r.remove(t["child"])
    r.restart()
    r.step(avail=2000)
    assert r.d.status["memguard"]["paused"] == [] and r.events("RELEASED")


def test_recover_force_thaws_everything_and_lists_without_force(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    t = r.impl_tree(300)
    r.step(avail=2000)  # freezes the scope (scopes first)
    for _ in range(4):
        r.step(avail=2000)  # then stops the legacy tree
    assert r.freeze_value("loadgov-gate-x.scope") == "1" and r.state(t["work"]) == "T"
    r.d.limits.set_flag("freeze", "test")
    r.d._after_change({})
    out, err = _Out(), _Out()
    sock = str(tmp_path / "none.sock")
    fake = Signals(r.host.fs, False, r.kernel.opener, r.kernel.sender, r.kernel.closer)
    kw = dict(out=out, err=err, euid=lambda: 0, runner=r.kernel.docker_runner, signals=fake)
    assert recover.run(r.host.fs, str(tmp_path / "state"), sock, False, **kw) == 2
    assert r.freeze_value("loadgov-gate-x.scope") == "1" and "would thaw" in out.text
    assert recover.run(r.host.fs, str(tmp_path / "state"), sock, True, **{**kw, "euid": lambda: 1000}) == 4
    assert r.freeze_value("loadgov-gate-x.scope") == "1"
    assert recover.run(r.host.fs, str(tmp_path / "state"), sock, True, **kw) == 0
    assert r.freeze_value("loadgov-gate-x.scope") == "0" and r.state(t["work"]) == "S"
    assert "thaw scope loadgov-gate-x.scope" in out.text and "registry cleared" in out.text
    body = json.load(open(tmp_path / "state" / "state.json"))
    assert body["memguard"] == {} and body["freeze"] is None


def test_recover_refuses_while_a_daemon_answers(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    path = str(tmp_path / "live.sock")
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)
    try:
        err = _Out()
        assert recover.run(r.host.fs, str(tmp_path / "state"), path, True, euid=lambda: 0, err=err) == 5
        assert "stop loadgovd first" in err.text
    finally:
        srv.close()


class _Out:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def test_enforce_off_releases_guard_pauses_by_the_resume_rule(tmp_path, fake_host):
    """rolling `enforce` back thaws what an enforce run paused, by the normal rule, and logs RELEASED."""
    r = Rig(tmp_path, fake_host)
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    r.step(avail=2000)
    assert r.freeze_value("loadgov-gate-x.scope") == "1"
    r.restart(enforce=False)
    r.step(avail=2500)
    assert r.freeze_value("loadgov-gate-x.scope") == "1"  # still low: not released, nothing written
    for _ in range(12):
        r.step(avail=7000)
    assert r.freeze_value("loadgov-gate-x.scope") == "0"
    rel = r.events("RELEASED")
    assert len(rel) == 1 and rel[0]["reason"].startswith("RELEASED") and rel[0]["result"] == "released"
    assert not r.events("RESUME")


# ---- priority: system > human > agent ----------------------------------------------------------


def test_owner_unfreeze_of_a_guard_pause_is_refused_below_the_resume_threshold(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.step(avail=2000)
    r = rig.owner("unfreeze", reason="I want it")
    assert r["ok"] is False and r["refused"] is True and r["restrictedBy"] == "system"
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"
    assert rig.d.rest_call("unfreeze", {}, "192.0.2.5")["restrictedBy"] == "system"
    rig.step(avail=5000)  # recovered, but not to the resume level
    assert rig.owner("unfreeze", reason="x")["refused"] is True
    rig.step(avail=7000)
    r = rig.owner("unfreeze", reason="memory is fine now")
    assert r["ok"] is True and len(r["thawed"]) == 1
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"


def test_human_freeze_is_always_unfreezable_and_sets_the_limits_flag(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    t = rig.impl_tree(300)
    rig.kernel.containers = ["gates-2", "proj-test-db-1", "gates-3-review-x"]
    r = rig.owner("freeze", reason="fan is loud")
    assert r["ok"] and len(r["frozen"]) == 3 and r["would"] is False
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"
    assert {p for p, _ in rig.kernel.signals("SIGSTOP")} == {t["work"], t["child"]}
    assert not rig.kernel.signals(pid=t["claude"])
    assert rig.kernel.mutating_docker() == [["docker", "pause", "gates-2"]]  # the deny list is honoured
    assert rig.call("limits")["limits"]["freeze"] == "fan is loud"
    assert rig.d.limits_summary()["freeze"] == "fan is loud"
    assert rig.call("limits")["limits"]["restriction"]["kind"] == "freeze"
    u = rig.owner("unfreeze", reason="done")  # memory is fine, and it is a human freeze either way
    assert u["ok"] and len(u["thawed"]) == 3 and u["flag_cleared"] is True
    assert rig.freeze_value("loadgov-gate-x.scope") == "0" and rig.state(t["work"]) == "S"
    assert rig.kernel.mutating_docker()[-1] == ["docker", "unpause", "gates-2"]
    assert rig.call("limits")["limits"]["freeze"] is None


def test_human_freeze_survives_low_memory_and_unfreezes_even_then(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.owner("freeze", reason="quiet")
    rig.step(avail=1000)
    assert rig.owner("unfreeze", reason="x")["ok"] is True  # human entries ignore the memory level


def test_freeze_ttl_auto_thaws_and_clears_the_flag(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    r = rig.owner("freeze", reason="ten seconds", ttl="10s")
    assert r["expires_at"] and rig.freeze_value("loadgov-gate-x.scope") == "1"
    rig.step(dt=5)
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"
    rig.step(dt=6)
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"
    assert rig.d.limits_summary()["freeze"] is None


def test_freeze_without_ttl_lasts_until_unfreeze(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.owner("freeze", reason="q")
    rig.step(dt=7200)
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"


def test_freeze_of_one_project_leaves_the_global_flag_alone(rig):
    rig.add(700, "cargo", 1, 900)
    rig.add(710, "cargo", 1, 900)
    rig.scope("loadgov-gate-a.scope", "gate", [700], project="a")
    rig.scope("loadgov-gate-b.scope", "gate", [710], project="b")
    r = rig.owner("freeze", reason="only a", project="a")
    assert len(r["frozen"]) == 1 and rig.freeze_value("loadgov-gate-a.scope") == "1"
    assert rig.freeze_value("loadgov-gate-b.scope") == "0" and rig.d.limits_summary()["freeze"] is None


def test_an_agent_cannot_freeze_or_unfreeze_and_owner_needs_the_claim(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    for op in ("freeze", "unfreeze"):
        r = rig.call(op, AGENT, owner=True, reason="x")
        assert r["error"] == "owner_denied" and "governed cgroup" in r["message"]
        assert rig.call(op, OWNER, reason="x")["error"] == "owner_denied"  # no owner: true
    assert rig.freeze_value("loadgov-gate-x.scope") == "0" and rig.kernel.sent == []
    assert [a["result"] for a in rig.audit() if a["op"] in ("freeze", "unfreeze")] == ["refused"] * 4
    assert rig.call("freeze", OWNER, owner=True)["error"] == "invalid_args"  # reason is required


def test_observe_mode_freeze_is_a_would_but_sets_the_flag(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False)
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    out = r.owner("freeze", reason="q")
    assert out["would"] is True and r.freeze_value("loadgov-gate-x.scope") == "0"
    assert r.kernel.sent == [] and r.d.limits_summary()["freeze"] == "q"
    assert r.owner("unfreeze", reason="q")["ok"]
    assert r.kernel.sent == [] and r.freeze_value("loadgov-gate-x.scope") == "0"


# ---- JVM reaper --------------------------------------------------------------------------------

GRADLE = "/usr/lib/jvm/bin/java -Xmx512m org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.5"


def test_out_of_scope_daemon_is_reaped_before_any_unrelated_scope_is_frozen(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.add(800, "java", 1, 900, cmd=GRADLE, cgroup="/user.slice/gradle")
    rig.step(avail=2900)
    assert rig.kernel.sent == [(800, "SIGTERM")]
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"  # not frozen in the same decision
    rig.step(avail=2900)
    assert rig.freeze_value("loadgov-gate-x.scope") == "1"
    assert rig.kernel.sent == [(800, "SIGTERM")]  # not signalled twice
    assert [e["key"] for e in rig.d.watch.summary()["events"]] == ["reap", "pause"]


def test_a_daemon_inside_a_job_scope_is_not_reaped_before_a_pause(rig):
    rig.add(700, "java", 1, 900, cmd=GRADLE)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.step(avail=2900)
    assert rig.freeze_value("loadgov-gate-x.scope") == "1" and rig.kernel.signals("SIGTERM") == []


def test_below_1536_every_daemon_is_reaped_and_only_daemons(rig):
    rig.add(700, "java", 1, 900, cmd=GRADLE, cgroup="/user.slice/g")
    rig.add(701, "java", 1, 900, cmd="/usr/bin/java org.jetbrains.kotlin.daemon.KotlinCompileDaemon")
    rig.add(702, "java", 1, 900, cmd="/usr/bin/java -Dnote=GradleDaemon org.gradle.launcher.GradleMain build")
    rig.add(703, "vim", 1, 900, cmd="vim GradleDaemon.txt")
    rig.add(
        704, "java", 1, 900, cmd="/usr/bin/java -jar ide.jar org.jetbrains.kotlin.daemon.KotlinCompileDaemonX"
    )
    rig.add(705, "claude", 1, 900, cmd="claude")
    rig.step(avail=1400)
    assert sorted(rig.kernel.signals("SIGTERM")) == [(700, "SIGTERM"), (701, "SIGTERM")]
    assert {p for p, _ in rig.kernel.sent} == {700, 701}
    assert jvm.is_build_daemon("java", ["x", jvm.MAIN_CLASSES[0]])
    assert not jvm.is_build_daemon("vim", ["x", jvm.MAIN_CLASSES[0]])
    assert not jvm.is_build_daemon("java", ["-Dx=" + jvm.MAIN_CLASSES[1]])


def test_reap_op_agent_may_call_it_audited_and_rest_is_owner(rig):
    rig.add(800, "java", 1, 900, cmd=GRADLE)
    r = rig.call("reap", AGENT, reason="memory looks tight")
    assert r["ok"] and [x["pid"] for x in r["reaped"]] == [800] and r["would"] is False
    assert rig.kernel.sent == [(800, "SIGTERM")]
    a = [x for x in rig.audit() if x["op"] == "reap"]
    assert a[0]["principal"] == "agent" and a[0]["result"] == "applied"
    rr = rig.d.rest_call("reap", {}, "192.0.2.5")
    assert rr["ok"]
    b = [x for x in rig.audit() if x["op"] == "reap"][-1]
    assert (b["principal"], b["source"], b["peer"]) == ("owner", "rest", "192.0.2.5")
    assert rig.call("reap", AGENT, owner=True)["error"] == "owner_denied"


def test_reap_in_observe_mode_is_a_would(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False)
    r.add(800, "java", 1, 900, cmd=GRADLE)
    out = r.call("reap", AGENT)
    assert out["would"] is True and out["reaped"][0]["result"] == "would" and r.kernel.sent == []


def test_pre_gate_reap_only_with_enforce_and_low_memory(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=True, avail=12000)
    r.add(800, "java", 1, 900, cmd=GRADLE)
    r.step(dt=1)
    assert r.call("reap", AGENT, pre_gate=True)["skipped"] == "enough memory"
    r.step(avail=7000, dt=1)  # below 8 GB; above the guard's thresholds
    assert r.call("reap", AGENT, pre_gate=True)["reaped"][0]["pid"] == 800
    (tmp_path / "off").mkdir()
    off = Rig(tmp_path / "off", fake_host, enforce=False, avail=7000)
    off.add(800, "java", 1, 900, cmd=GRADLE)
    off.step(dt=1)
    assert off.call("reap", AGENT, pre_gate=True)["skipped"] == "enforce is off" and off.kernel.sent == []


# ---- orphan reaper ----------------------------------------------------------------------------------


def test_orphaned_stopped_tree_is_thawed_termed_and_killed_after_60_s(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = r.impl_tree(300)
    r.step(avail=2000)
    assert r.state(t["work"]) == "T"
    r.remove(t["shell"])  # the owning agent session is gone
    r.step(avail=2000)
    seq = [(p, s) for p, s in r.kernel.sent if s != "SIGSTOP"]
    assert (t["work"], "SIGCONT") in seq and (t["work"], "SIGTERM") in seq
    assert seq.index((t["work"], "SIGCONT")) < seq.index((t["work"], "SIGTERM"))
    assert not r.kernel.signals("SIGKILL")
    r.step(avail=2000, dt=30)
    assert not r.kernel.signals("SIGKILL")
    r.step(avail=2000, dt=31)  # 61 s after the SIGTERM
    assert {p for p, _ in r.kernel.signals("SIGKILL")} == {t["work"], t["child"]}
    r.kernel.die_on = {t["work"], t["child"]}
    r.kernel.sender(t["work"], 9)
    r.kernel.sender(t["child"], 9)
    r.step(avail=2000)
    assert r.d.status["memguard"]["paused"] == []
    assert not r.kernel.signals(pid=t["claude"])


def test_orphan_reaper_in_observe_mode_only_reports(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=False)
    t = r.impl_tree(300)
    r.step(avail=2000)
    r.remove(t["shell"])
    before = list(r.kernel.sent)
    r.step(avail=2000)
    r.step(avail=2000)
    assert r.kernel.sent == before
    assert len([e for e in r.events("REAP") if e["reason"].startswith("WOULD REAP")]) == 1


def test_foreign_stopped_processes_are_only_reported(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    r.add(900, "vim", 1, 5, state="T", cgroup="/loop-a")
    s = r.step(avail=12000)
    assert s["foreign_stopped"] == [{"pid": 900, "comm": "vim", "cgroup": "/loop-a"}]
    r.step(avail=12000)
    assert r.kernel.sent == [] and len(r.events("REPORT")) == 1


# ---- containers, proj-b rule and the marker ------------------------------------------------------


def test_docker_pause_respects_the_deny_list_and_the_proj_b_marker(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.kernel.containers = ["gates-1", "review-p9-test-db", "job-3-review-x", "other", "fix-p2"]
    marker = r.host.fs.lock_path("proj-b-guard.paused")
    r.step(avail=1900)  # below the proj-b rule's 2048 MB
    assert r.kernel.mutating_docker() == [["docker", "pause", "fix-p2"]] or r.kernel.mutating_docker() == [
        ["docker", "pause", "gates-1"]
    ]
    assert open(marker).read() == "paused\n"
    for _ in range(3):
        r.step(avail=1900)
    r.step(avail=1900)
    paused = {a[2] for a in r.kernel.mutating_docker() if a[1] == "pause"}
    assert paused == {"gates-1", "fix-p2"}  # only allowed names; the deny list and "other" never
    for _ in range(40):
        r.step(avail=5000)
    assert not os.path.exists(marker)
    assert {a[2] for a in r.kernel.mutating_docker() if a[1] == "unpause"} == {"gates-1", "fix-p2"}


def test_marker_is_not_written_in_observe_mode_and_a_legacy_marker_is_left_alone(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False)
    marker = r.host.fs.lock_path("proj-b-guard.paused")
    open(marker, "w").write("paused\n")  # written by the legacy guard
    r.kernel.containers = ["gates-1"]
    r.step(avail=1900)
    r.step(avail=1900)
    assert open(marker).read() == "paused\n" and r.kernel.mutating_docker() == []


def test_proj_b_project_rule_does_not_fire_at_the_global_threshold(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.kernel.containers = ["gates-1"]
    r.step(avail=2500)  # below proj-a's 3 GB, above proj-b' 2 GB: its own work is not paused
    assert r.kernel.mutating_docker() == []
    r.step(avail=2000, swap=0)
    assert r.kernel.mutating_docker() == [["docker", "pause", "gates-1"]]


def test_proj_b_swap_rule_pauses_when_swap_is_high_and_avail_below_4gb(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.kernel.containers = ["gates-1"]
    r.step(avail=5000, swap=2500)
    assert r.kernel.mutating_docker() == []  # leftover swap alone is no pressure
    r.step(avail=3900, swap=2500)
    assert r.kernel.mutating_docker() == [["docker", "pause", "gates-1"]]


def test_claude_p_sessions_under_the_project_cwd_are_stopped_minus_claude(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host)
    r.add(1100, "claude", 1, 600, cmd="claude -p do-it", cwd="/srv/work/proj-b/sub")
    r.add(1101, "cargo", 1100, 400)
    r.add(1200, "claude", 1, 600, cmd="claude -p other", cwd="/srv/work/elsewhere")
    r.add(1201, "cargo", 1200, 400)
    r.step(avail=1900)
    assert r.kernel.signals("SIGSTOP") == [(1101, "SIGSTOP")]


def _matches(tmp_path, fake_host, agent_cwd, cwd):
    """True when a `claude -p` session in `cwd` is stopped by a project with `agent_cwd` (one tree per sample)."""
    r = Rig(tmp_path, fake_host, projects={"proj-b": GuardProject(2048, 4096, 2048, 12, None, agent_cwd)})
    r.add(1100, "claude", 1, 600, cmd="claude -p go", cwd=cwd)
    r.add(1101, "cargo", 1100, 400)
    r.step(avail=1900)
    return r.kernel.signals("SIGSTOP") == [(1101, "SIGSTOP")]


def test_agent_cwd_string_matches_itself_and_below_but_not_a_sibling_name(tmp_path, fake_host):
    for i, (cwd, want) in enumerate(
        (("/x/proj", True), ("/x/proj/sub", True), ("/x/proj-other", False), ("/x/project", False))
    ):
        assert _matches(tmp_path / str(i), fake_host, "/x/proj", cwd) is want, cwd


def test_agent_cwd_list_matches_every_entry_on_a_path_boundary(tmp_path, fake_host):
    lst = ["/x/proj/", "/x/proj-wt"]
    for i, (cwd, want) in enumerate(
        (("/x/proj/sub", True), ("/x/proj-wt/a", True), ("/x/proj-wt", True), ("/x/proj-other", False))
    ):
        assert _matches(tmp_path / str(i), fake_host, lst, cwd) is want, cwd


def test_agent_cwd_accepts_a_string_or_a_list_and_rejects_the_rest():
    def cwd(v):
        return (
            parse_config(f"[memguard.projects.p]\nlow_mb=1\nhigh_mb=3000\nagent_cwd={v}")
            .memguard.projects["p"]
            .agent_cwd
        )

    assert cwd("'/a'") == ("/a",) and cwd("['/a', '/b']") == ("/a", "/b")
    assert GuardProject(1, 3000, None, 3, None, "/a").agent_cwd == ("/a",)
    for bad in ("'rel'", "['/a', 'rel']", "[]", "5", "['/a', 5]", "[['/a']]", "true"):
        with pytest.raises(ConfigError):
            cwd(bad)


# ---- status, REST, config, docs ---------------------------------------------------------------------


def test_status_has_memguard_capabilities_and_validates(rig):
    s = rig.d.tick()
    from loadgov.protocol import validate_status

    assert validate_status(s) == []
    assert s["capabilities"] == ["freeze", "reap"]
    m = s["memguard"]
    assert (m["low_mb"], m["resume_mb"], m["crit_mb"]) == (3072, 6144, 1500)
    assert m["enforce"] is True and m["paused"] == [] and m["cooldown_s"] == 0
    assert rig.call("status")["status"]["capabilities"] == ["freeze", "reap"]
    rig.call("set", OWNER, owner=True, ram_floor_gb=5.0, reason="x")
    assert rig.d.tick()["memguard"]["low_mb"] == 5120 and rig.d.status["memguard"]["resume_mb"] == 7168


def test_ha_integration_lights_up_on_the_capabilities_field(rig):
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "custom_components/loadgov/coordinator.py").read_text()
    assert 'status.get("capabilities")' in src and "FEATURE_FREEZE" in src
    st = rig.d.tick()
    assert set(st["capabilities"]) == {"freeze", "reap"}  # the strings const.py names


def test_rest_freeze_unfreeze_reap_run_the_owner_path(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    r = rig.d.rest_call("freeze", {"ttl": "15m"}, "192.0.2.5")
    assert r["ok"] and rig.freeze_value("loadgov-gate-x.scope") == "1"
    fr = [a for a in rig.audit() if a["op"] == "freeze"][0]
    assert (fr["principal"], fr["source"], fr["peer"]) == ("owner", "rest", "192.0.2.5")
    assert "REST freeze" in fr["reason"]
    assert rig.d.rest_call("unfreeze", {}, "192.0.2.5")["ok"]
    assert rig.freeze_value("loadgov-gate-x.scope") == "0"
    assert rig.d.rest_call("freeze", {"bogus": 1}, "p")["error"] == "bad_request"


def test_rest_http_routes_exist_and_use_the_same_path(tmp_path, fake_host):
    from test_rest import http, make, run_rest

    d = make(tmp_path, fake_host("calm"))

    async def scenario(port):
        c, body, _ = await http(port, "POST", "/v1/freeze", {"reason": "q"})
        assert c == 200 and body["ok"] and body["would"] is True  # observe mode by default
        c, body, _ = await http(port, "POST", "/v1/unfreeze", {})
        assert c == 200 and body["ok"]
        c, body, _ = await http(port, "POST", "/v1/reap", {})
        assert c == 200 and body["would"] is True
        assert (await http(port, "POST", "/v1/freeze", {"nope": 1}))[0] == 400
        assert (await http(port, "GET", "/v1/freeze"))[0] == 405

    run_rest(d, scenario)
    ops = [(a["op"], a["source"]) for a in (json.loads(x) for x in open(d.audit.path)) if a["op"] in
           ("freeze", "unfreeze", "reap")]  # fmt: skip
    assert ops == [("freeze", "rest"), ("unfreeze", "rest"), ("reap", "rest")]


def test_config_memguard_and_reaper_sections():
    c = parse_config("[memguard]\nenforce = true\npre_gate_reap_gb = 6\n[reaper]\nenforce = true\n")
    assert c.memguard.enforce and c.memguard.pre_gate_reap_gb == 6 and c.reaper.enforce
    assert parse_config("").memguard.enforce is False and parse_config("").reaper.enforce is False
    assert parse_config("").memguard.projects == {}  # no built-in projects
    c = parse_config(
        "[memguard.projects.proj-b]\nlow_mb = 1024\nhigh_mb = 4096\nswap_mb = 2048\n"
        "[memguard.projects.new]\nlow_mb=1\nhigh_mb=4000\n"
    )
    assert c.memguard.projects["proj-b"] == GuardProject(1024, 4096, 2048, 3, None, None)
    assert c.memguard.projects["new"].high_mb == 4000
    p = parity_config().memguard  # the legacy-parity values live in tests/parity.toml
    assert p.projects["proj-b"] == GuardProject(
        2048, 4096, 2048, 12, p.projects["proj-b"].containers, "/srv/work/proj-b"
    )
    assert p.legacy_snapshot.project == "proj-a" and p.pause_marker.file == "proj-b-guard.paused"
    assert parse_config("").memguard.deny_containers == ("*-test-db*", "*-review-*")
    assert (
        parse_config("").memguard.legacy_snapshot is None and parse_config("").memguard.pause_marker is None
    )
    for bad in ("[memguard]\nenforce = 1", "[memguard]\nx = 1", "[reaper]\nenforce = 'y'",
                "[memguard.projects.p]\nlow_mb = 5", "[memguard.projects.p]\nlow_mb=9\nhigh_mb=3", "[memguard.projects.p]\nlow_mb=1000\nhigh_mb=3047",
                "[memguard.projects.p]\nlow_mb=1\nhigh_mb=3000\ncontainers='('",
                "[memguard]\ndeny_containers = 'x'", "[memguard]\nlegacy_snapshot = { prefix = '/t' }",
                "[memguard]\npause_marker = { file = 'a/b', project = 'p' }"):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_config(bad)


def test_log_path_gets_legacy_style_lines(tmp_path, fake_host):
    log = tmp_path / "memguard.log"
    r = Rig(tmp_path, fake_host, log_path=str(log))
    r.impl_tree(300)
    r.step(avail=2000)
    line = log.read_text().splitlines()[0]
    assert "PAUSE implementer 300" in line and "avail=2000MB" in line


def test_the_pause_is_saved_before_the_freeze_is_written(rig, monkeypatch):
    """A crash right after `cgroup.freeze = 1` must still leave the entry in state.json."""
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    seen = []
    real = cgroup.write_freeze

    def spy(fs, cg, frozen, enforce):
        if frozen:
            body = json.load(open(rig.tmp / "state" / "state.json"))
            seen.append([e["how"] for e in body["memguard"]["paused"]])
        return real(fs, cg, frozen, enforce)

    monkeypatch.setattr(cgroup, "write_freeze", spy)
    rig.step(avail=2000)
    assert seen == [["freeze"]]


def test_a_failed_pause_is_dropped_and_never_pushed(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    (rig.cg / "build.slice").mkdir(exist_ok=True)
    (rig.cg / SCOPE_FREEZE_PATH).unlink()  # the kernel file is gone: the write fails
    rig.step(avail=2000)
    assert rig.d.status["memguard"]["paused"] == []
    assert [e["request"]["event"] for e in rig.events()] == ["PAUSE_FAILED"]
    assert rig.d.watch.summary()["events"] == []


SCOPE_FREEZE_PATH = "build.slice/build-work.slice/build-work-test.slice/loadgov-gate-x.scope/cgroup.freeze"


def test_restrict_cgroup_ignores_everything_outside_the_prefix(tmp_path, fake_host):
    """The host tests' boundary: only job scopes below the prefix; no legacy trees, containers or JVMs."""
    r = Rig(tmp_path, fake_host, restrict="/build.slice/build-work.slice/build-work-test.slice/")
    r.kernel.containers = ["gates-1"]
    r.impl_tree(300)
    r.add(800, "java", 1, 900, cmd=GRADLE)
    r.add(700, "cargo", 1, 900)
    r.scope("loadgov-gate-in.scope", "gate", [700])
    r.add(710, "cargo", 1, 900)
    r.scope("loadgov-gate-out.scope", "gate", [710])
    r.d.jobs.jobs["loadgov-gate-out.scope"].cgroup = "/elsewhere/loadgov-gate-out.scope"
    r.step(avail=1400)
    assert r.freeze_value("loadgov-gate-in.scope") == "1"
    assert r.kernel.sent == [] and r.kernel.mutating_docker() == []
    assert r.freeze_value("loadgov-gate-out.scope") == "0"


# ---- review fixes: who may signal, honest results, live-work safety ---------------------------------


def orphaned(r):
    t = r.impl_tree(300)
    r.step(avail=2000)
    assert r.state(t["work"]) == "T"
    r.remove(t["shell"])
    return t


def test_orphan_signals_follow_the_reaper_flag_only_reaper_on_memguard_off(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = orphaned(r)
    r.restart(enforce=False, reaper=True)  # memguard off now; the real entry is re-adopted
    r.step(avail=2500)
    assert (t["work"], "SIGTERM") in r.kernel.sent  # [reaper] enforce alone allows it


def test_orphan_signals_reaper_off_memguard_on_sends_nothing(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=True, reaper=False)
    orphaned(r)
    before = list(r.kernel.sent)
    r.step(avail=2500)
    r.step(avail=2500)
    assert r.kernel.sent == before
    assert [e["reason"].startswith("WOULD REAP") for e in r.events("REAP")] == [True]


def test_reap_lines_carry_the_real_send_result_and_the_entry_stays_until_gone(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = orphaned(r)
    r.specs[t["work"]].comm = "claude"  # the tree now has an agent in it: the signal is refused
    r.write(t["work"])
    r.step(avail=2500)
    line = [e["reason"] for e in r.events("REAP")][-1]
    assert "refused" in line and "SIGTERM sent" not in line
    assert not r.kernel.signals("SIGTERM", t["work"])
    assert r.d.status["memguard"]["paused"]  # still registered: the tree is not gone
    r.specs[t["work"]].comm = "node"
    r.write(t["work"])
    r.remove(t["child"])
    r.step(avail=2500)
    assert "1 sent" in [e["reason"] for e in r.events("REAP")][-1]
    r.kernel.die_on = {t["work"]}
    r.kernel.sender(t["work"], 9)
    r.step(avail=2500)
    assert r.d.status["memguard"]["paused"] == []


def test_agent_reap_and_pre_gate_spare_a_daemon_inside_a_job_scope(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, avail=7000)
    r.add(700, "java", 1, 900, cmd=GRADLE)
    r.scope("loadgov-gate-x.scope", "gate", [700])
    r.add(800, "java", 1, 900, cmd=GRADLE, cgroup="/user.slice/idle")
    r.step(dt=1)
    out = r.call("reap", AGENT)
    assert [x["pid"] for x in out["reaped"]] == [800]
    assert [x["pid"] for x in r.call("reap", AGENT, pre_gate=True)["reaped"]] == [800]
    assert not r.kernel.signals(pid=700)
    own = r.d.rest_call("reap", {}, "192.0.2.5")  # the owner's explicit reap keeps "all"
    assert sorted(x["pid"] for x in own["reaped"]) == [700, 800]


def test_below_1536_the_system_reap_still_takes_every_daemon(rig):
    rig.add(700, "java", 1, 900, cmd=GRADLE)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.step(avail=1400)
    assert (700, "SIGTERM") in rig.kernel.sent


def test_a_scope_records_the_claude_pid_as_its_session(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    r.add(499, "bash", 1, 5)
    r.add(500, "claude", 499, 600)
    r.add(1501, "cargo", 500, 900)
    r.scope("loadgov-impl-x.scope", "impl", [499, 500, 1501], owner_pid=901)
    r.add(901, "loadctl", 1)
    r.step(avail=2000)
    assert r.d.memguard.engine.paused[0].session["pid"] == 500
    r.remove(901)  # a SIGKILLed loadctl run must not get the live agent's work reaped
    r.step(avail=2000)
    r.step(avail=2000)
    assert r.kernel.signals("SIGTERM") == [] and r.state(1501) == "T"


def test_fail_closed_when_the_registry_cannot_be_saved(rig, monkeypatch):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    monkeypatch.setattr(rig.d.store, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rig.step(avail=2000)
    assert rig.freeze_value("loadgov-gate-x.scope") == "0" and rig.kernel.sent == []
    assert rig.d.status["memguard"]["paused"] == []
    ev = rig.events("REFUSED")
    assert ev and "persist failed" in ev[0]["reason"] and ev[0]["result"] == "refused"
    r = rig.owner("freeze", reason="q")
    assert rig.freeze_value("loadgov-gate-x.scope") == "0" and r["frozen"] == []


def test_freeze_orphan_targets_carry_start_times(rig):
    rig.add(700, "cargo", 1, 900)
    rig.scope("loadgov-gate-x.scope", "gate", [700])
    rig.step(avail=2000)
    e = rig.d.memguard.engine.paused[0]
    assert rig.d.memguard._target_pids(e) == [(700, rig.specs[700].start)]


def test_orphan_that_stays_refused_is_given_up_after_ten_rounds_and_audited(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = orphaned(r)
    r.specs[t["work"]].comm = "claude"  # every SIGTERM is refused
    r.write(t["work"])
    r.remove(t["child"])
    for _ in range(9):
        r.step(avail=2500)
    assert r.d.status["memguard"]["paused"], "still retrying after 9 rounds"
    r.step(avail=2500)
    assert r.d.status["memguard"]["paused"] == []
    line = [e["reason"] for e in r.events("REAP")][-1]
    assert "giving up after 10 rounds" in line and "refused" in line
    n = len(r.kernel.sent)
    r.step(avail=2500)
    assert len(r.kernel.sent) == n  # nothing is signalled any more


def test_orphan_whose_targets_all_changed_is_dropped_at_once(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = orphaned(r)
    r.specs[t["work"]].start += 1  # the pid now belongs to a different process
    r.write(t["work"])
    r.remove(t["child"])
    r.step(avail=2500)
    assert r.d.status["memguard"]["paused"] == []
    assert "every target is gone or changed" in [e["reason"] for e in r.events("REAP")][-1]


def test_orphan_that_cannot_be_thawed_is_never_given_up_alerts_once_and_retries_slowly(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, reaper=True)
    t = orphaned(r)
    r.specs[t["work"]].comm = "claude"  # SIGTERM refused ...
    r.write(t["work"])
    r.remove(t["child"])
    r.kernel.ignore_cont = True  # ... and the SIGCONT does not take effect: the tree stays stopped
    for _ in range(12):
        r.step(avail=2500)
    assert r.d.status["memguard"]["paused"], "an entry whose tree may still be stopped is kept"
    assert r.state(t["work"]) == "T"
    keys = [e["key"] for e in r.d.status["alerts"]["events"]]
    assert keys.count("stuck_paused") == 1
    stuck = [e for e in r.d.status["alerts"]["events"] if e["key"] == "stuck_paused"][0]
    assert stuck["level"] == "critical"
    n = len(r.kernel.sent)
    r.step(avail=2500)
    assert len(r.kernel.sent) == n  # slow cadence: nothing within 60 s
    r.step(avail=2500, dt=61)
    assert len(r.kernel.sent) > n  # retried after 60 s
    assert [e["key"] for e in r.d.status["alerts"]["events"]].count("stuck_paused") == 1
    r.kernel.ignore_cont = False
    r.step(avail=2500, dt=61)  # the thaw works at last: now it may be given up
    assert r.d.status["memguard"]["paused"] == []
    assert "giving up" in [e["reason"] for e in r.events("REAP")][-1]
