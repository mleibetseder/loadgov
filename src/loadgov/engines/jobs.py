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
from dataclasses import asdict, dataclass

from loadgov.engines.eta import finish_estimate
from loadgov.engines.jobstats import JobStats
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
        self.url_template = ""  # [keys] url
        self.events: list[dict] = []
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
        self.meter.sample()
        self._ev("job_start", "accepted", f"{j.kind} job {j.label} in {j.unit}", j)
        self._keyed(self.events[-1], j)

    def end(self, unit: str, reason: str = "job_end") -> Job | None:
        j = self.jobs.get(unit)
        if j:
            use = self.meter.finish(j, self.clock())  # while j is still registered: its last reads count
            del self.jobs[unit]
            self._remember(j, reason.startswith("light_overrun"))
            self.stats.record(j.project, j.kind, use)
            self._ev("job_end", "ended", reason, j)
            self._keyed(self.events[-1], j)
            self.events[-1].update(use)  # cpu_s, mem_peak_mb, duration_s, host_cpu_s, docker_cpu_s
        return j

    def tick(self) -> bool:
        """Drop jobs whose scope is empty. True when something changed."""
        self.meter.sample(force=False)
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
