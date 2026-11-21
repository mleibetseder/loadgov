"""Finish and start estimates from the median duration of past jobs. Pure functions, no clock reads.

Unknown stays unknown: an estimate that depends on something unknown is None, never a guess.
"""

from __future__ import annotations

from collections.abc import Sequence


def finish_estimate(since: float, expected_s: float | None, now: float) -> tuple[float | None, bool]:
    """(expected finish time, overdue) of a job that started at `since`. Without an expected duration
    both are unknown (None, False). Overdue keeps the past finish time; consumers show "overdue"."""
    if expected_s is None:
        return None, False
    eta = since + expected_s
    return eta, now - since > expected_s


def queue_starts(free_at: float | None, expected: Sequence[float | None]) -> list[float | None]:
    """Expected start time of each waiter, front first. `free_at` is when the slot is expected free
    (None = unknown); each waiter starts when the one ahead of it is expected to finish, so one unknown
    makes every start behind it unknown."""
    out: list[float | None] = []
    t = free_at
    for e in expected:
        out.append(t)
        t = None if t is None or e is None else t + e
    return out
