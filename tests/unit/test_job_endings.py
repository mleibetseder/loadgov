"""Job endings: one cause per ended job, precedence, the ring, job_stats counts, `loadctl jobs --ended`."""

import json
import os
import subprocess

import pytest
from memguard_rig import Rig
from test_headroom import make_daemon
from test_light_lane import add_jobs
from test_run import Host, call, go, stamp

from loadgov.cli import loadctl
from loadgov.engines.endings import CAUSES, RING_MAX, Endings, classify
from loadgov.engines.jobs import Job
from loadgov.protocol import VERSION


@pytest.fixture(name="procs")
def _procs():
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


@pytest.fixture
def rig(tmp_path, fake_host):
    return Rig(tmp_path, fake_host)


def wire(monkeypatch, d, ends=None):
    def req(_sock, op, args=None, **_k):
        d.t += 1
        d.status["at"] = stamp(d.t)
        d.status["at_ms"] = int(d.t * 1000)
        if op == "job_end" and ends is not None:
            ends.append(args)
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, os.getpid())

    monkeypatch.setattr(loadctl, "request", req)


def one(d, kind="impl", project="web-app", **end):
    """Register a job (no scope) and end it with the given job_end arguments; return the stored ending."""
    add_jobs(d, 1, project, kind)
    unit = next(iter(d.jobs.jobs))
    r = call(d, "job_end", unit=unit, **end)
    assert r["ok"] and r["ended"] is True
    rec = d.jobs.endings.find(unit)
    assert rec["cause"] in CAUSES
    return rec


def test_classify_precedence_first_match_wins():
    every = {"memguard": True, "oom": True, "overrun": "runtime", "timeout": True, "cancelled": True,
             "exit_code": 1}  # fmt: skip
    for want, key in (("killed_by_memguard", "memguard"), ("oom_killed", "oom"), ("light_overrun", "overrun"),
                      ("timeout", "timeout"), ("cancelled", "cancelled"), ("failed", "exit_code")):  # fmt: skip
        assert classify(**every) == want
        every.pop(key)
    assert classify(exit_code=0) == "ok"
    assert classify(reported=True) == "reported_without_code"
    assert classify() == "scope_empty_unknown"


def test_each_cause_path_sets_exactly_one_cause(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    assert one(d, exit_code=0)["cause"] == "ok"
    assert one(d, exit_code=2)["cause"] == "failed"
    assert one(d, exit_code=137, signal=9)["signal"] == 9
    assert one(d, exit_code=130, signal=2, cancelled=True)["cause"] == "cancelled"
    assert one(d, exit_code=1, timeout=True)["cause"] == "timeout"
    assert one(d, exit_code=1, oom_kill=True)["cause"] == "oom_killed"
    assert one(d, kind="light", exit_code=76, overrun="runtime")["cause"] == "light_overrun"
    assert one(d, kind="light", exit_code=76, overrun="memory")["cause"] == "light_overrun"
    assert one(d)["cause"] == "reported_without_code"  # an old client: no exit code
    add_jobs(d, 1, "web-app", "impl")
    d.jobs.end(next(iter(d.jobs.jobs)), "scope empty")
    assert d.jobs.endings.recent()[0]["cause"] == "scope_empty_unknown"
    causes = [r["cause"] for r in d.jobs.endings.recent()]
    assert len(causes) == 10 and set(causes) == set(CAUSES) - {"killed_by_memguard"}
    # killed_by_memguard: see the memguard join below


def test_the_job_end_event_and_job_stats_carry_the_cause(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1, "web-app", "impl")
    unit = next(iter(d.jobs.jobs))
    d.jobs.drain_events()
    d.jobs.end(unit, "job_end", {"exit_code": 137, "signal": 9})
    ev = d.jobs.drain_events()[-1]
    assert (ev["op"], ev["exit_code"], ev["signal"], ev["cause"]) == ("job_end", 137, 9, "failed")
    one(d, exit_code=0)
    one(d, exit_code=0)
    row = d.jobs.stats.summary()[0]
    assert row["causes"] == {"failed": 1, "ok": 2} and row["count"] == 3


def test_an_abort_joins_its_job_by_unit(rig):
    rig.add(800, "rustc", 1, 9000, cmd="rustc --crate-name app")
    rig.scope("loadgov-compile-x.scope", "compile", [800])
    rig.step(avail=2000)
    rig.step(avail=1400)
    assert rig.kernel.signals() == [(800, "SIGTERM")]
    assert rig.events("ABORT")[0]["request"]["unit"] == "loadgov-compile-x.scope"
    assert rig.d.jobs.aborted == {"loadgov-compile-x.scope"}
    rig.d.jobs.end("loadgov-compile-x.scope", "job_end", {"exit_code": 143, "signal": 15})
    end = rig.d.jobs.endings.recent()[0]
    assert (end["cause"], end["exit_code"], end["signal"]) == ("killed_by_memguard", 143, 15)
    assert rig.d.jobs.aborted == set()  # one ending, then the mark is gone


def test_a_shadow_abort_marks_no_job(tmp_path, fake_host):
    r = Rig(tmp_path, fake_host, enforce=False)
    r.add(800, "rustc", 1, 9000, cmd="rustc --crate-name app")
    r.scope("loadgov-compile-x.scope", "compile", [800])
    r.step(avail=2000)
    r.step(avail=1400)
    assert r.events("ABORT")[0]["reason"].startswith("WOULD ABORT")
    assert r.d.jobs.aborted == set()


def test_an_oom_kill_in_the_scope_during_the_job(fake_host, tmp_path):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    cg = "/x/loadgov-impl-oom.scope"
    path = os.path.join(host.cgroup, cg.lstrip("/"))
    os.makedirs(path)
    events = os.path.join(path, "memory.events")
    with open(events, "w") as f:
        f.write("low 0\noom 0\noom_kill 1\n")
    j = Job("impl", "web-app", "l", "loadgov-impl-oom.scope", cg, 1000, 1, 1000, None, d.clock())
    d.jobs.add(j)
    assert j.oom0 == 1
    with open(events, "w") as f:
        f.write("low 0\noom 0\noom_kill 2\n")
    d.jobs.tick()  # records the count; the scope then vanishes with its memory.events
    os.remove(events)
    d.jobs.end(j.unit, "scope empty")
    assert d.jobs.endings.recent()[0]["cause"] == "oom_killed"
    # no growth since the start: not an OOM kill
    with open(events, "w") as f:
        f.write("oom_kill 5\n")
    j2 = Job("impl", "web-app", "l", "loadgov-impl-calm.scope", cg, 1001, 1, 1001, None, d.clock())
    d.jobs.add(j2)
    d.jobs.end(j2.unit, "job_end", {"exit_code": 1})
    assert d.jobs.endings.recent()[0]["cause"] == "failed"


def test_a_late_job_end_replaces_scope_empty_unknown_once(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1, "web-app", "impl")
    unit = next(iter(d.jobs.jobs))
    d.jobs.end(unit, "scope empty")
    assert d.jobs.stats.summary()[0]["causes"] == {"scope_empty_unknown": 1}
    r = call(d, "job_end", unit=unit, exit_code=0)
    assert r["ok"] and r["ended"] is False
    rec = d.jobs.endings.find(unit)
    assert (rec["cause"], rec["exit_code"]) == ("ok", 0)
    assert d.jobs.stats.summary()[0]["causes"] == {"ok": 1}
    call(d, "job_end", unit=unit, exit_code=9)  # a repeat changes nothing
    assert d.jobs.endings.find(unit)["cause"] == "ok" and len(d.jobs.endings.ring) == 1


def test_a_late_overrun_after_the_tick_becomes_light_overrun(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    unit = next(iter(d.jobs.jobs))
    d.jobs.end(unit, "scope empty")
    tail = unit.removesuffix(".scope").rsplit("-", 1)[-1]
    peer = int(tail) if tail.isdigit() else None
    args = {"unit": unit, "overrun": "runtime", "exit_code": 76}
    d.dispatch({"v": VERSION, "op": "job_end", "args": args}, peer)
    assert d.jobs.endings.find(unit)["cause"] == "light_overrun"


def test_a_bad_report_is_invalid_args(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    for bad in ({"exit_code": "1"}, {"exit_code": 256}, {"exit_code": True}, {"signal": 0}, {"cancelled": 1}):
        assert call(d, "job_end", unit="loadgov-impl-x-1.scope", **bad)["error"] == "invalid_args"


def test_the_ring_is_bounded_and_survives_a_restart():
    e = Endings()
    for i in range(RING_MAX + 20):
        e.add({"project": "p", "kind": "impl", "label": "l", "key": None, "unit": f"u{i}", "ended_at": 100.0 + i,
               "duration_s": 1.0, "exit_code": 0, "signal": None, "cause": "ok"})  # fmt: skip
    assert len(e.ring) == RING_MAX and e.find("u0") is None and e.find(f"u{RING_MAX + 19}")
    assert e.recent(limit=2)[0]["unit"] == f"u{RING_MAX + 19}"
    assert len(e.recent(since=100.0 + RING_MAX + 15)) == 5
    again = Endings()
    again.restore(json.loads(json.dumps(e.to_state())) + [{"cause": "bogus"}, "x"])
    assert again.to_state() == e.to_state()


def test_endings_and_causes_are_saved_in_the_state_file(fake_host, tmp_path):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    one(d, exit_code=4)
    assert d._save_state()
    d2 = make_daemon(host, tmp_path)
    assert [r["cause"] for r in d2.jobs.endings.recent()] == ["failed"]
    assert d2.jobs.stats.summary()[0]["causes"] == {"failed": 1}


def test_job_endings_op_since_and_limit(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    one(d, exit_code=1)
    d.t += 7200
    one(d, exit_code=0)
    rows = call(d, "job_endings")["endings"]
    assert [r["cause"] for r in rows] == ["ok", "failed"] and rows[0]["ended_at"].endswith("Z")
    assert [r["cause"] for r in call(d, "job_endings", since="1h")["endings"]] == ["ok"]
    assert len(call(d, "job_endings", limit=1)["endings"]) == 1
    assert call(d, "job_endings", since="soon")["error"] == "invalid_args"
    assert call(d, "job_endings", limit=0)["error"] == "invalid_args"


def test_loadctl_jobs_ended_renders_and_prints_json(fake_host, tmp_path, monkeypatch, capsys):
    d = make_daemon(fake_host("calm"), tmp_path)
    one(d, exit_code=1)
    one(d, exit_code=137, signal=9)
    wire(monkeypatch, d)
    assert loadctl.main(["jobs", "--ended"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and "web-app impl" in out[0] and "exit 137 signal 9 failed" in out[0]
    assert loadctl.main(["jobs", "--ended", "--since", "1h", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["exit_code"] for r in rows] == [137, 1]
    assert set(rows[0]) == {"project", "kind", "label", "key", "unit", "ended_at", "duration_s", "exit_code",
                            "signal", "cause"}  # fmt: skip
    assert loadctl.format_endings([]) == "no jobs ended in that time"
    assert loadctl.main(["jobs", "--ended", "--since", "x"]) != 0


def test_loadctl_run_sends_exit_code_signal_and_cancel(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ends = []
    wire(monkeypatch, d, ends)
    for code, want in ((0, {"exit_code": 0}), (3, {"exit_code": 3}), (-9, {"exit_code": 137, "signal": 9})):
        ends.clear()
        h = Host(host, procs().pid, code=code)
        go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "web-app", "--label", "x", "--", "true")
        assert {k: v for k, v in ends[0].items() if k != "unit"} == want
    last = d.jobs.endings.recent()[0]
    assert (last["exit_code"], last["signal"], last["cause"]) == (137, 9, "failed")


def test_loadctl_run_reports_a_forwarded_signal_as_cancelled(fake_host, tmp_path, monkeypatch, procs):
    import signal

    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ends = []
    wire(monkeypatch, d, ends)
    h = Host(host, procs().pid, code=-15, on_wait=lambda _p: signal.raise_signal(signal.SIGTERM))
    go(monkeypatch, d, h.deps, "--kind", "impl", "--project", "web-app", "--label", "x", "--", "true")
    assert ends[0]["cancelled"] is True and ends[0]["signal"] == 15
    assert d.jobs.endings.recent()[0]["cause"] == "cancelled"


def test_a_late_replacement_is_audited_once(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1, "web-app", "impl")
    unit = next(iter(d.jobs.jobs))
    d.jobs.end(unit, "scope empty")
    call(d, "job_end", unit=unit, exit_code=2, signal=9)
    call(d, "job_end", unit=unit, exit_code=0)  # a repeat: nothing more
    rows = [e for e in d.audit.tail(200) if e["op"] == "job_end_amended"]
    assert len(rows) == 1
    r = rows[0]
    q = r["request"]
    assert (q["unit"], q["cause_before"], q["cause"], q["exit_code"], q["signal"]) == (
        unit, "scope_empty_unknown", "failed", 2, 9)  # fmt: skip
    assert r["result"] == "amended" and r["reason"] == "cause scope_empty_unknown -> failed"
    assert "pid" in r  # the peer, as on every op line


def test_an_abort_whose_signal_was_not_sent_does_not_mark_the_job(rig):
    rig.d.memguard.signals.send = lambda pid, start, sig: "gone"
    rig.add(800, "rustc", 1, 9000, cmd="rustc --crate-name app")
    rig.scope("loadgov-compile-x.scope", "compile", [800])
    rig.step(avail=2000)
    rig.step(avail=1400)
    assert rig.events("ABORT") and rig.d.jobs.aborted == set()
