"""Signal one process safely: a pidfd pins it, its start time is re-checked, `claude` is never a target.

Every signal that stops or ends a process (`SIGSTOP`, `SIGTERM`, `SIGKILL`) needs `enforce`; without it
`send` answers `would` and opens nothing. `SIGCONT` is the undo of a pause an earlier enforce run made, so
it is always sent (a rolled-back `enforce` must still be able to release what it stopped).

The opener, sender and closer are injectable so unit tests never signal a process they did not start.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable

from loadgov.host.procfs import HostFS

AGENT_COMM = "claude"  # AGENTS.md rule 5: never stopped, frozen or killed by the daemon
UNDO = (signal.SIGCONT,)


def parse_stat(text: str | None) -> tuple[str, str, int] | None:
    """(comm, state, start time in ticks = field 22) of a /proc/<pid>/stat line."""
    try:
        text = text or ""
        lpar, rpar = text.index("("), text.rindex(")")
        rest = text[rpar + 2 :].split()
        return text[lpar + 1 : rpar], rest[0], int(rest[19])
    except (ValueError, IndexError):
        return None


class Signals:
    def __init__(
        self,
        fs: HostFS,
        enforce: bool = False,
        opener: Callable[[int], int] = os.pidfd_open,
        sender: Callable[[int, int], None] = signal.pidfd_send_signal,
        closer: Callable[[int], None] = os.close,
    ):
        self.fs, self.enforce = fs, enforce
        self._open, self._send, self._close = opener, sender, closer

    def stat(self, pid: int) -> tuple[str, str, int] | None:
        try:
            return parse_stat(self.fs.proc(f"{pid}/stat"))
        except OSError:
            return None

    def alive(self, pid: int, start: int | None) -> bool:
        """Still the same process (start time) and not a zombie."""
        st = self.stat(pid)
        return st is not None and st[1] not in ("Z", "X") and (start is None or st[2] == start)

    def send(self, pid: int, start: int | None, sig: signal.Signals) -> str:
        """`would`, `sent`, `gone` (no such process), `changed` (not the process we recorded), `refused`
        (pid 1, this daemon, or the agent `claude`) or `error: <reason>`."""
        if sig not in UNDO and not self.enforce:
            return "would"
        if pid <= 1 or pid == os.getpid():
            return "refused"
        try:
            fd = self._open(pid)  # the fd pins the process: no pid reuse after the checks below
        except ProcessLookupError:
            return "gone"
        except OSError as e:
            return f"error: {e.strerror}"
        try:
            st = self.stat(pid)
            if st is None or st[1] in ("Z", "X"):
                return "gone"
            if start is not None and st[2] != start:
                return "changed"
            if st[0] == AGENT_COMM and sig not in UNDO:
                return "refused"
            try:
                self._send(fd, sig)
            except ProcessLookupError:
                return "gone"
            except OSError as e:
                return f"error: {e.strerror}"
            return "sent"
        finally:
            self._close(fd)
