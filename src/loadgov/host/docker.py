"""`docker pause` / `docker unpause` through an injectable runner.

`names()` only lists (`docker ps`); `pause` needs `enforce` (else `would`, the runner is not called);
`unpause` is always allowed (undo of a pause an earlier enforce run made). Container names are
checked against a strict pattern before they reach an argv.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def run_docker(argv: list[str]) -> tuple[int, str]:
    """The one external command of this module: (exit code, stdout); 127 when docker is missing."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return r.returncode, r.stdout


class Docker:
    def __init__(self, runner: Callable[[list[str]], tuple[int, str]] = run_docker, enforce: bool = False):
        self.runner, self.enforce = runner, enforce

    def names(self) -> list[str]:
        """Names of the running containers; empty when docker is unavailable."""
        rc, out = self.runner(["docker", "ps", "--format", "{{.Names}}"])
        return [n for n in out.split() if NAME.match(n)] if rc == 0 else []

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
