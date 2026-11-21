"""Resource use of a job: its own scope, the whole host and the Docker scopes attributed to it.

A job's scope cgroup only holds the processes it started. Image builds and containers live elsewhere
(`/system.slice/docker-<id>.scope`), so the scope's `cpu.stat` alone says almost nothing about a gate that
builds an image. `UsageMeter.sample()` therefore also integrates `usage_usec` deltas of every Docker scope
over the job's lifetime. Every read is one small cgroup file; nothing runs when no job is registered.

Attribution of a Docker scope's CPU delta (per sample):
  1. project match: the container's compose label `com.docker.compose.project` equals the job's project,
     or its name starts with `<project>-`, or it matches the project's memguard `containers` regex. Several
     matching jobs split the delta equally. A project match always wins over rule 2.
  2. heavy-slot fallback: the job that holds the heavy slot (one at a time) takes the delta that no project
     matched, including BuildKit builders and containers whose config cannot be read.
A delta that nothing claims is dropped. CPU a container used between its last sample and its removal is
not seen (the error is at most one sampling interval).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

DOCKER_SLICE = "system.slice"
DOCKER_ROOT = "/var/lib/docker/containers"
_SCOPE = re.compile(r"^docker-([0-9a-f]{64})\.scope$")
EVERY_S = 5.0  # the 1 s job tick samples at most this often; job start and end always do
COMPOSE_LABEL = "com.docker.compose.project"


def usage_usec(text: str | None) -> int | None:
    """`usage_usec` of a cgroup v2 `cpu.stat`, or None when unreadable."""
    for line in (text or "").splitlines():
        k, _, v = line.partition(" ")
        if k == "usage_usec" and v.strip().isdigit():
            return int(v)
    return None


def read_int(text: str | None) -> int | None:
    t = (text or "").strip()
    return int(t) if t.isdigit() else None


class UsageMeter:
    def __init__(self, fs, jobs: Callable[[], dict], pattern: Callable[[str], str | None] | None = None,
                 docker_root: str = DOCKER_ROOT, reader: Callable | None = None,
                 clock: Callable[[], float] = time.time, every_s: float = EVERY_S):  # fmt: skip
        self.fs, self.jobs, self.clock, self.every_s, self.at = fs, jobs, clock, every_s, None
        self.pattern = pattern or (lambda project: None)  # project -> memguard `containers` regex
        self.docker_root = docker_root
        self.read = reader or fs.cgroup  # (cgroup path, file name) -> text or None
        self.last: dict[str, int] = {}  # docker scope id -> usage_usec at the previous sample
        self.meta: dict[str, tuple[str, str | None]] = {}  # id -> (name, compose project)
        self.primed = False

    def _meta(self, cid: str) -> tuple[str, str | None] | None:
        """(name, compose project) from the container's config.v2.json; None when unreadable (not cached)."""
        if cid in self.meta:
            return self.meta[cid]
        try:
            with open(f"{self.docker_root}/{cid}/config.v2.json") as f:
                raw = json.load(f)
            name = str(raw.get("Name") or "").lstrip("/")
            labels = (raw.get("Config") or {}).get("Labels") or {}
            self.meta[cid] = (name, labels.get(COMPOSE_LABEL))
        except (OSError, ValueError, AttributeError):
            return None
        return self.meta[cid]

    def _matches(self, project: str, meta: tuple[str, str | None] | None) -> bool:
        if meta is None:
            return False
        name, compose = meta
        if compose == project or name.startswith(f"{project}-"):
            return True
        rx = self.pattern(project)
        try:
            return bool(rx and re.search(rx, name))
        except re.error:
            return False

    def _docker_deltas(self) -> dict[str, int]:
        now: dict[str, int] = {}
        entries = self.fs.list_cgroup(DOCKER_SLICE)
        if self.primed and not entries:
            return {}  # a failed or empty listing: keep the counters, or the next one charges lifetime CPU
        for entry in entries:
            m = _SCOPE.match(entry)
            u = usage_usec(self.read(f"{DOCKER_SLICE}/{entry}", "cpu.stat")) if m else None
            if m and u is not None:
                now[m.group(1)] = u
        deltas = {c: max(0, u - self.last.get(c, 0)) for c, u in now.items()} if self.primed else {}
        self.last = now
        self.meta = {c: v for c, v in self.meta.items() if c in now}
        return deltas

    def sample(self, force: bool = True) -> None:
        """Read the host, the Docker scopes and every job scope; integrate the deltas into the jobs.
        With no job the baseline is dropped, so a later first sample only primes it."""
        jobs = list(self.jobs().values())
        if not jobs:
            self.primed, self.last, self.at = False, {}, None
            return
        now = self.clock()
        if not force and self.at is not None and now - self.at < self.every_s:
            return
        self.at = now
        deltas = self._docker_deltas()
        self.primed = True
        host = usage_usec(self.read("/", "cpu.stat"))
        for j in jobs:
            if host is not None:
                j.host_us = host
                if j.host_us0 is None:
                    j.host_us0 = host
            own = usage_usec(self.read(j.cgroup, "cpu.stat"))
            if own is not None:
                j.cpu_us = own
            peak = read_int(self.read(j.cgroup, "memory.peak"))
            if peak is not None:
                j.peak_b = peak
        heavy = next((j for j in jobs if j.slot == "heavy"), None)
        for cid, delta in deltas.items():
            if not delta:
                continue
            meta = self._meta(cid)
            mine = [j for j in jobs if self._matches(j.project, meta)] or ([heavy] if heavy else [])
            for j in mine:
                j.docker_us += delta / len(mine)

    def finish(self, j, now: float) -> dict:
        """The job's totals at its end: the live reads where the scope still answers, else the last sample."""
        self.sample()  # flushes docker deltas to every job, including this one (it is still registered)
        return {
            "duration_s": round(max(0.0, now - j.since), 1),
            "cpu_s": _s(j.cpu_us),
            "mem_peak_mb": None if j.peak_b is None else round(j.peak_b / 2**20, 1),
            "host_cpu_s": None if j.host_us is None or j.host_us0 is None else _s(j.host_us - j.host_us0),
            "docker_cpu_s": _s(j.docker_us),
        }


def _s(usec: float | None) -> float | None:
    return None if usec is None else round(usec / 1e6, 3)
