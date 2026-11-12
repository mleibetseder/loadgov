"""JVM build-daemon reaper (merges the legacy memory guard and the JVM kill of the `guarded` shim).

A target is a `java` process with exactly one of the two daemon main classes as an argument; nothing
else is ever a target (a file called GradleDaemon.txt, a Gradle client or an IDE JVM are not). Reaping
is SIGTERM through a pidfd with the start time re-checked (`host.signals`); daemons restart on the next
build. Below `REAP_ALL_MB` available (legacy: 1536) all of them are reaped; under memory pressure the
guard reaps the ones outside every registered job scope first.
"""

from __future__ import annotations

from dataclasses import dataclass

MAIN_CLASSES = (
    "org.gradle.launcher.daemon.bootstrap.GradleDaemon",
    "org.jetbrains.kotlin.daemon.KotlinCompileDaemon",
)
REAP_ALL_MB = 1536


@dataclass(frozen=True)
class Jvm:
    pid: int
    start: int
    in_scope: bool  # inside a registered `loadctl run` job scope

    @property
    def key(self) -> tuple[int, int]:
        return (self.pid, self.start)


def is_build_daemon(comm: str, args: list[str]) -> bool:
    return comm == "java" and any(a in MAIN_CLASSES for a in args)
