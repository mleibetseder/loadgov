"""Read-only process and cgroup tree helpers over `HostFS` (the fixture tree in unit tests)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from loadgov.host.procfs import HostFS
from loadgov.host.signals import parse_stat


@dataclass(frozen=True)
class P:
    pid: int
    ppid: int
    comm: str
    state: str
    start: int
    rss_mb: int


def read_proc(fs: HostFS, pid: int) -> P | None:
    try:
        stat = fs.proc(f"{pid}/stat")
        st = parse_stat(stat)
        if st is None:
            return None
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        rss_kb = 0
        try:
            for line in fs.proc(f"{pid}/status").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        except OSError:
            pass
    except (OSError, ValueError, IndexError):
        return None  # exited between listdir and read
    return P(pid, ppid, st[0], st[1], st[2], rss_kb // 1024)


def table(fs: HostFS) -> dict[int, P]:
    return {p.pid: p for p in (read_proc(fs, pid) for pid in fs.pids()) if p}


def children(tab: dict[int, P]) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for p in tab.values():
        out.setdefault(p.ppid, []).append(p.pid)
    return out


def descendants(tab: dict[int, P], kids: dict[int, list[int]], root: int) -> list[P]:
    """Every descendant of `root` (not `root` itself)."""
    out, todo, seen = [], list(kids.get(root, [])), {root}
    while todo:
        pid = todo.pop()
        if pid in seen or pid not in tab:
            continue
        seen.add(pid)
        out.append(tab[pid])
        todo.extend(kids.get(pid, []))
    return out


def ancestors(tab: dict[int, P], pid: int) -> set[int]:
    out, cur = set(), tab[pid].ppid if pid in tab else 0
    while cur > 1 and cur in tab and cur not in out:
        out.add(cur)
        cur = tab[cur].ppid
    return out


def cmdline(fs: HostFS, pid: int) -> list[str] | None:
    try:
        raw = fs.proc_bytes(f"{pid}/cmdline")
    except OSError:
        return None
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def cwd(fs: HostFS, pid: int) -> str | None:
    try:
        return os.readlink(os.path.join(fs.proc_root, str(pid), "cwd"))
    except OSError:
        return None


def cgroup_of(fs: HostFS, pid: int) -> str | None:
    try:
        for line in fs.proc(f"{pid}/cgroup").splitlines():
            if line.startswith("0::"):
                return line[3:].strip()
    except OSError:
        pass
    return None


def scope_pids(fs: HostFS, cgroup: str) -> list[int]:
    """Pids in the cgroup and in every nested cgroup below it (`cgroup.procs`)."""
    root = os.path.join(fs.cgroup_root, cgroup.strip("/"))
    out: list[int] = []
    for dirpath, _dirs, files in os.walk(root):
        if "cgroup.procs" not in files:
            continue
        try:
            with open(os.path.join(dirpath, "cgroup.procs")) as f:
                out.extend(int(x) for x in f.read().split() if x.isdigit())
        except OSError:
            continue
    return out
