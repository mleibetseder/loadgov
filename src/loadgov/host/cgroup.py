"""Read and write `cpu.max` and `cgroup.freeze` of a cgroup below `HostFS.cgroup_root`.

Writing needs an existing file (it never creates one). `cpu.max` only ever stores `"<int> 100000\n"`: the
value `max` is refused, because the daemon leaves the last cap in place and never lifts it (ADR-0001).
`cgroup.freeze` is written `1` only with `enforce` (otherwise `write_freeze` answers `would` and touches
nothing); writing `0` (thaw) is always allowed: it undoes a pause that only an earlier enforce run made.
The memory backstop helpers (`write_memory_limit`, `clear_memory_limit`, `write_oom_group`) are called only
under `[backstop] enabled` (or to undo an earlier enabled run); only `clear_memory_limit` writes `max`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from loadgov.host.procfs import HostFS

PERIOD_US = 100_000


@dataclass(frozen=True)
class CpuMax:
    quota: int | None  # microseconds per period; None = `max` (unlimited)
    period: int

    @property
    def cores(self) -> float | None:
        return None if self.quota is None else self.quota / self.period


def _path(fs: HostFS, cgroup: str, name: str = "cpu.max") -> str:
    rel = cgroup.strip("/")
    if not rel or ".." in rel.split("/") or "\0" in rel:
        raise ValueError(f"bad cgroup path {cgroup!r}")
    return os.path.join(fs.cgroup_root, rel, name)


def parse_cpu_max(text: str) -> CpuMax:
    """`<quota|max> <period>`; a missing period means the default 100000."""
    parts = text.split()
    if not 1 <= len(parts) <= 2:
        raise ValueError(f"bad cpu.max {text!r}")
    period = int(parts[1]) if len(parts) == 2 else PERIOD_US
    if period <= 0:
        raise ValueError(f"bad cpu.max {text!r}")
    return CpuMax(None if parts[0] == "max" else int(parts[0]), period)


def read_cpu_max(fs: HostFS, cgroup: str) -> CpuMax | None:
    """None when the cgroup (or its cpu.max) does not exist or cannot be read."""
    try:
        with open(_path(fs, cgroup)) as f:
            return parse_cpu_max(f.read())
    except (OSError, ValueError):
        return None


def cap_quota(cores: float) -> int:
    """`cores x 100000`, as the legacy script computed it."""
    quota = round(cores * PERIOD_US)
    if quota <= 0:
        raise ValueError(f"cap must be positive, got {cores!r}")
    return quota


def write_cpu_max(fs: HostFS, cgroup: str, cores: float) -> None:
    """Set the cap with one write of `"<quota> 100000\\n"`. Raises OSError when the file is missing."""
    data = f"{cap_quota(cores)} {PERIOD_US}\n".encode()
    fd = os.open(_path(fs, cgroup), os.O_WRONLY | os.O_TRUNC)  # no O_CREAT: never creates a file
    try:
        if os.write(fd, data) != len(data):
            raise OSError("short write to cpu.max")
    finally:
        os.close(fd)


def read_freeze(fs: HostFS, cgroup: str) -> bool | None:
    """`cgroup.freeze` as a bool; None when the cgroup or the file is gone."""
    try:
        with open(_path(fs, cgroup, "cgroup.freeze")) as f:
            return f.read().strip() == "1"
    except (OSError, ValueError):
        return None


def write_freeze(fs: HostFS, cgroup: str, frozen: bool, enforce: bool) -> str:
    """Freeze (needs `enforce`: else `would`, nothing is written) or thaw (always) a cgroup tree.

    Raises OSError when `cgroup.freeze` is missing; it is never created.
    """
    if frozen and not enforce:
        return "would"
    fd = os.open(_path(fs, cgroup, "cgroup.freeze"), os.O_WRONLY)  # no O_CREAT, no O_TRUNC
    try:
        if os.write(fd, b"1\n" if frozen else b"0\n") < 1:
            raise OSError("short write to cgroup.freeze")
    finally:
        os.close(fd)
    return "ok"


def read_int(fs: HostFS, cgroup: str, name: str) -> int | None:
    """A single-integer cgroup file (`memory.current`, `memory.peak`, ...); None when missing or `max`."""
    try:
        with open(_path(fs, cgroup, name)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def read_events(fs: HostFS, cgroup: str, name: str = "memory.events") -> dict[str, int] | None:
    """A flat-keyed file (`memory.events`): `{"oom_kill": 3, ...}`; None when the file is gone."""
    try:
        with open(_path(fs, cgroup, name)) as f:
            return {k: int(v) for k, v in (line.split() for line in f if len(line.split()) == 2)}
    except (OSError, ValueError):
        return None


def _write_file(fs: HostFS, cgroup: str, name: str, data: bytes) -> None:
    fd = os.open(_path(fs, cgroup, name), os.O_WRONLY | os.O_TRUNC)  # no O_CREAT: never creates a file
    try:
        if os.write(fd, data) != len(data):
            raise OSError(f"short write to {name}")
    finally:
        os.close(fd)


def write_memory_limit(fs: HostFS, cgroup: str, name: str, nbytes: int) -> None:
    """Write `memory.max` or `memory.swap.max` as a positive byte count (a number, never `max`)."""
    if name not in ("memory.max", "memory.swap.max") or nbytes < 0 or (nbytes == 0 and name == "memory.max"):
        raise ValueError(f"bad memory limit {name}={nbytes!r}")
    _write_file(fs, cgroup, name, f"{nbytes}\n".encode())


def clear_memory_limit(fs: HostFS, cgroup: str, name: str) -> None:
    """The backstop rollback: write `max` back. The only place that ever writes `max` to a memory file."""
    if name not in ("memory.max", "memory.swap.max"):
        raise ValueError(f"bad memory limit file {name!r}")
    _write_file(fs, cgroup, name, b"max\n")


def write_oom_group(fs: HostFS, cgroup: str) -> None:
    """`memory.oom.group = 1`: an OOM kill in this cgroup kills every process of it together."""
    _write_file(fs, cgroup, "memory.oom.group", b"1\n")
