"""Who is calling? Owner claims are default-deny.

An `owner: true` request is accepted only when the caller's cgroup is outside every `build-work` slice
(and the configured `[principal] deny_cgroups`), and no ancestor process matches a configured
`[principal] ancestor_deny` pattern (a dispatcher). These checks are audit-grade
evidence, not a security boundary: the socket is root-only and a root caller can lie about its
ancestry.
"""

from __future__ import annotations

import re

from loadgov.config import PrincipalConfig
from loadgov.host.procfs import HostFS

MAX_DEPTH = 64


def cgroup_of(fs: HostFS, pid: int) -> str | None:
    try:
        for line in fs.proc(f"{pid}/cgroup").splitlines():
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        pass
    return None


def cmdline_of(fs: HostFS, pid: int) -> str | None:
    try:
        return fs.proc_bytes(f"{pid}/cmdline").replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return None


def _ppid(fs: HostFS, pid: int) -> int | None:
    try:
        for line in fs.proc(f"{pid}/status").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def caller_info(fs: HostFS, pid: int | None) -> dict:
    if pid is None:
        return {"pid": None, "cgroup": None, "cmdline": None}
    cmd = cmdline_of(fs, pid)
    return {"pid": pid, "cgroup": cgroup_of(fs, pid), "cmdline": cmd[:200] if cmd else cmd}


def owner_denial(fs: HostFS, pid: int | None, cfg: PrincipalConfig | None = None) -> str | None:
    """None when the caller may act as owner, else the reason it may not."""
    if pid is None:
        return "caller unknown (no peer credentials)"
    cg = cgroup_of(fs, pid)
    if cg is None:
        return "caller cgroup unreadable"
    cfg = cfg or PrincipalConfig()
    ancestors = re.compile("|".join(f"(?:{p})" for p in cfg.ancestor_deny)) if cfg.ancestor_deny else None
    if (
        cg.startswith(("/build-work.slice", "/build.slice/build-work.slice", *cfg.deny_cgroups))
        or "build-work-" in cg
    ):
        return f"caller is inside a governed cgroup ({cg})"
    cur = pid
    for _ in range(MAX_DEPTH):  # fail closed: anything unreadable or too deep is a denial
        if cur <= 1:
            return None
        cmd = cmdline_of(fs, cur)
        if cmd is None:
            return f"ancestor pid {cur} unreadable"
        if ancestors and ancestors.search(cmd):
            return f"caller has a dispatcher ancestor (pid {cur})"
        nxt = _ppid(fs, cur)
        if nxt is None:
            return f"ancestor pid {cur} status unreadable"
        cur = nxt
    return "process ancestry too deep"


def is_self_or_descendant(fs: HostFS, pid: int | None, ancestor: int) -> bool:
    """True when `pid` is `ancestor` or runs below it (a `loadctl` child of the holder's shell)."""
    cur = pid
    for _ in range(MAX_DEPTH):
        if cur is None or cur <= 1:
            return False
        if cur == ancestor:
            return True
        cur = _ppid(fs, cur)
    return False
