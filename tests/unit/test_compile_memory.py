"""Compile memory admission (ILM-34): peaks learned per (project, crate), the compile slot grant waits for
MemAvailable >= expected peak + the guard's low floor. Fakes only: no /proc, no cgroups, no host access."""

import json
import os
from pathlib import Path

import pytest
from conftest import parity_config
from test_slots import add_proc, mk_daemon

from loadgov.cli.loadctl import _slot_why, format_waiters
from loadgov.config import CompileConfig, ConfigError, parse_config
from loadgov.engines.jobs import Job
from loadgov.engines.jobstats import HIST_CAP, MAX_LABELS, MAX_PEAK_PROJECTS, JobStats, PeakBook
from loadgov.engines.slots import SlotManager
from loadgov.host.procfs import HostFS
from loadgov.protocol import STATUS_SCHEMA, validate_status

LOW = 3072
DOC = Path(__file__).resolve().parents[2] / "docs" / "protocol.md"


def stat_text(pid, start=1):
    return f"{pid} (x) S " + "0 " * 18 + f"{start} 0\n"


class Rig:
    """A SlotManager whose host memory, learned peaks and busy-ness the test sets."""

    def __init__(self, tmp_path, **cfg):
        self.clock_t = 1000.0
        self.alive = set()
        fs = HostFS(proc_root=str(tmp_path), lock_root=str(tmp_path))
        self.m = SlotManager(
            fs, clock=lambda: self.clock_t, stat_reader=lambda p: stat_text(p) if p in self.alive else None
        )
        self.book, self.avail, self.total, self.busy = PeakBook(), 20000, 32000, False
        self.m.compile_cfg = CompileConfig(**{"memory_admission": True, **cfg})
        self.m.expected_peak = lambda p, label, h, q: self.book.expected(p, label, h, q / 100)
        self.m.memory = lambda: (self.avail, self.total, LOW)
        self.m.work_running = lambda pid: self.busy

    def acq(self, pid, label, project="web-app", kind="compile", wait=True):
        self.alive.add(pid)
        self.clock_t += 0.1
        return self.m.acquire("compile", project, label, pid, wait, kind=kind)

    def tick(self):
        self.clock_t += 1
        for w in self.m.slots["compile"].queue:  # a polling client stays fresh
            w.seen = self.clock_t
        self.m.tick()

    def head(self):
        return self.m.summary()["compile"]["waiters"][0]


# ---- learning, percentile, persistence, bounds -----------------------------------------------------


def test_percentile_is_taken_over_the_last_history_peaks():
    b = PeakBook()
    for v in (9000, 1000, 1000, 1000, 1000, 1000):
        b.record("web-app", "app_core", v)
    assert b.expected("web-app", "app_core", 20, 1.0) == 9000  # 100 = maximum
    assert b.expected("web-app", "app_core", 5, 1.0) == 1000  # the 9000 is older than the last 5
    assert b.expected("web-app", "app_core", 20, 0.9) == pytest.approx(5000)  # p90, linear interpolation
    assert b.expected("web-app", "unknown_crate", 20, 0.9) is None
    b.record("web-app", "app_core", None)  # no memory.peak: nothing learned
    assert list(b.book["web-app"]["app_core"]) == [9000, 1000, 1000, 1000, 1000, 1000]


def test_one_outlier_in_twenty_does_not_set_p90():
    b = PeakBook()
    b.record("web-app", "app_core", 10000)
    for _ in range(19):
        b.record("web-app", "app_core", 2000)
    assert b.expected("web-app", "app_core", 20, 0.9) == 2000


def test_bounds_per_label_labels_per_project_and_projects():
    b = PeakBook()
    for i in range(HIST_CAP + 10):
        b.record("web-app", "app_core", i)
    assert list(b.book["web-app"]["app_core"]) == list(range(10, HIST_CAP + 10))
    for i in range(MAX_LABELS + 5):
        b.record("web-app", f"crate{i}", 1)
    assert len(b.book["web-app"]) == MAX_LABELS
    assert "app_core" not in b.book["web-app"] and f"crate{MAX_LABELS + 4}" in b.book["web-app"]  # LRU
    b.record("web-app", "crate10", 2)  # touched: now the most recent, survives one more eviction
    b.record("web-app", "fresh", 1)
    assert "crate10" in b.book["web-app"] and "crate5" not in b.book["web-app"]
    for i in range(MAX_PEAK_PROJECTS + 3):
        b.record(f"p{i}", "c", 1)
    assert len(b.book) == MAX_PEAK_PROJECTS and "web-app" not in b.book


def test_state_roundtrip_and_unreadable_entries_are_skipped():
    b = PeakBook()
    b.record("web-app", "app_core", 4600)
    b.record("web-app", "app_core", 5600)
    b.record("web-app", "other", 100)
    c = PeakBook()
    c.restore(json.loads(json.dumps(b.to_state())))
    assert c.to_state() == b.to_state()
    d = PeakBook()
    d.restore({"web-app": {"ok": [1, "x", True, 3.5], "bad": "no"}, "p": 3, 5: {}})
    assert d.to_state() == {"web-app": {"ok": [1.0, 3.5]}}
    d.restore("garbage")


def test_unknown_crate_falls_back_to_the_project_compile_percentile():
    st = JobStats()
    for v in (1000, 2000, 3000):
        st.record("web-app", "compile", {"mem_peak_mb": v, "duration_s": 1})
    st.record("web-app", "gate", {"mem_peak_mb": 90000, "duration_s": 1})
    assert st.mem_peak_pct("web-app", "compile", 1.0) == 3000
    assert st.mem_peak_pct("web-app", "compile", 0.5) == 2000
    assert st.mem_peak_pct("other", "compile", 0.5) is None


# ---- admission --------------------------------------------------------------------------------------


def test_waits_for_memory_then_is_granted_when_it_frees(tmp_path):
    r = Rig(tmp_path)
    r.book.record("web-app", "app_core", 5000)
    r.avail = 7000  # needs 5000 + 3072 = 8072
    q = r.acq(101, "app_core")
    assert (
        q["status"] == "queued"
        and q["reason"] == "waiting for memory: needs ~8072 MB, 7000 MB free, admitted anyway in 15 min"
    )
    w = r.head()
    assert (w["reason"], w["needs_mb"], w["free_mb"], w["peak_mb"]) == (q["reason"], 8072, 7000, 5000)
    assert r.m.summary()["compile"]["state"] == "free"  # the slot is free, the crate waits
    r.tick()
    assert r.m.summary()["compile"]["state"] == "free"
    r.avail = 8072  # exactly enough
    r.tick()
    assert r.m.summary()["compile"]["state"] == "held"
    assert r.acq(101, "app_core")["status"] == "granted"
    ev = [e for e in r.m.drain_events() if e["op"] == "compile_wait_memory"]
    assert len(ev) == 1 and ev[0]["project"] == "web-app"  # audited once, not per tick


def test_enough_memory_grants_at_once_and_other_kinds_are_never_held_back(tmp_path):
    r = Rig(tmp_path)
    r.book.record("web-app", "app_core", 5000)
    assert r.acq(101, "app_core")["status"] == "granted"
    r.m.release("compile", 101)
    r.avail = 100
    assert r.acq(102, "app_core", kind=None)["status"] == "granted"  # a legacy rustc-slot caller: no kind
    r.m.release("compile", 102)
    assert r.acq(103, "app_core", kind="gate")["status"] == "granted"
    heavy = r.m.acquire("heavy", "web-app", "gate", 104, True, kind="compile")  # only the compile slot
    assert heavy["status"] == "granted"


def test_unknown_crate_uses_project_p90_else_default_peak(tmp_path):
    r = Rig(tmp_path, default_peak_mb=1024)
    r.avail = 4096
    assert r.acq(101, "newcrate")["status"] == "granted"  # 1024 + 3072 = 4096 free: fits
    r.m.release("compile", 101)
    r.avail = 4095
    q = r.acq(102, "newcrate")
    assert q["status"] == "queued" and "needs ~4096 MB, 4095 MB free" in q["reason"]
    r.m.release("compile", 102)
    r.m.expected_peak = lambda p, label, h, q: 6000.0  # the project's compile percentile
    q = r.acq(103, "newcrate")
    assert "needs ~9072 MB" in q["reason"]


def test_fifo_the_head_waits_and_no_later_small_compile_passes_it(tmp_path):
    r = Rig(tmp_path)
    r.book.record("web-app", "big", 9000)
    r.book.record("web-app", "small", 100)
    r.avail = 5000
    assert r.acq(101, "big")["status"] == "queued"
    q = r.acq(102, "small")  # would fit, but the head has not started yet
    assert q["status"] == "queued" and q["position"] == 2 and "reason" not in q
    for _ in range(3):
        r.tick()
    assert r.m.summary()["compile"]["state"] == "free"
    ws = r.m.summary()["compile"]["waiters"]
    assert [w["label"] for w in ws] == ["big", "small"]
    assert ws[0]["reason"] and ws[1]["reason"] is ws[1]["needs_mb"] is ws[1]["free_mb"] is None
    r.avail = 12072
    r.tick()
    assert r.m.slots["compile"].holder.label == "big"  # the head first, then the next in FIFO order
    r.m.release("compile", 101)
    assert r.m.slots["compile"].holder.label == "small"


def test_no_memory_reason_while_a_holder_runs(tmp_path):
    r = Rig(tmp_path)
    r.book.record("web-app", "big", 9000)
    assert r.acq(101, "first")["status"] == "granted"
    r.avail = 5000
    q = r.acq(102, "big")
    assert q["status"] == "queued" and "reason" not in q  # behind a holder: the reason is the holder
    assert r.head()["reason"] is None
    r.m.release("compile", 101)
    assert r.head()["reason"].startswith("waiting for memory: needs ~12072 MB, 5000 MB free")


def test_idle_host_escape_admits_a_crate_the_host_can_never_fit(tmp_path):
    r = Rig(tmp_path)
    r.total, r.avail = 16000, 15000
    r.book.record("web-app", "huge", 14000)  # needs 17072 > MemTotal 16000
    r.busy = True
    q = r.acq(101, "huge")
    assert q["status"] == "queued" and "needs ~17072 MB, 15000 MB free" in q["reason"]
    r.tick()
    assert r.m.summary()["compile"]["state"] == "free"
    r.busy = False  # the host is as free as it gets
    r.tick()
    assert r.m.slots["compile"].holder.label == "huge"
    ev = [e for e in r.m.drain_events() if e["op"] == "compile_admitted_idle"]
    assert len(ev) == 1 and ev[0]["result"] == "granted" and "no other build work" in ev[0]["reason"]
    assert ev[0]["project"] == "web-app"


def test_a_crate_that_fits_normally_is_not_audited_as_idle_admission(tmp_path):
    r = Rig(tmp_path)
    r.total, r.avail = 16000, 15000
    r.book.record("web-app", "huge", 14000)
    r.busy = True
    assert r.acq(101, "huge")["status"] == "queued"
    r.busy = False
    r.total = 40000  # the host grew: the crate now fits normally
    r.avail = 20000
    r.tick()
    assert r.m.slots["compile"].holder is not None
    assert not [e for e in r.m.drain_events() if e["op"] == "compile_admitted_idle"]


def test_off_is_todays_behaviour_exactly(tmp_path):
    r = Rig(tmp_path, memory_admission=False)
    r.book.record("web-app", "big", 90000)
    r.avail = 1
    assert r.acq(101, "big")["status"] == "granted"
    s = r.m.summary()["compile"]
    assert s["memory_admission"] is False and s["state"] == "held"
    r.acq(102, "next")
    assert r.head()["reason"] is None
    assert not [e for e in r.m.drain_events() if e["op"].startswith("compile_")]
    # a fresh manager that never heard of the feature has the same default
    fs = HostFS(proc_root=str(tmp_path), lock_root=str(tmp_path))
    m = SlotManager(fs, stat_reader=lambda p: stat_text(p))
    assert m.compile_cfg.memory_admission is False
    assert m.acquire("compile", "web-app", "big", 5, True, kind="compile")["status"] == "granted"
    assert "memory_admission" not in m.summary()["heavy"]


def test_no_sample_yet_never_blocks(tmp_path):
    r = Rig(tmp_path)
    r.m.memory = lambda: None
    assert r.acq(101, "app_core")["status"] == "granted"


# ---- config ------------------------------------------------------------------------------------------


def test_config_defaults_and_validation():
    c = parse_config("").compile
    assert (c.memory_admission, c.default_peak_mb, c.history, c.percentile) == (False, 1024, 20, 90)
    text = "[compile]\nmemory_admission = true\ndefault_peak_mb = 2048\nhistory = 5\npercentile = 100\n"
    c = parse_config(text).compile
    assert (c.memory_admission, c.default_peak_mb, c.history, c.percentile) == (True, 2048, 5, 100)
    for bad in (
        "memory_admission = 1",
        "default_peak_mb = 10",
        "default_peak_mb = true",
        "history = 0",
        "history = 51",
        "percentile = 49",
        "percentile = 101",
        "percentile = 90.5",
        "nope = 1",
    ):
        with pytest.raises(ConfigError):
            parse_config(f"[compile]\n{bad}\n")
    with pytest.raises(ConfigError):
        parse_config("compile = 1\n")


def test_example_config_block_is_valid_when_uncommented():
    text = Path(__file__).resolve().parents[2].joinpath("packaging/config/loadgov.toml.example").read_text()
    block = text.split("# [compile]\n", 1)[1]
    lines = [ln[2:].split("#")[0] for ln in block.splitlines() if ln.startswith("# ") and "=" in ln]
    assert parse_config("[compile]\n" + "\n".join(lines) + "\n").compile == CompileConfig()


# ---- the daemon: jobs, state, status, reload ---------------------------------------------------------


def job(d, unit, kind, label, project="web-app", peak=None):
    j = Job(kind, project, label, unit, f"/{unit}", 900, 1, 900, None, d.clock())
    d.jobs.jobs[unit] = j
    j.peak_b = None if peak is None else peak * 2**20
    return j


def test_finished_compile_jobs_teach_the_peak_only_compiles_and_only_with_a_peak(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    for unit, kind, label, peak in (
        ("u1", "compile", "app_core", 4600),
        ("u2", "compile", "app_core", 5600),
        ("u3", "gate", "gate", 7000),
        ("u4", "compile", "nopeak", None),
    ):
        job(d, unit, kind, label, peak=peak)
        d.jobs.end(unit, report={"exit_code": 0})
    assert d.jobs.peaks.to_state() == {"web-app": {"app_core": [4600.0, 5600.0]}}
    # label p90 of [4600, 5600] = 5500; an unknown crate takes the project's compile p90 from job_stats
    assert d.jobs.expected_peak_mb("web-app", "app_core", 20, 90) == pytest.approx(5500)
    assert d.jobs.expected_peak_mb("web-app", "never_built", 20, 90) == pytest.approx(5500)
    assert d.jobs.expected_peak_mb("other", "never_built", 20, 90) is None
    assert d.jobs.expected_peak_mb("web-app", "app_core", 20, 100) == 5600


def test_peaks_are_saved_with_the_state_and_restored(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    job(d, "u1", "compile", "app_core", peak=4600)
    d.jobs.end("u1", report={"exit_code": 0})
    d._save_state()
    assert json.loads(open(d.store.path).read())["compile_peaks"] == {"web-app": {"app_core": [4600.0]}}
    d2, _ = mk_daemon(fake_host, tmp_path)  # same state dir: a restart
    assert d2.jobs.peaks.to_state() == {"web-app": {"app_core": [4600.0]}}


def test_daemon_status_shows_reason_and_flag_and_validates(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    d._apply_config(parity_config(compile=CompileConfig(memory_admission=True)))
    avail = d.tick()["memory"]["avail_mb"]
    d.jobs.peaks.record("web-app", "app_core", avail)  # needs avail + the low floor: never fits
    args = {"slot": "compile", "project": "web-app", "label": "app_core", "pid": os.getpid(), "wait": True,
            "kind": "compile"}  # fmt: skip
    reply = d.dispatch({"v": 1, "op": "slot_acquire", "args": args}, 501)
    assert reply["status"] == "queued"
    assert (
        reply["reason"]
        == f"waiting for memory: needs ~{avail + LOW} MB, {avail} MB free, admitted anyway in 15 min"
    )
    s = d.tick()
    assert validate_status(s) == []
    c = s["slots"]["compile"]
    assert c["memory_admission"] is True
    w = c["waiters"][0]
    assert (w["needs_mb"], w["free_mb"], w["peak_mb"]) == (avail + LOW, avail, avail)
    assert (
        f"(waiting for memory: needs ~{avail + LOW} MB, {avail} MB free, admitted anyway in 15 min)"
        in format_waiters(c)[0]
    )
    assert d.slots.slots["heavy"].mem_block is None


def test_reload_turns_the_admission_on_and_off_live(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    assert d.slots.compile_cfg.memory_admission is False
    cfg = tmp_path / "loadgov.toml"
    cfg.write_text("[compile]\nmemory_admission = true\nhistory = 7\n")
    d.config_path = str(cfg)
    d.reload_config()
    assert d.slots.compile_cfg == CompileConfig(memory_admission=True, history=7)
    cfg.write_text("")
    d.reload_config()
    assert d.slots.compile_cfg.memory_admission is False


# ---- loadctl and the protocol doc ---------------------------------------------------------------------


def test_slot_wait_line_names_the_memory_reason():
    why = "waiting for memory: needs ~9000 MB, 100 MB free"
    assert _slot_why({"reason": why, "holder": "unknown"}) == why
    assert _slot_why({"holder": "web-app x pid=1"}) == "held by web-app x pid=1"


def test_protocol_doc_describes_the_memory_admission():
    text = DOC.read_text()
    para = next(ln for ln in text.splitlines() if ln.startswith("- **Compile memory admission**"))
    for word in (
        "`[compile] memory_admission`", "`false`", "`default_peak_mb`", "`history`", "`percentile`",
        "`compile_peaks`", "`compile_wait_memory`", "`compile_admitted_idle`", "`compile_admitted_timeout`", "`max_memory_wait_s`", "the head waits",
        "`waiting for memory: needs ~X MB, Y MB free, admitted anyway in N min`", "MemTotal", "`--headroom none`",
    ):  # fmt: skip
        assert word in para
    d = CompileConfig()
    assert f"(default {d.history})" in para and f"(default {d.percentile}," in para
    assert f"(default {d.default_peak_mb})" in para
    for key in ("reason", "needs_mb", "free_mb", "peak_mb"):
        assert f"slots.compile.waiters[].{key}" in STATUS_SCHEMA
        assert f"| `slots.compile.waiters[].{key}` |" in text
    assert "slots.compile.memory_admission" in STATUS_SCHEMA


# ---- the wait is bounded; the waiter's own ancestry is not other work --------------------------------


def test_the_wait_is_bounded_and_the_reason_counts_down(tmp_path):
    r = Rig(tmp_path, max_memory_wait_s=600)
    r.book.record("web-app", "app_core", 5600)
    r.avail = 7000  # needs 8672, never comes
    assert "admitted anyway in 10 min" in r.acq(101, "app_core")["reason"]
    r.clock_t += 300
    r.tick()
    assert "admitted anyway in 5 min" in r.head()["reason"]
    r.clock_t += 295
    r.tick()
    assert r.m.summary()["compile"]["state"] == "free" and "in 1 min" in r.head()["reason"]
    r.clock_t += 10
    r.tick()
    assert r.m.slots["compile"].holder.label == "app_core"
    ev = [e for e in r.m.drain_events() if e["op"] == "compile_admitted_timeout"]
    assert len(ev) == 1 and ev[0]["result"] == "granted" and "7000 MB free" in ev[0]["reason"]
    assert "max_memory_wait_s 600" in ev[0]["reason"]


def test_a_second_blocked_period_restarts_the_clock_and_is_audited_again(tmp_path):
    r = Rig(tmp_path, max_memory_wait_s=600)
    r.book.record("web-app", "app_core", 5600)
    r.avail = 7000
    r.acq(101, "app_core")
    s, w = r.m.slots["compile"], r.m.slots["compile"].queue[0]
    r.clock_t += 500
    assert r.m._memory_block(s, w) is not None
    r.avail = 9000  # fits for a moment (a grant that then fails leaves the waiter queued)
    assert r.m._memory_block(s, w) is None
    r.avail = 7000
    r.clock_t += 400  # 900 s since the first block, but the period started again
    assert "admitted anyway in 10 min" in r.m._memory_block(s, w)["reason"]
    waits = [e for e in r.m.drain_events() if e["op"] == "compile_wait_memory"]
    assert len(waits) == 2


def test_config_max_memory_wait_s():
    assert parse_config("").compile.max_memory_wait_s == 900
    assert parse_config("[compile]\nmax_memory_wait_s = 60\n").compile.max_memory_wait_s == 60
    for bad in ("max_memory_wait_s = 9", "max_memory_wait_s = 86401", "max_memory_wait_s = 1.5"):
        with pytest.raises(ConfigError):
            parse_config(f"[compile]\n{bad}\n")


def test_compile_inside_its_registered_gate_is_not_blocked_by_its_parent(fake_host, tmp_path):
    d, host = mk_daemon(fake_host, tmp_path)
    cfg = parity_config(compile=CompileConfig(memory_admission=True, max_memory_wait_s=600))
    d._apply_config(cfg)
    s = d.tick()
    total = s["memory"]["total_mb"]
    d.jobs.peaks.record("web-app", "huge", total)  # needs more than the host has
    gate, rustc = 701, 702
    add_proc(Path(host.proc), gate, "/gate.scope", "sh", 1)
    add_proc(Path(host.proc), rustc, "/gate.scope/rustc.scope", "rustc", gate)
    for pid, ppid in ((gate, 1), (rustc, gate)):
        (Path(host.proc) / str(pid) / "stat").write_text(f"{pid} (x) S {ppid} " + "0 " * 17 + "5 0\n")
    d.stat_reader = d.slots.stat_reader = lambda pid: (
        (Path(host.proc) / str(pid) / "stat").read_text()
        if (Path(host.proc) / str(pid) / "stat").exists()
        else None
    )  # noqa: E501
    job(d, "gate-unit", "gate", "gate")
    d.jobs.jobs["gate-unit"].cgroup = "/gate.scope"
    d.jobs.jobs["gate-unit"].pid = gate
    assert d.slots.acquire("heavy", "web-app", "gate", gate, True, kind="gate")["status"] == "granted"
    assert d._work_running(rustc) is False  # its parent gate (job and heavy holder) is not other work
    other = job(d, "other-unit", "impl", "someone", peak=None)
    other.cgroup, other.pid = "/elsewhere.scope", 799
    assert d._work_running(rustc) is True  # a foreign job is
    q = d.slots.acquire("compile", "web-app", "huge", rustc, True, kind="compile")
    assert q["status"] == "queued" and "waiting for memory" in q["reason"]
    del d.jobs.jobs["other-unit"]
    d.slots.tick()
    assert d.slots.slots["compile"].holder.label == "huge"  # the idle escape fires inside the gate
    assert [e for e in d.slots.drain_events() if e["op"] == "compile_admitted_idle"]


def test_compile_inside_a_gate_is_admitted_by_the_timeout_when_other_work_runs(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    d._apply_config(parity_config(compile=CompileConfig(memory_admission=True, max_memory_wait_s=60)))
    avail = d.tick()["memory"]["avail_mb"]
    d.jobs.peaks.record("web-app", "app_core", avail)  # never fits, but fits the host's total
    job(d, "other-unit", "impl", "someone")
    pid = os.getpid()
    assert d.slots.acquire("compile", "web-app", "app_core", pid, True, kind="compile")["status"] == "queued"
    assert d.slots.slots["compile"].holder is None
    t = d.clock()
    d.clock = d.slots.clock = lambda: t + 61
    for w in d.slots.slots["compile"].queue:
        w.seen = t + 61
    d.slots.tick()
    assert d.slots.slots["compile"].holder.label == "app_core"
    assert [e for e in d.slots.drain_events() if e["op"] == "compile_admitted_timeout"]


# ---- ILM-36: a compile stopped before its real peak never lowers the estimate ---------------------------


def _learn(d, peaks, label="app_core"):
    for i, p in enumerate(peaks):
        job(d, f"l{label}{i}", "compile", label, peak=p)
        d.jobs.end(f"l{label}{i}", report={"exit_code": 0})


INCOMPLETE = {
    "killed_by_memguard": None,
    "oom_killed": {"oom_kill": True},
    "timeout": {"timeout": True},
    "cancelled": {"cancelled": True},
    "light_overrun": {"overrun": "runtime"},
    "scope_empty_unknown": None,
}


def _end(d, unit, cause, peak):
    job(d, unit, "compile", "app_core", peak=peak)
    if cause == "killed_by_memguard":
        d.jobs.note_abort(unit)
    d.jobs.end(unit, report=INCOMPLETE[cause] if cause != "scope_empty_unknown" else None)
    assert d.jobs.endings.find(unit)["cause"] == cause


@pytest.mark.parametrize("cause", list(INCOMPLETE))
def test_incomplete_ending_with_a_lower_peak_does_not_lower_the_estimate(fake_host, tmp_path, cause):
    d, _ = mk_daemon(fake_host, tmp_path)
    _learn(d, [4000, 5000, 6000])
    before = d.jobs.expected_peak_mb("web-app", "app_core", 20, 90)
    _end(d, "k1", cause, 1000)
    assert d.jobs.peaks.to_state() == {"web-app": {"app_core": [4000.0, 5000.0, 6000.0]}}
    assert d.jobs.expected_peak_mb("web-app", "app_core", 20, 90) == before


@pytest.mark.parametrize("cause", list(INCOMPLETE))
def test_incomplete_ending_with_a_higher_peak_raises_the_estimate(fake_host, tmp_path, cause):
    d, _ = mk_daemon(fake_host, tmp_path)
    _learn(d, [4000, 5000, 6000])
    before = d.jobs.expected_peak_mb("web-app", "app_core", 20, 90)
    _end(d, "k1", cause, 9000)
    assert d.jobs.peaks.to_state()["web-app"]["app_core"][-1] == 9000.0
    assert d.jobs.expected_peak_mb("web-app", "app_core", 20, 90) > before


def test_incomplete_ending_of_a_crate_without_history_teaches_nothing(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    _end(d, "k1", "oom_killed", 9000)
    assert d.jobs.peaks.to_state() == {}


def test_normal_endings_are_learned_as_before_even_when_lower(fake_host, tmp_path):
    d, _ = mk_daemon(fake_host, tmp_path)
    _learn(d, [4000, 5000, 6000])
    for unit, rep in (("n1", {"exit_code": 0}), ("n2", {"exit_code": 101}), ("n3", {})):
        job(d, unit, "compile", "app_core", peak=1000)
        d.jobs.end(unit, report=rep)
    assert d.jobs.peaks.to_state()["web-app"]["app_core"] == [4000.0, 5000.0, 6000.0, 1000.0, 1000.0, 1000.0]


def test_protocol_doc_states_the_incomplete_peak_rule_and_the_restart_clock():
    para = next(ln for ln in DOC.read_text().splitlines() if ln.startswith("- **Compile memory admission**"))
    for cause in INCOMPLETE:
        assert f"`{cause}`" in para
    assert "higher than every peak kept" in para and "restart" in para and "`mem_since`" in para
