"""Sample history: one compact CSV row per 15 s in `<state-dir>/history.csv`, about 7 days.

Same shape as the legacy monitor's history file: a header line, then rows; the file is trimmed in chunks
(once it holds `KEEP + SLACK` rows it is rewritten with the newest `KEEP`), through a temp file and
`os.replace`, so a reader or a crash sees the old or the new file. The daemon ticks every 5 s; `record`
writes at most one row per `interval_s` (clock-driven, so tests use a fake clock).
"""

from __future__ import annotations

import os
from collections.abc import Callable

from loadgov.daemon.state import atomic_write_text

COLUMNS = (
    "at", "level", "busy_pct", "cpu_psi60", "mem_psi60", "io_psi60", "avail_mb", "swap_used_mb",
    "disk_free_gb", "cap_cores", "bw_mem_mb", "bw_swap_mb", "bw_peak_mb",
)  # fmt: skip
INTERVAL_S = 15
KEEP = 40_320  # 7 days at 15 s
SLACK = 1_000  # trim in chunks, like hwmon
MAX_TAIL = KEEP  # what `tail` returns at most (a direct file read)
SOCKET_MAX = 5_760  # what the socket op returns at most: 1 day; more would stall the daemon loop


def _cell(v) -> str:
    return "" if v is None else str(v)


def row_for(s: dict, bw: dict) -> list[str]:
    """One row from a status sample and the build-work.slice readings (`mem`, `swap`, `peak`: MB or None)."""
    cap = (s.get("cgroups") or {}).get("build-work.slice") or {}
    return [_cell(v) for v in (
        s["at"], s["level"], s["cpu"]["busy_pct"], s["cpu"]["psi_some_avg60"], s["memory"]["psi_some_avg60"],
        s["io"]["psi_some_avg60"], s["memory"]["avail_mb"], s["swap"]["used_mb"], s["disk"]["free_gb"],
        cap.get("cores"), bw.get("mem"), bw.get("swap"), bw.get("peak"),
    )]  # fmt: skip


class History:
    def __init__(
        self,
        path: str,
        clock: Callable[[], float],
        interval_s: float = INTERVAL_S,
        keep: int = KEEP,
        slack: int = SLACK,
    ):
        self.path, self.clock, self.interval_s, self.keep, self.slack = path, clock, interval_s, keep, slack
        self._last: float | None = None
        self._rows: int | None = None  # data rows in the file, counted on first use

    def _count(self) -> int:
        """Data rows in the file; a last line torn by a crash is ended so the next row starts clean."""
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except OSError:
            return 0
        if data and not data.endswith(b"\n"):
            with open(self.path, "ab") as f:
                f.write(b"\n")
        return max(data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1) - 1, 0)

    def record(self, row: list[str]) -> bool:
        """Append `row` unless one was written less than `interval_s` ago. True when written."""
        now = self.clock()
        if self._last is not None and now - self._last < self.interval_s:
            return False
        if self._rows is None:
            self._rows = self._count()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a") as f:
            if new:
                f.write(",".join(COLUMNS) + "\n")
                self._rows = 0
            f.write(",".join(row) + "\n")
        self._last = now
        self._rows += 1
        if self._rows > self.keep + self.slack:
            self._trim()
        return True

    def _trim(self) -> None:
        try:
            with open(self.path) as f:
                lines = f.readlines()
            atomic_write_text(self.path, "".join(lines[:1] + lines[-self.keep :]))
            self._rows = self.keep
        except OSError:
            pass  # the next row tries again

    def tail(self, n: int) -> list[list[str]]:
        """The newest `n` complete rows (a torn last line after a crash is skipped), oldest first."""
        n = max(0, min(n, MAX_TAIL))
        try:
            with open(self.path) as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        rows = [ln.split(",") for ln in lines[1:]]
        rows = [r for r in rows if len(r) == len(COLUMNS)]
        return rows[len(rows) - n :] if n else []
