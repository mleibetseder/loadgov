"""Stray-process detection: orphaned CPU hogs that run outside every governed scope.

Read-only. At each check the CPU share of every process over the interval since the previous check is
computed from its utime+stime ticks, keyed by (pid, start time) so a reused pid is a new process. A process
is *hot* while that share is at least `min_cpu_pct` (percent of one core); `hot_since` is where its current
unbroken run of hot intervals began (a cool interval resets it; the first check only takes the baseline).

A hot process is a *stray* once it has been hot for `min_age_s` and ALL of these hold: it is in no
registered job scope, not under `/system.slice` or `/init.scope`, not in a container scope (docker,
containerd, libpod), not matched by `exclude_cgroups`, not a kernel thread (empty cmdline or ppid 2), not
`claude` and not the daemon, and it is orphaned: no live `claude` among its ancestors AND (its ppid is 1 OR
its environment's CLAUDE_PID names a pid that is dead or is not a `claude`). An unreadable environment
counts as empty.

Signals are not sent here (`daemon/stray_ops.py` does that, owner only and behind `[strays] enforce`).
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable

from loadgov.config import StraysConfig
from loadgov.engines.limits import iso
from loadgov.host import proctree
from loadgov.host.procfs import HostFS
from loadgov.host.signals import AGENT_COMM

CONTAINER_SEGMENTS = ("docker", "containerd", "cri-containerd", "libpod", "crio")


def in_container(cgroup: str) -> bool:
    """A docker / containerd / libpod scope: some path segment is `<runtime>` or `<runtime>-<id>[.scope]`."""
    for seg in cgroup.split("/"):
        base = seg[: -len(".scope")] if seg.endswith(".scope") else seg
        if any(base == r or base.startswith(r + "-") for r in CONTAINER_SEGMENTS):
            return True
    return False


def parse_environ(raw: bytes) -> dict[str, str]:
    out = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep and k:
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


class StrayDetector:
    def __init__(
        self,
        fs: HostFS,
        cfg: StraysConfig,
        project_cwds: dict[str, tuple[str, ...]],
        job_cgroups: Callable[[], list[str]],
        self_pid: Callable[[], int] = os.getpid,
    ):
        self.fs, self.cfg, self.project_cwds = fs, cfg, project_cwds
        self.job_cgroups, self.self_pid = job_cgroups, self_pid
        self._prev_t: float | None = None
        self._prev: dict[tuple[int, int], int] = {}  # (pid, start) -> utime+stime ticks at the last check
        self._hot: dict[tuple[int, int], float] = {}  # (pid, start) -> hot_since

    # ---- one check -------------------------------------------------------------------------------

    def check(self, now: float, proc_ticks: dict[int, tuple[int, int, str, int]]) -> list[dict]:
        """The current strays; `proc_ticks` is `Sampler.proc_ticks` (pid -> (start, ticks, comm, rss_mb))."""
        cur = {(pid, v[0]): v[1] for pid, v in proc_ticks.items()}
        prev_t, prev = self._prev_t, self._prev
        self._prev_t, self._prev = now, cur
        pct: dict[tuple[int, int], float] = {}
        if prev_t is not None and now > prev_t:
            for key, ticks in cur.items():
                if key in prev and ticks >= prev[key]:
                    share = 100.0 * (ticks - prev[key]) / self.fs.clk_tck / (now - prev_t)
                    if share >= self.cfg.min_cpu_pct:
                        pct[key] = share
        self._hot = {k: self._hot.get(k, prev_t) for k in pct}  # a cool interval drops the key: reset
        due = [k for k, since in self._hot.items() if now - since >= self.cfg.min_age_s]
        if not due:
            return []
        tab = proctree.table(self.fs)
        out = []
        for pid, start in sorted(due):
            item = self.classify(tab, pid, start)
            if item:
                since = self._hot[(pid, start)]
                item.update(cpu_pct=round(pct[(pid, start)], 1), hot_since=iso(since), age_s=int(now - since))
                out.append(item)
        return out

    # ---- one process -----------------------------------------------------------------------------

    def excluded(self, cgroup: str) -> bool:
        """Services, containers, init and the configured globs are never strays."""
        if cgroup == "/system.slice" or cgroup.startswith("/system.slice/"):
            return True
        if cgroup == "/init.scope" or cgroup.startswith("/init.scope/"):
            return True
        if in_container(cgroup) or any(fnmatch.fnmatchcase(cgroup, g) for g in self.cfg.exclude_cgroups):
            return True
        return any(cgroup == j or cgroup.startswith(j.rstrip("/") + "/") for j in self.job_cgroups())

    def environ(self, pid: int) -> dict[str, str]:
        try:
            return parse_environ(self.fs.proc_bytes(f"{pid}/environ"))
        except OSError:
            return {}  # unreadable counts as empty

    def project_of(self, cwd: str | None) -> str | None:
        for name, prefixes in self.project_cwds.items():
            for x in prefixes:
                x = x.rstrip("/")
                if cwd and (cwd == x or cwd.startswith(x + "/")):
                    return name
        return None

    def classify(self, tab: dict[int, proctree.P], pid: int, start: int) -> dict | None:
        """The stray record of (pid, start) when every stray condition holds now, else None."""
        p = tab.get(pid)
        if p is None or p.start != start or p.state in ("Z", "X") or pid <= 1:
            return None
        if p.comm == AGENT_COMM or pid == self.self_pid() or p.ppid == 2:
            return None
        args = proctree.cmdline(self.fs, pid)
        if not args:  # empty cmdline: a kernel thread (or a zombie); None: gone
            return None
        cg = proctree.cgroup_of(self.fs, pid)
        if cg is None or self.excluded(cg):  # an unreadable cgroup cannot be checked: not a stray
            return None
        if any(a in tab and tab[a].comm == AGENT_COMM for a in proctree.ancestors(tab, pid)):
            return None  # a live agent still owns it
        env = self.environ(pid)
        raw = env.get("CLAUDE_PID", "")
        claude_pid = int(raw) if raw.isascii() and raw.isdigit() else None
        owner_dead = claude_pid is not None and not (claude_pid in tab and tab[claude_pid].comm == AGENT_COMM)
        if not (p.ppid == 1 or owner_dead):
            return None
        cwd = proctree.cwd(self.fs, pid)
        return {
            "pid": pid, "start_time": start, "comm": p.comm, "cmdline": " ".join(args)[:200], "cwd": cwd,
            "cgroup": cg, "project": self.project_of(cwd), "orphan": True,
            "session": {"claude_pid": claude_pid, "session_id": env.get("CLAUDE_CODE_SESSION_ID")},
        }  # fmt: skip
