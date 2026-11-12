"""Memory backstop on `build.slice/build-work.slice`: a hard `memory.max` / `memory.swap.max` that
only fires when the memory guard was too slow, plus `memory.oom.group = 1` on every job scope so an OOM
kill takes that one scope down as a group and nothing else.

`[backstop] enabled = false` (default) is the enforce flag (AGENTS.md rule 2): nothing is written to any
cgroup file. With it on, the daemon sets exactly the configured values (never `max`, never below 4 GB for
`memory.max`, checked by the config) and re-asserts them when they differ. A `memory.max` at or below the
slice's current usage is not written (the kernel would OOM-kill running builds at once): the status says
`applied: false` and the write follows when usage has fallen. Rollback: with the flag off and a marker
`<state-dir>/backstop.json` from an earlier run, `max` is written back to both files (the only place `max`
is written), then the marker is removed.

Detection: every tick the `oom_kill` / `oom_group_kill` counters of `memory.events` of the slice (they
include all descendant scopes) are compared with the last reading; an increase emits one `BACKSTOP` alert
(critical) naming the scope and its `memory.peak` (the slice's when the killed scope is already gone), plus an
audit line.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable

from loadgov.config import BackstopConfig
from loadgov.daemon.state import atomic_write_text
from loadgov.engines.governor import PRIMARY
from loadgov.engines.limits import iso
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS

log = logging.getLogger("loadgovd")
MIB = 1024 * 1024
LIMIT_FILES = ("memory.max", "memory.swap.max")


class BackstopRuntime:
    def __init__(
        self,
        fs: HostFS,
        cfg: BackstopConfig,
        clock: Callable[[], float],
        state_dir: str,
        emit: Callable[[str, dict], object],  # Daemon.emit_event
        audit: Callable[[dict], None],  # AuditLog.append
        jobs: Callable[[], dict],  # unit -> Job (has .cgroup)
        slice_path: str = PRIMARY,  # test-only override (integration test on loadgov-test.slice)
    ):
        self.fs, self.cfg, self.clock, self.emit, self.audit, self._jobs = fs, cfg, clock, emit, audit, jobs
        self.slice = slice_path
        self.marker = os.path.join(state_dir, "backstop.json")
        self.applied = False
        self.note: str | None = None
        self.oom_kills = 0
        self.last_event: str | None = None
        self._base: tuple[int, int] | None = None  # (oom_kill, oom_group_kill) at the last reading
        self._seen: dict[str, tuple[int, int]] = {}  # unit -> (last memory.peak MB, own oom_kill)
        self._grouped: set[str] = set()
        self._said: str | None = None

    # ---- driving ---------------------------------------------------------------------------------

    def tick(self) -> None:
        if self.cfg.enabled:
            self._apply()
            self._group_all()
            self._detect()
        elif os.path.exists(self.marker):
            self._rollback()

    def on_job(self, unit: str, job_cgroup: str) -> None:
        """A job was just registered: its scope exists, so `memory.oom.group` can be set at once."""
        if self.cfg.enabled:
            self._group(unit, job_cgroup)

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "applied": self.applied,
            "memory_max_mb": self.cfg.memory_max_mb,
            "memory_swap_max_mb": self.cfg.memory_swap_max_mb,
            "oom_kills": self.oom_kills,
            "last_event": self.last_event,
            "note": self.note,
        }

    # ---- apply and roll back ---------------------------------------------------------------------

    def _say(self, result: str, reason: str) -> None:
        """Audit and log a state change once (not on every tick)."""
        if reason == self._said:
            return
        self._said = reason
        log.info("backstop: %s", reason)
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": result, "reason": reason, "cgroup": self.slice})  # fmt: skip

    def _read(self, name: str) -> str | None:
        text = self.fs.cgroup(self.slice, name)
        return None if text is None else text.strip()

    def _apply(self) -> None:
        want = {
            "memory.max": str(self.cfg.memory_max_mb * MIB),
            "memory.swap.max": str(self.cfg.memory_swap_max_mb * MIB),
        }
        have = {name: self._read(name) for name in LIMIT_FILES}
        if None in have.values():
            self.applied, self.note = False, f"{self.slice} has no memory limit files"
            self._say("failed", self.note)
            return
        todo = [n for n in LIMIT_FILES if have[n] != want[n]]
        if not todo:
            self.applied, self.note = True, None
            return
        self.applied = False
        try:  # the marker first (like a memory guard pause): without it a rollback could not find the slice
            os.makedirs(os.path.dirname(self.marker), exist_ok=True)
            atomic_write_text(self.marker, json.dumps({"slice": self.slice}))
        except OSError as e:
            self.note = f"refused: cannot persist {self.marker}: {e.strerror}"
            self._say("refused", self.note)
            return
        usage = cgroup.read_int(self.fs, self.slice, "memory.current")
        if "memory.max" in todo and usage is not None and usage >= int(want["memory.max"]):
            limit_mb = int(want["memory.max"]) // MIB
            self.note = f"deferred: usage {usage // MIB} MB is not below memory.max {limit_mb} MB"
            self._say("deferred", self.note)
            todo.remove("memory.max")
        for name in todo:
            try:
                cgroup.write_memory_limit(self.fs, self.slice, name, int(want[name]))
            except OSError as e:
                self.note = f"cannot write {name}: {e.strerror}"
                self._say("failed", self.note)
                return
        if all(self._read(n) == want[n] for n in LIMIT_FILES):
            self.applied, self.note = True, None
            self._say("applied", f"memory.max {self.cfg.memory_max_mb} MB, "
                                 f"memory.swap.max {self.cfg.memory_swap_max_mb} MB")  # fmt: skip

    def _rollback(self) -> None:
        """The flag is off but an earlier run wrote the limits: put `max` back (the documented rollback)."""
        self.applied = False
        failed = []
        for name in LIMIT_FILES:
            try:
                cgroup.clear_memory_limit(self.fs, self.slice, name)
            except FileNotFoundError:
                continue  # the slice is gone: nothing left to undo
            except OSError as e:
                failed.append(f"{name}: {e.strerror}")
        if failed:
            self.note = "rollback incomplete: " + "; ".join(failed)
            self._say("failed", self.note)
            return
        try:
            os.unlink(self.marker)
        except OSError as e:
            self.note = f"rollback done but {self.marker} stays: {e.strerror}"
            return
        self.note = None
        self._say("rollback", f"memory.max and memory.swap.max of {self.slice} set back to max")

    # ---- memory.oom.group ------------------------------------------------------------------------

    def _group(self, unit: str, job_cgroup: str) -> None:
        if unit in self._grouped:
            return
        self._grouped.add(unit)
        try:
            cgroup.write_oom_group(self.fs, job_cgroup)
        except FileNotFoundError:
            pass  # the scope is already gone
        except OSError as e:
            log.warning("backstop: cannot set memory.oom.group on %s: %s", unit, e.strerror)

    def _group_all(self) -> None:
        jobs = self._jobs()
        self._grouped &= set(jobs)
        for unit, j in jobs.items():
            self._group(unit, j.cgroup)

    # ---- detection -------------------------------------------------------------------------------

    def _detect(self) -> None:
        live: dict[str, tuple[int, int]] = {}
        for unit, j in self._jobs().items():
            peak = cgroup.read_int(self.fs, j.cgroup, "memory.peak")
            own = (cgroup.read_events(self.fs, j.cgroup) or {}).get("oom_kill", 0)
            live[unit] = ((peak or 0) // MIB, own)
        before, self._seen = self._seen, live
        ev = cgroup.read_events(self.fs, self.slice)
        if ev is None:
            return
        now = (ev.get("oom_kill", 0), ev.get("oom_group_kill", 0))
        prev, self._base = self._base, now
        if prev is None or now[0] < prev[0] or now[1] < prev[1]:
            return  # first reading, or the counters restarted
        kills = now[0] - prev[0]
        if kills <= 0 and now[1] == prev[1]:
            return
        self.oom_kills += max(kills, 1)
        grew = [u for u, (_, own) in live.items() if u in before and own > before[u][1]]
        names = grew or [u for u in before if u not in live]  # a killed scope is usually gone by now
        scope = ", ".join(names[:3]) if names else "unknown (no scope found)"
        slice_peak = (cgroup.read_int(self.fs, self.slice, "memory.peak") or 0) // MIB
        # a scope that is still there reports its own memory.peak; a killed one is gone, and its high-water
        # mark lives on in the slice's memory.peak (the scope's last reading is up to one tick old)
        peaks = [live[u][0] if u in live else slice_peak for u in names]
        peak = max(peaks) if peaks else slice_peak
        msg = f"OOM kill in {self.slice}: {max(kills, 1)} process(es), scope {scope}, memory.peak {peak} MB"
        self.last_event = f"{iso(self.clock())} {scope} peak {peak} MB"
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": "event", "reason": msg, "cgroup": self.slice})  # fmt: skip
        self.emit("backstop", {"message": msg, "scope": scope, "memory_peak_mb": peak})
