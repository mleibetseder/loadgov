"""`docker pause` / `docker unpause` through an injectable runner.

`names()` only lists (`docker ps`); `pause` needs `enforce` (else `would`, the runner is not called);
`unpause` is always allowed (undo of a pause an earlier enforce run made). Container names are
checked against a strict pattern before they reach an argv.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def run_docker(argv: list[str]) -> tuple[int, str]:
    """The one external command of this module: (exit code, stdout); 127 when docker is missing."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return r.returncode, r.stdout


NAMES_TTL_S = 5.0  # the container list is refreshed at most this often


def _spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="docker-ps", daemon=True).start()


class Docker:
    def __init__(
        self,
        runner: Callable[[list[str]], tuple[int, str]] = run_docker,
        enforce: bool = False,
        spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.runner, self.enforce, self.spawn, self.clock = runner, enforce, spawn, clock
        self._lock = threading.Lock()
        self._names: list[str] = []
        self._at: float | None = None
        self._busy = False

    def names(self) -> list[str]:
        """Names of the running containers; empty when docker is unavailable."""
        rc, out = self.runner(["docker", "ps", "--format", "{{.Names}}"])
        return [n for n in out.split() if NAME.match(n)] if rc == 0 else []

    def cached_names(self) -> tuple[list[str], float | None]:
        """The last known container names and their age in seconds (None before the first list finished).

        Never blocks: when the list is older than `NAMES_TTL_S` and no refresh runs, one `docker ps` is
        started in a worker thread (`spawn`), so a slow docker cannot hold the event loop or the guard."""
        with self._lock:
            age = None if self._at is None else self.clock() - self._at
            start = not self._busy and (age is None or age >= NAMES_TTL_S)
            self._busy = self._busy or start
            names = list(self._names)
        if start:
            try:
                self.spawn(self._refresh)  # outside the lock: an inline spawn takes it again
            except Exception as e:  # noqa: BLE001 - e.g. "can't start new thread" under memory thrash
                log.warning("docker ps refresh not started: %s", e)
                with self._lock:
                    self._busy = False  # the next call tries again; the last list keeps being served
            with self._lock:  # an inline refresh has finished: report its list
                age = None if self._at is None else self.clock() - self._at
                names = list(self._names)
        return names, age

    def _refresh(self) -> None:
        try:
            names = self.names()
        except Exception:  # noqa: BLE001 - a worker must never die with the flag set
            names = None
        with self._lock:
            if names is not None:
                self._names, self._at = names, self.clock()
            self._busy = False

    def pause(self, name: str) -> str:
        if not self.enforce:
            return "would"
        return self._do("pause", name)

    def unpause(self, name: str) -> str:
        return self._do("unpause", name)

    def _do(self, verb: str, name: str) -> str:
        if not NAME.match(name):
            return "refused"
        rc, _ = self.runner(["docker", verb, name])
        return "ok" if rc == 0 else f"error: docker {verb} exit {rc}"
