"""Jobs started by `loadctl run`: one transient systemd scope each, registered with the daemon.

A job lives while its scope's `cgroup.procs` is non-empty (the job pid and start time decide when the
cgroup is gone or unknown). `job_end` removes it at once; a job whose `job_end` never arrives is dropped
on the first tick after the scope empties. The slot a job holds is released by the slot manager on the
same condition (`SlotManager.bind_scope`), not by this table.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass

from loadgov.engines.endings import INCOMPLETE_PEAK, Endings, classify
from loadgov.engines.eta import finish_estimate
from loadgov.engines.jobstats import JobStats, PeakBook
from loadgov.engines.slots import HolderRef, _stamp, parse_stat, read_stat
from loadgov.engines.usage import UsageMeter
from loadgov.keys import key_url

log = logging.getLogger("loadgovd")


@dataclass
class Job:
    kind: str
    project: str
    label: str
    unit: str  # the scope unit name, e.g. loadgov-impl-x-123.scope
    cgroup: str  # cgroup v2 path of the scope
    pid: int  # the job process (systemd-run execs the command in place)
    start: int | None  # its start time (stat field 22)
    owner_pid: int  # the `loadctl run` process
    slot: str | None  # "heavy" / "compile" while the job holds one
    since: float
    # last values seen by the meter (engines/usage.py); they survive a daemon restart with the job
    cpu_us: int | None = None  # the scope's cpu.stat usage_usec
    peak_b: int | None = None  # the scope's memory.peak, bytes
    host_us0: int | None = None  # root cgroup usage_usec at the job's start
    host_us: int | None = None  # ... at the last sample
    docker_us: float = 0.0  # CPU of Docker scopes attributed to this job
    key: str | None = None  # issue key the client passed; never derived
    oom0: int | None = None  # scope memory.events oom_kill at the job's start
    oom: int | None = None  # ... at the last read (the scope is gone by the time the tick sees it empty)

    @property
    def ref(self) -> HolderRef:
        return HolderRef(self.pid, self.start, self.cgroup)


RECENT_MAX = 64  # ended jobs remembered for a late `job_end` overrun report
RECENT_S = 600.0


class JobTable:
    def __init__(self, fs, clock=time.time, stat_reader=read_stat, scope_reader=None, pattern=None, **meter):
        self.clock, self.stat_reader = clock, stat_reader
        self.scope_reader = scope_reader or fs.scope_procs
        self.jobs: dict[str, Job] = {}
        self.meter = UsageMeter(fs, lambda: self.jobs, pattern, clock=clock, **meter)
        self.stats = JobStats()
        self.endings = Endings()
        self.aborted: set[str] = set()  # units whose rustc a memguard ABORT terminated (until they end)
        self.peaks = PeakBook()  # compile memory peaks per (project, crate) for the slot's memory admission
        self.url_template = ""  # [keys] url
        self.events: list[dict] = []
        self.on_end: Callable[[Job, dict], None] | None = None  # (job, its job_end audit event); may add keys
        # unit -> [job, ended_at, overrun already audited]; lets a `job_end` that arrives after the tick
        # ended the job still audit its overrun, exactly once
        self.recent: OrderedDict[str, list] = OrderedDict()

    def _remember(self, j: Job, counted: bool) -> None:
        now = self.clock()
        self.recent[j.unit] = [j, now, counted]
        while self.recent and (
            len(self.recent) > RECENT_MAX or now - next(iter(self.recent.values()))[1] > RECENT_S
        ):
            self.recent.popitem(last=False)

    def late_overrun(self, unit: str) -> Job | None:
        """The recently ended light job of `unit` whose overrun was not audited yet (marks it audited)."""
        rec = self.recent.get(unit)
        if rec is None or rec[2] or rec[0].kind != "light" or self.clock() - rec[1] > RECENT_S:
            return None
        rec[2] = True
        return rec[0]

    def unregistered_overrun(self, j: Job) -> Job | None:
        """A light job that never registered (`job_start` failed) reports its overrun, audited once."""
        if j.unit in self.jobs or j.unit in self.recent:
            return None
        self._remember(j, True)
        return j

    def _ev(self, op: str, result: str, reason: str, j: Job) -> None:
        self.events.append(
            {"op": op, "principal": "agent", "result": result, "reason": reason, "project": j.project,
             "pid": j.pid, "request": {"kind": j.kind, "label": j.label, "unit": j.unit, "slot": j.slot}}
        )  # fmt: skip

    def overrun(self, j: Job, reason: str) -> None:
        """Audit `light_overrun` for a light job that hit its runtime or memory limit."""
        self._ev("light_overrun", "overrun", reason, j)
        self._keyed(self.events[-1], j)

    def note_abort(self, unit: str) -> None:
        """A real memguard ABORT hit a process of this job's scope (the join for `killed_by_memguard`)."""
        if unit in self.jobs:
            self.aborted.add(unit)

    def _oom_kills(self, j: Job) -> int | None:
        text = self.meter.fs.cgroup(j.cgroup, "memory.events") if j.cgroup else None
        for ln in (text or "").splitlines():
            key, _, val = ln.partition(" ")
            if key == "oom_kill" and val.strip().isdigit():
                return int(val)
        return None

    def _oom_seen(self, j: Job) -> bool:
        n = self._oom_kills(j)
        if n is not None:
            j.oom = n
        return j.oom is not None and j.oom0 is not None and j.oom > j.oom0

    def amend(self, unit: str, report: dict) -> dict | None:
        """A `job_end` report that came after the tick ended the job: replace a `scope_empty_unknown`
        cause by the one the report gives. The ending (project, label, before and after cause) or None."""
        rec = self.endings.find(unit)
        if rec is None or rec["cause"] != "scope_empty_unknown":
            return None
        cause = classify(
            oom=bool(report.get("oom_kill")),
            overrun=report.get("overrun"),
            timeout=bool(report.get("timeout")),
            cancelled=bool(report.get("cancelled")),
            exit_code=report.get("exit_code"),
            reported=True,
        )
        self.stats.recount(rec["project"], rec["kind"], rec["cause"], cause)
        before = rec["cause"]
        rec.update(cause=cause, exit_code=report.get("exit_code"), signal=report.get("signal"))
        return {"project": rec["project"], "kind": rec["kind"], "label": rec["label"], "before": before,
                "after": cause}  # fmt: skip

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    def docker_cpu(self, key: tuple) -> float:
        """Docker CPU seconds the usage meter attributed to the jobs of a watched slot holder (`SlotManager`
        progress key): the job of its scope, or the jobs loadctl run started under its pid; 0 if none."""
        mine = [
            j
            for j in self.jobs.values()
            if (j.unit == key[2] if key[0] == "scope" else j.owner_pid == key[1])
        ]
        return sum(j.docker_us for j in mine) / 1e6

    def start_of(self, pid: int) -> int | None:
        st = parse_stat(self.stat_reader(pid))
        return st[1] if st else None

    def add(self, j: Job) -> None:
        self.meter.sample()  # flush what the running jobs used so far, then start j's baseline
        self.jobs[j.unit] = j
        j.oom0 = j.oom = self._oom_kills(j)
        self.meter.sample()
        self._ev("job_start", "accepted", f"{j.kind} job {j.label} in {j.unit}", j)
        self._keyed(self.events[-1], j)

    def end(self, unit: str, reason: str = "job_end", report: dict | None = None) -> Job | None:
        """End a job. `report` is the `job_end` request's outcome (exit_code, signal, overrun, cancelled,
        timeout, oom_kill); None when the tick ended it. The ending gets exactly one cause (endings.py)."""
        j = self.jobs.get(unit)
        if j:
            use = self.meter.finish(j, self.clock())  # while j is still registered: its last reads count
            rep = report or {}
            cause = classify(
                memguard=unit in self.aborted,
                oom=self._oom_seen(j) or bool(rep.get("oom_kill")),
                overrun=rep.get("overrun"),
                timeout=bool(rep.get("timeout")),
                cancelled=bool(rep.get("cancelled")),
                exit_code=rep.get("exit_code"),
                reported=report is not None,
            )
            self.aborted.discard(unit)
            del self.jobs[unit]
            self._remember(j, reason.startswith("light_overrun"))
            self.stats.record(j.project, j.kind, use, cause)
            if j.kind == "compile":
                self.peaks.record(j.project, j.label, use.get("mem_peak_mb"), cause in INCOMPLETE_PEAK)
            self._ev("job_end", "ended", reason, j)
            self._keyed(self.events[-1], j)
            self.events[-1].update(use)  # cpu_s, mem_peak_mb, duration_s, host_cpu_s, docker_cpu_s
            self.events[-1].update(exit_code=rep.get("exit_code"), signal=rep.get("signal"), cause=cause)
            self.endings.add({
                "project": j.project, "kind": j.kind, "label": j.label, "key": j.key, "unit": unit,
                "ended_at": self.clock(), "duration_s": use["duration_s"],
                "exit_code": rep.get("exit_code"), "signal": rep.get("signal"), "cause": cause,
            })  # fmt: skip
            if self.on_end is not None:
                try:  # best effort (the backstop's leftover scan): never stops a job from ending
                    self.on_end(j, self.events[-1])
                except Exception:
                    log.exception("job end hook failed for %s", unit)
        return j

    def tick(self) -> bool:
        """Drop jobs whose scope is empty. True when something changed."""
        self.meter.sample(force=False)
        for j in self.jobs.values():
            self._oom_seen(j)  # keep the last oom_kill count: the scope is gone once it empties
        gone = [u for u, j in self.jobs.items() if not j.ref.alive(self.stat_reader, self.scope_reader)]
        for u in gone:
            self.end(u, "scope empty")
        return bool(gone)

    @staticmethod
    def _keyed(event: dict, j: Job) -> None:
        if j.key:
            event["request"]["key"] = j.key

    def summary(self) -> list[dict]:
        now, out = self.clock(), []
        for j in self.jobs.values():
            expected = self.stats.expected_s(j.project, j.kind)
            eta, overdue = finish_estimate(j.since, expected, now)
            out.append(
                {"kind": j.kind, "project": j.project, "label": j.label, "unit": j.unit, "cgroup": j.cgroup,
                 "pid": j.pid, "slot": j.slot, "key": j.key, "url": key_url(self.url_template, j.key),
                 "since": _stamp(j.since), "expected_s": expected,
                 "eta": None if eta is None else _stamp(eta), "overdue": overdue}
            )  # fmt: skip
        return out

    def expected_peak_mb(self, project: str, label: str, history: int, percentile: int) -> float | None:
        """Expected memory peak of a compile: the label's own percentile, else the project's compile
        percentile over all its crates (job_stats), else None (the caller uses `default_peak_mb`)."""
        q = percentile / 100
        own = self.peaks.expected(project, label, history, q)
        return own if own is not None else self.stats.mem_peak_pct(project, "compile", q)

    def to_state(self) -> list[dict]:
        return [asdict(j) for j in self.jobs.values()]

    def restore(self, raw) -> None:
        """Re-adopt saved jobs whose scope still has processes; the rest are dropped silently."""
        for item in raw if isinstance(raw, list) else []:
            try:
                j = Job(**item)
            except (TypeError, ValueError):
                log.warning("ignoring unreadable saved job")
                continue
            if j.ref.alive(self.stat_reader, self.scope_reader):
                self.jobs[j.unit] = j
