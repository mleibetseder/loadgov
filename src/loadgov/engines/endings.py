"""How jobs ended: the cause rule and a bounded ring of recent endings (see docs/protocol.md, Job endings).

Exactly one `cause` per ended job, decided by `classify` in a fixed order (first match wins):
killed_by_memguard, oom_killed, light_overrun, timeout, cancelled, then the exit code (ok / failed), and
without a code `reported_without_code` (a `job_end` from an old client) or `scope_empty_unknown` (the tick
ended the job and no report arrived).
"""

from __future__ import annotations

from collections import deque

CAUSES = (
    "ok", "failed", "killed_by_memguard", "oom_killed", "light_overrun", "timeout", "cancelled",
    "scope_empty_unknown", "reported_without_code",
)  # fmt: skip
# Causes whose memory.peak is only a lower bound: the compile was stopped before it reached its real peak
# (ILM-36). `failed` and `reported_without_code` stay complete: the process ended by itself.
INCOMPLETE_PEAK = frozenset(
    {"killed_by_memguard", "oom_killed", "light_overrun", "timeout", "cancelled", "scope_empty_unknown"}
)
RING_MAX = 500
FIELDS = ("project", "kind", "label", "key", "unit", "ended_at", "duration_s", "exit_code", "signal", "cause")


def classify(
    *,
    memguard: bool = False,
    oom: bool = False,
    overrun: str | None = None,
    timeout: bool = False,
    cancelled: bool = False,
    exit_code: int | None = None,
    reported: bool = False,
) -> str:
    """The one cause of an ending. `reported` is true when a `job_end` request arrived."""
    if memguard:
        return "killed_by_memguard"
    if oom:
        return "oom_killed"
    if overrun:
        return "light_overrun"
    if timeout:
        return "timeout"
    if cancelled:
        return "cancelled"
    if exit_code is not None:
        return "ok" if exit_code == 0 else "failed"
    return "reported_without_code" if reported else "scope_empty_unknown"


class Endings:
    """The last `RING_MAX` endings, oldest first; saved in state.json."""

    def __init__(self):
        self.ring: deque[dict] = deque(maxlen=RING_MAX)

    def add(self, rec: dict) -> None:
        self.ring.append({f: rec.get(f) for f in FIELDS})

    def find(self, unit: str) -> dict | None:
        return next((r for r in reversed(self.ring) if r["unit"] == unit), None)

    def recent(self, since: float | None = None, limit: int = RING_MAX) -> list[dict]:
        """Newest first; `since` is an epoch second (endings before it are left out)."""
        out = [r for r in reversed(self.ring) if since is None or r["ended_at"] >= since]
        return out[:limit]

    def to_state(self) -> list[dict]:
        return list(self.ring)

    def restore(self, raw) -> None:
        for r in raw if isinstance(raw, list) else []:
            try:
                rec = {f: r[f] for f in FIELDS}
                if rec["cause"] not in CAUSES or not isinstance(rec["ended_at"], (int, float)):
                    continue
                self.ring.append(rec)
            except (KeyError, TypeError):
                continue  # an unreadable entry is dropped; the rest is kept
