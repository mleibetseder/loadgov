"""Orphan reaper: a paused tree whose owning agent session is gone is released and terminated.

Pure planning over the paused registry. A tree is orphaned when the session recorded with its entry (the
`claude -p` / implementer shell pid for a legacy tree, the `loadctl run` pid for a job scope, plus start
time) no longer exists; nobody is left to resume it and it would hold its memory forever. The plan is
three steps per entry: `term` (thaw / SIGCONT, then SIGTERM), `kill` (SIGKILL when targets are still
alive 60 s after the SIGTERM) and `done` (no target is alive: the entry is dropped). Processes that are
stopped in a governed cgroup but are not in the registry are never touched, only reported.
"""

from __future__ import annotations

from collections.abc import Callable

from loadgov.engines.memguard import Entry

KILL_AFTER_S = 60.0
MAX_TERM_TRIES = 10  # rounds with no target signalled (refused / changed) before the entry is given up


def plan(
    now: float,
    entries: list[Entry],
    session_alive: Callable[[dict], bool],
    targets_alive: Callable[[Entry], bool],
) -> list[tuple[str, Entry]]:
    out = []
    for e in entries:
        if not e.real or e.session is None:
            continue
        if e.term_at is None:
            if not session_alive(e.session):
                out.append(("term", e))
        elif not targets_alive(e):
            out.append(("done", e))
        elif not e.killed and now - e.term_at >= KILL_AFTER_S:
            out.append(("kill", e))
    return out
