"""CPU and memory per project, read from the project's `build-work-<P>.slice` (read-only).

Source: `cpu.stat` `usage_usec` and `memory.current` of the slice. The slice of a project is the parent of a
running job's scope cgroup (the real path, from `/proc/<pid>/cgroup`); with no running job it follows the
systemd nesting rule below the build-work slice (`build-work-web-app.slice` lives in
`build.slice/build-work.slice/build-work-web.slice/build-work-web-app.slice`). A slice's counters include its
child slices, so a project `web` also counts the CPU of a project `web-app`.

`cpu_pct` is the `usage_usec` delta of the slice over the interval since the previous sample (100 = one core)
plus the Docker CPU the usage meter attributed to the project's jobs in the same interval (`Job.docker_us`
deltas; no Docker scope is read here). The first sample, an unreadable file and a counter that went backwards
give null. Docker CPU a job used after the last sample and before it ended is not counted.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from loadgov.engines.usage import EVERY_S, read_int, usage_usec

ROOT = "/build.slice/build-work.slice"
MAX_PROJECTS = 64
DEFAULT_PROJECT = "default"


def slice_leaf(project: str) -> str:
    """The project's slice name without `.slice`, as `loadctl run` names it (same sanitising)."""
    clean = re.sub(r"[^A-Za-z0-9_.]+", "-", project).strip("-.")[:60].strip("-.")
    return f"build-work-{clean or 'default'}"


def nested_slice(project: str, root: str = ROOT) -> str:
    """cgroup path of the project's slice under `root` by systemd's dash-prefix nesting."""
    parts = [p for p in slice_leaf(project).split("-")[2:] if p]
    path = root.rstrip("/")
    for i in range(len(parts)):
        path += f"/build-work-{'-'.join(parts[: i + 1])}.slice"
    return path


class ProjectMeter:
    def __init__(self, fs, jobs: Callable[[], dict], waiting: Callable[[], list[str]],
                 configured: Callable[[], list[str]], clock: Callable[[], float],
                 every_s: float = EVERY_S, limit: int = MAX_PROJECTS):  # fmt: skip
        self.fs, self.jobs, self.waiting, self.configured = fs, jobs, waiting, configured
        self.clock, self.every_s, self.limit = clock, every_s, limit
        self.at: float | None = None
        self.prev_cpu: dict[str, int] = {}  # slice path -> usage_usec at the previous sample
        self.prev_docker: dict[str, float] = {}  # job unit -> docker_us at the previous sample
        self.out: dict[str, dict] = {}

    def summary(self) -> dict:
        return self.out

    def sample(self) -> bool:
        """Refresh the per-project readings at most every `every_s`. True when they were refreshed."""
        now = self.clock()
        if self.at is not None and now - self.at < self.every_s:
            return False
        dt = None if self.at is None else now - self.at
        dt = dt if dt and dt > 0 else None
        self.at = now
        jobs = list(self.jobs().values())
        running: dict[str, list] = {}
        for j in jobs:
            running.setdefault(j.project or DEFAULT_PROJECT, []).append(j)
        waits: dict[str, int] = {}
        for p in self.waiting():
            waits[p or DEFAULT_PROJECT] = waits.get(p or DEFAULT_PROJECT, 0) + 1
        names = sorted(set(running) | set(waits))
        names += [p for p in sorted(set(self.configured())) if p not in running and p not in waits]
        cpu_now: dict[str, int] = {}
        out: dict[str, dict] = {}
        for name in names[: self.limit]:
            mine = running.get(name, [])
            path = os.path.dirname(mine[0].cgroup) if mine else nested_slice(name)
            usec = cpu_now.get(path)
            if usec is None:
                usec = usage_usec(self.fs.cgroup(path, "cpu.stat"))
                if usec is not None:
                    cpu_now[path] = usec
            before = self.prev_cpu.get(path)
            docker_us = sum(max(0.0, j.docker_us - self.prev_docker.get(j.unit, 0.0)) for j in mine)
            cpu = None
            if dt and usec is not None and before is not None and usec >= before:
                cpu = round(((usec - before) + docker_us) / (dt * 1e6) * 100, 1)
            mem = read_int(self.fs.cgroup(path, "memory.current"))
            out[name] = {
                "cpu_pct": cpu,
                "cores": None if cpu is None else round(cpu / 100, 2),
                "docker_cpu_pct": round(docker_us / (dt * 1e6) * 100, 1) if dt else 0,
                "mem_mb": None if mem is None else mem // 2**20,
                "jobs": len(mine),
                "waiting": waits.get(name, 0),
                "slice": path,
            }
        self.prev_cpu = cpu_now
        self.prev_docker = {j.unit: j.docker_us for j in jobs}
        self.out = out
        return True
