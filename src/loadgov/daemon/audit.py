"""Append-only audit log: one JSON object per line in <log-dir>/audit.jsonl, rotated by size.

Rotation (`[audit] max_mb`, `keep`): before a line that would push the file over `max_mb`, `audit.jsonl`
becomes `audit.jsonl.1`, `.1` becomes `.2` and so on up to `keep`; the oldest is dropped. It happens between
lines in the single writer, so no line is lost or split. The current file is renamed first (to `.new`);
when that fails (an append-only `chattr +a` file, a read-only directory) nothing is shifted, so repeated
failures never push the older rotated files out. A failure while shifting puts the file back.
A leftover `.new` (daemon killed between the two renames) is folded into the chain before the next
rotation and at start, never overwritten, and `tail` reads it as the newest rotated file. The file
is opened by path for every line, so a
removal or move by something else (logrotate, an operator) just starts a new file. A rotation error is
logged once and never reaches the caller. New files get mode 0640 (less when the umask says so). `tail`
skips a line that is not valid JSON (a torn write) instead of failing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os

FIELDS = (
    "ts", "op", "principal", "pid", "cgroup", "cmdline", "project", "reason", "request", "result",
    "before", "after", "source", "peer",
)  # fmt: skip

log = logging.getLogger("loadgov.audit")


class AuditLog:
    def __init__(self, log_dir: str, max_mb: int = 50, keep: int = 5):
        self.path = os.path.join(log_dir, "audit.jsonl")
        self.max_mb, self.keep = max_mb, keep
        self._rotate_failed = False
        self._fold_leftover()

    @property
    def _staged(self) -> str:
        return f"{self.path}.new"

    def _fold_leftover(self) -> None:
        if self.max_mb > 0 and os.path.exists(self._staged):
            self._shift_in()

    def configure(self, max_mb: int, keep: int) -> None:
        self.max_mb, self.keep = max_mb, keep
        self._fold_leftover()

    def _rotate_if_needed(self, incoming: int) -> None:
        if self.max_mb <= 0:
            return
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return  # no file yet (or removed): nothing to rotate
        if size == 0 or size + incoming <= self.max_mb * 1024 * 1024:
            return
        staged = self._staged
        if os.path.exists(staged) and not self._shift_in():
            return  # a leftover .new (killed mid-rotation) is never overwritten: fold it in first
        try:
            os.replace(self.path, staged)  # if the current file cannot move, nothing else does
        except OSError as e:
            self._note_failure(e)
            return
        if not self._shift_in():
            with contextlib.suppress(OSError):
                os.replace(staged, self.path)  # keep appending to the same file

    def _shift_in(self) -> bool:
        """Shift `.1`..`.keep-1` up and move the staged `.new` to `.1`; False (logged once) on an error."""
        try:
            for i in range(self.keep - 1, 0, -1):
                if os.path.exists(f"{self.path}.{i}"):
                    os.replace(f"{self.path}.{i}", f"{self.path}.{i + 1}")
            os.replace(self._staged, f"{self.path}.1")
        except OSError as e:
            self._note_failure(e)
            return False
        self._rotate_failed = False
        return True

    def _note_failure(self, e: OSError) -> None:
        if not self._rotate_failed:
            log.error("audit log rotation failed: %s", e)
        self._rotate_failed = True

    def append(self, entry: dict) -> None:
        row = {k: entry.get(k) for k in FIELDS}
        if row["cmdline"]:
            row["cmdline"] = row["cmdline"][:200]
        line = json.dumps(row, separators=(",", ":")) + "\n"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._rotate_if_needed(len(line.encode()))
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        with os.fdopen(fd, "a") as f:
            f.write(line)

    def _lines(self, path: str) -> list[str]:
        try:
            with open(path, errors="replace") as f:
                return f.readlines()
        except FileNotFoundError:
            return []

    def tail(self, n: int) -> list[dict]:
        n = max(n, 0)
        lines = self._lines(self.path)[-n:] if n else []
        older = [self._staged, *(f"{self.path}.{i}" for i in range(1, max(self.keep, 1) + 1))]
        for path in older:  # a leftover .new is the newest rotated file
            if len(lines) >= n:
                break
            lines = self._lines(path)[-(n - len(lines)) :] + lines
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:  # a torn or garbled line: skip it, the rest of the log is still good
                continue
        return out
