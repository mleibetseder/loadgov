"""Jobs started by `loadctl run`: one transient systemd scope each, registered with the daemon.

A job lives while its scope's `cgroup.procs` is non-empty (the job pid and start time decide when the
cgroup is gone or unknown). `job_end` removes it at once; a job whose `job_end` never arrives is dropped
on the first tick after the scope empties. The slot a job holds is released by the slot manager on the
same condition (`SlotManager.bind_scope`), not by this table.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

from loadgov.engines.slots import HolderRef, parse_stat, read_stat

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

    @property
    def ref(self) -> HolderRef:
        return HolderRef(self.pid, self.start, self.cgroup)


class JobTable:
    def __init__(self, fs, clock=time.time, stat_reader=read_stat, scope_reader=None):
        self.clock, self.stat_reader = clock, stat_reader
        self.scope_reader = scope_reader or fs.scope_procs
        self.jobs: dict[str, Job] = {}
        self.events: list[dict] = []

    def _ev(self, op: str, result: str, reason: str, j: Job) -> None:
        self.events.append(
            {"op": op, "principal": "agent", "result": result, "reason": reason, "project": j.project,
             "pid": j.pid, "request": {"kind": j.kind, "label": j.label, "unit": j.unit, "slot": j.slot}}
        )  # fmt: skip

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    def start_of(self, pid: int) -> int | None:
        st = parse_stat(self.stat_reader(pid))
        return st[1] if st else None

    def add(self, j: Job) -> None:
        self.jobs[j.unit] = j
        self._ev("job_start", "accepted", f"{j.kind} job {j.label} in {j.unit}", j)

    def end(self, unit: str, reason: str = "job_end") -> Job | None:
        j = self.jobs.pop(unit, None)
        if j:
            self._ev("job_end", "ended", reason, j)
        return j

    def tick(self) -> bool:
        """Drop jobs whose scope is empty. True when something changed."""
        gone = [u for u, j in self.jobs.items() if not j.ref.alive(self.stat_reader, self.scope_reader)]
        for u in gone:
            self.end(u, "scope empty")
        return bool(gone)

    def summary(self) -> list[dict]:
        return [
            {"kind": j.kind, "project": j.project, "label": j.label, "unit": j.unit, "cgroup": j.cgroup,
             "pid": j.pid, "slot": j.slot,
             "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(j.since))}
            for j in self.jobs.values()
        ]  # fmt: skip

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
