"""CPU governor: port of the legacy CPU governor script, steering the cap of `build-work.slice`.

Every 10 s it takes the host's busy share (from /proc/stat jiffies accumulated since the last decision;
idle = idle + iowait, integer percent like the script). One sample above TARGET lowers the cap by 0.5 core
(not below MIN 1.0); nine samples in a row below TARGET - 10 raise it by 0.5 (not above the effective
`max_cores`); anything else resets both counters. TARGET is the effective `target_cpu_pct`.

The start value is min(cap in the target cgroup, the caps of the mirrored cgroups, effective `max_cores`):
it only lowers, never raises a target (ADR-0001); enforce writes it to any target above it. A step
down never raises the cap (an adopted 0.5 stays 0.5). The 10 s window runs on a monotonic clock. When the
effective `max_cores` falls below the cap, the cap is clamped down at once, whatever the sample cadence;
when it rises, the cap climbs at the normal pace. With `enforce` off nothing is written: the engine keeps
a virtual cap and records `WOULD` events. On stop or crash the last cap stays (never `max`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from loadgov.engines.limits import iso
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS

log = logging.getLogger("loadgovd")

PRIMARY = "build.slice/build-work.slice"  # ADR-0001, "Slice naming"
MIN_CORES = 1.0
STEP = 0.5
INTERVAL_S = 10.0
DOWN_AFTER = 1  # samples above TARGET
UP_AFTER = 9  # samples below TARGET - ROOM_PCT
ROOM_PCT = 10


def busy_int(prev: tuple[int, int], cur: tuple[int, int]) -> int | None:
    """Legacy `(100 * (dt - di)) / dt` in integer arithmetic; None when no time has passed."""
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    return None if dt <= 0 else (100 * (dt - di)) // dt


class Governor:
    def __init__(
        self,
        fs: HostFS,
        clock: Callable[[], float],
        enforce: bool = False,
        mirrors: tuple[str, ...] = (),  # [compat] mirror_cgroups: other cgroups that follow the cap
        primary: str = PRIMARY,  # test-only override (integration test on loadgov-test.slice)
        interval_s: float = INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,  # the 10 s window; wall-clock steps cannot stall it
    ):
        self.fs, self.clock = fs, clock
        self.enforce, self.mirrors = enforce, tuple(mirrors)
        self.primary = primary
        self.interval_s, self.monotonic = interval_s, monotonic
        self.cap: float | None = None  # None until the target cgroup exists
        self.max_cores, self.target_pct = 0.0, 0.0
        self.last_busy: int | None = None
        self.last_change_at: float | None = None
        self.last_reason: str | None = None
        self._hi = self._lo = 0
        self._base: tuple[int, int, float] | None = None  # jiffies and time of the last decision window
        self._events: list[dict] = []

    # ---- driving ---------------------------------------------------------------------------------

    def tick(self, cpu_times: tuple[int, int], max_cores: float, target_pct: float) -> None:
        """Call on every daemon tick with the current (total, idle) jiffies and the effective limits."""
        self.target_pct = target_pct
        self.clamp(max_cores)
        now = self.monotonic()
        if self._base is None:
            self._base = (*cpu_times, now)
            return
        if now - self._base[2] < self.interval_s - 0.5:  # a 5 s daemon tick lands on every second call
            return
        busy = busy_int(self._base[:2], cpu_times)
        self._base = (*cpu_times, now)
        if busy is None or self.cap is None:
            return
        self.last_busy = busy
        if busy > target_pct:
            self._hi, self._lo = self._hi + 1, 0
        elif busy < target_pct - ROOM_PCT:
            self._lo, self._hi = self._lo + 1, 0
        else:
            self._hi = self._lo = 0
        if self._hi >= DOWN_AFTER:
            self._move(min(self.cap, max(self.cap - STEP, MIN_CORES)), busy, f"host above {target_pct:g}%")
            self._hi = 0
        elif self._lo >= UP_AFTER:
            self._move(min(self.cap + STEP, self.max_cores), busy, "room again")
            self._lo = 0

    def clamp(self, max_cores: float) -> None:
        """Adopt the effective `max_cores`; a cap above it drops to it now (owner or HA lowered it)."""
        self.max_cores = max_cores
        if self.cap is None:
            self._start()
        if self.cap is not None and self.cap > max_cores:
            self._move(max_cores, self.last_busy, f"max_cores lowered to {max_cores:g}")
        if self.cap is not None and self.enforce:
            m = self._mirror_cores()
            if m is not None and m < self.cap:  # a lower mirror cap is adopted, never overridden
                self.cap = m
            self._lower("target above cap")

    def drain_events(self) -> list[dict]:
        out, self._events = self._events, []
        return out

    # ---- internals -------------------------------------------------------------------------------

    def _mirror_cores(self) -> float | None:
        caps = [c.cores for c in (cgroup.read_cpu_max(self.fs, m) for m in self.mirrors) if c is not None]
        caps = [c for c in caps if c is not None]
        return min(caps) if caps else None

    def _start(self) -> None:
        """cap = min(primary cap, mirror cap while mirrored, max_cores): it only ever lowers."""
        cur = cgroup.read_cpu_max(self.fs, self.primary)
        if cur is None:
            return  # no such cgroup: nothing to govern, retried on the next tick
        caps = [self.max_cores, cur.cores, self._mirror_cores()]
        self.cap = min(c for c in caps if c is not None)
        self._lower("start")

    def _lower(self, reason: str) -> None:
        """Bring every target that is above the cap down to it (enforce); never raises a target.
        In observe mode only the start is noted, as a WOULD event."""
        noted = False
        for path in [self.primary, *self.mirrors]:
            cur = cgroup.read_cpu_max(self.fs, path)
            if cur is None or (cur.cores is not None and cur.cores <= self.cap):
                continue
            err = None
            if self.enforce:
                try:
                    cgroup.write_cpu_max(self.fs, path, self.cap)
                except OSError as e:
                    err = e.strerror or str(e)
                    log.warning("governor: cannot lower %s to %g cores: %s", path, self.cap, err)
                    continue
            elif reason != "start":
                continue
            if not noted:  # the primary first; a mirror-only lowering is audited too
                self._note(cur.cores, self.cap, None, reason, self.enforce, err)
                noted = True

    def _note(self, old, new, busy, reason, applied, error=None) -> None:
        self.last_change_at, self.last_reason = self.clock(), reason
        self._events.append(
            {"before": old, "cap": new, "busy": busy, "reason": reason, "applied": applied, "error": error}
        )

    def _move(self, new: float, busy: int | None, reason: str) -> None:
        old = self.cap
        if new == old:
            return
        error = None
        if self.enforce:
            error = self._write(new)
            if error and not error.startswith("mirror"):
                log.warning("governor: cannot set %s to %g cores: %s", self.primary, new, error)
                return  # the cap did not change: the next decision tries again
        self.cap = new
        self._note(old, new, busy, reason, self.enforce, error)

    def _write(self, cores: float) -> str | None:
        try:
            cgroup.write_cpu_max(self.fs, self.primary, cores)
        except OSError as e:
            return e.strerror or str(e)
        first = None
        for mirror in self.mirrors:
            if cgroup.read_cpu_max(self.fs, mirror) is None:
                continue
            try:
                cgroup.write_cpu_max(self.fs, mirror, cores)
            except OSError as e:
                msg = f"mirror {mirror}: {e.strerror or e}"
                log.warning("governor: %s", msg)
                first = first or msg
        return first

    # ---- reporting -------------------------------------------------------------------------------

    def status(self) -> dict:
        paths = [self.primary, *self.mirrors]
        targets = []
        for path in paths:
            raw = self.fs.cgroup(path, "cpu.max")
            targets.append(
                {"path": path, "present": raw is not None, "cpu_max": raw.strip() if raw else None}
            )
        return {
            "enforce": self.enforce,
            "cap_cores": self.cap,
            "target_pct": self.target_pct,
            "max_cores": self.max_cores,
            "min_cores": MIN_CORES,
            "last_busy_pct": self.last_busy,
            "last_change_at": iso(self.last_change_at),
            "last_reason": self.last_reason,
            "targets": targets,
        }


def event_line(ev: dict) -> str:
    """`cap=<c> cores busy=<b>% (<reason>)`, the legacy governor log text after the timestamp."""
    busy = "-" if ev["busy"] is None else ev["busy"]
    return f"cap={ev['cap']:g} cores busy={busy}% ({ev['reason']})"
