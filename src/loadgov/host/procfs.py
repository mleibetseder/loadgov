"""Read-only access to /proc, /sys/fs/cgroup and the lock directory, with injectable roots.

Every path the daemon reads goes through HostFS so unit tests can point it at a fixture tree.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


@dataclass
class HostFS:
    proc_root: str = "/proc"
    cgroup_root: str = "/sys/fs/cgroup"
    lock_root: str = "/run/lock"
    disks: tuple[str, ...] = ("/",)
    cpu_count: int = field(default_factory=lambda: os.cpu_count() or 1)
    clk_tck: int = field(default_factory=lambda: os.sysconf("SC_CLK_TCK"))
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs
    stat: Callable[[str], os.stat_result] = os.stat

    def proc(self, rel: str) -> str:
        with open(os.path.join(self.proc_root, rel)) as f:
            return f.read()

    def proc_bytes(self, rel: str) -> bytes:
        with open(os.path.join(self.proc_root, rel), "rb") as f:
            return f.read()

    def local_addresses(self) -> set[str]:
        """Every address this host owns (IPv4 `/32 host LOCAL` in fib_trie, IPv6 in if_inet6).

        Fails closed: raises OSError when the tables cannot be read (callers must then refuse).
        A missing if_inet6 is accepted: a kernel without IPv6 has no such file.
        """
        found: set[str] = set()
        last = None
        for line in self.proc("net/fib_trie").splitlines():
            t = line.strip()
            if t.startswith("|--"):
                last = t[3:].strip()
            elif t.startswith("/32 host LOCAL") and last:
                found.add(last)
        try:
            lines = self.proc("net/if_inet6").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            h = line.split()[0] if line.split() else ""
            if len(h) == 32:
                try:
                    found.add(str(ipaddress.IPv6Address(int(h, 16))))
                except ValueError as e:
                    raise OSError(f"bad if_inet6 line: {line!r}") from e
        return found

    def pids(self) -> Iterator[int]:
        for name in os.listdir(self.proc_root):
            if name.isdigit():
                yield int(name)

    def cgroup(self, cgroup: str, name: str) -> str | None:
        try:
            with open(os.path.join(self.cgroup_root, cgroup.lstrip("/"), name)) as f:
                return f.read()
        except OSError:
            return None

    def scope_procs(self, cgroup: str) -> str | None:
        """`cgroup.procs` of a job scope: None when the cgroup is gone; any other read error raises OSError
        (the caller must not read an unreadable scope as an empty one)."""
        try:
            with open(os.path.join(self.cgroup_root, cgroup.lstrip("/"), "cgroup.procs")) as f:
                return f.read()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def lock_path(self, name: str) -> str:
        return os.path.join(self.lock_root, name)

    def read_lock_file(self, name: str) -> str | None:
        try:
            with open(self.lock_path(name)) as f:
                return f.read()
        except OSError:
            return None
