"""Fixture host trees: a fake /proc, /sys/fs/cgroup and /run/lock built per test under tmp_path."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from loadgov.config import Config, parse_config
from loadgov.host.procfs import HostFS

PARITY_TOML = Path(__file__).with_name("parity.toml")


def parity_config(**overrides) -> Config:
    """Config with the legacy-parity values (generic project names) from tests/parity.toml."""
    return dataclasses.replace(parse_config(PARITY_TOML.read_text()), **overrides)


CORES = 6
MB = 1024  # kB per MB


def stat_line(total: int, idle: int) -> str:
    # user nice system idle iowait irq softirq steal guest guest_nice; busy = total - idle
    busy = total - idle
    return f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0 0\n"


@dataclass
class FakeProc:
    pid: int
    comm: str
    state: str = "S"
    cpu_ticks: int = 0
    rss_mb: int = 10
    cgroup: str = "/system.slice/x.service"
    cmdline: str = ""
    lock_fd: bool = False  # has fd 9 open on the heavy-slot lock file (the legacy bash protocol)


@dataclass
class Scenario:
    busy_pct: float = 10.0
    load1: float = 0.5
    load5: float = 0.5
    avail_mb: int = 12000
    total_mb: int = 16232
    swap_total_mb: int = 8191
    swap_used_mb: int = 0
    cpu_psi: tuple[float, float] = (1.0, 1.0)
    mem_psi: tuple[float, float, float] = (0.0, 0.0, 0.0)
    io_psi: tuple[float, float, float] = (0.0, 0.0, 0.0)
    disk_free_gb: float = 48.0
    procs: list[FakeProc] = field(default_factory=list)
    cpu_max: dict[str, str] = field(default_factory=lambda: {"loop-a": "150000 100000"})
    holder_line: str | None = None
    flock_pid: int | None = None


SCENARIOS = {
    "calm": Scenario(),
    "busy": Scenario(
        busy_pct=82.0,
        load1=5.4,
        load5=4.8,
        cpu_psi=(48.0, 45.0),
        avail_mb=7000,
        procs=[
            FakeProc(101, "rustc", "R", 9000, 6800, "/loop-a", "rustc --crate-name proj_a_backend"),
            FakeProc(102, "node", "R", 4000, 900, "/loop-a", "node vitest"),
            FakeProc(
                103, "java", "S", 2000, 2500, "/user.slice", "java org.gradle.launcher.daemon.GradleDaemon"
            ),
            FakeProc(104, "bash", "S", 1, 4, "/loop-a", "bash gate.sh", lock_fd=True),
            FakeProc(105, "bash", "S", 1, 4, "/proj-b", "bash heavy-slot waiter", lock_fd=True),
        ],
        # Legacy bash protocol: the flock(1) helper that took the lock has exited, so /proc/locks
        # shows nothing; the gate shell named in the holder line keeps the lock on fd 9.
        holder_line="proj-a gate job-229 1 pid=104 since=2026-11-10T18:00:00Z",
    ),
    "swap-surge": Scenario(busy_pct=55.0, avail_mb=4500, swap_used_mb=7600, mem_psi=(12.0, 9.0, 4.0)),
    "low-mem": Scenario(
        busy_pct=40.0,
        avail_mb=2900,
        swap_used_mb=3000,
        mem_psi=(30.0, 22.0, 15.0),
        procs=[
            FakeProc(201, "cargo", "T", 100, 400, "/loop-a", "cargo test"),
            FakeProc(202, "claude", "S", 500, 600, "/loop-a", "claude -p"),
        ],
    ),
}


def _w(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class FakeHost:
    """Builds a fixture tree for a scenario; `fs` is the HostFS pointed at it."""

    def __init__(self, root: str, sc: Scenario):
        self.root, self.sc = root, sc
        self.proc = os.path.join(root, "proc")
        self.cgroup = os.path.join(root, "cgroup")
        self.lock = os.path.join(root, "lock")
        for d in (self.proc, self.cgroup, self.lock):
            os.makedirs(d, exist_ok=True)
        self.prev_stat = stat_line(100_000, 50_000)
        # second reading: 10_000 more jiffies, busy share = busy_pct
        idle_delta = round(10_000 * (100 - sc.busy_pct) / 100)
        _w(f"{self.proc}/stat", stat_line(110_000, 50_000 + idle_delta))
        _w(f"{self.proc}/loadavg", f"{sc.load1} {sc.load5} 1.00 2/900 12345\n")
        swap_free = sc.swap_total_mb - sc.swap_used_mb
        _w(
            f"{self.proc}/meminfo",
            f"MemTotal: {sc.total_mb * MB} kB\nMemFree: 100 kB\nMemAvailable: {sc.avail_mb * MB} kB\n"
            f"SwapTotal: {sc.swap_total_mb * MB} kB\nSwapFree: {swap_free * MB} kB\n",
        )
        c10, c60 = sc.cpu_psi
        _w(
            f"{self.proc}/pressure/cpu",
            f"some avg10={c10} avg60={c60} avg300=0.00 total=1\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        )
        for kind, (s10, s60, f60) in (("memory", sc.mem_psi), ("io", sc.io_psi)):
            _w(
                f"{self.proc}/pressure/{kind}",
                f"some avg10={s10} avg60={s60} avg300=0.00 total=1\n"
                f"full avg10=0.00 avg60={f60} avg300=0.00 total=1\n",
            )
        _w(
            f"{self.proc}/uptime", "10100.00 50000.00\n"
        )  # processes start at 0, elapsed 101 s at 100 Hz -> ticks/101
        for p in sc.procs:
            d = f"{self.proc}/{p.pid}"
            # after comm: state, 10 fields, utime, stime, 6 fields, starttime (field 22)
            rest = [p.state] + ["0"] * 10 + [str(p.cpu_ticks), "0"] + ["0"] * 6 + ["0"]
            _w(f"{d}/stat", f"{p.pid} ({p.comm}) " + " ".join(rest) + " 0 0\n")
            _w(f"{d}/status", f"Name:\t{p.comm}\nVmRSS:\t{p.rss_mb * MB} kB\n")
            _w(f"{d}/cgroup", f"0::{p.cgroup}\n")
            with open(f"{d}/cmdline", "wb") as f:
                f.write(p.cmdline.replace(" ", "\0").encode() + b"\0")
            os.makedirs(f"{d}/fd", exist_ok=True)
            os.symlink("/dev/null", f"{d}/fd/0")
            if p.lock_fd:
                os.symlink(f"{self.lock}/host-heavy-slot.lock", f"{d}/fd/9")
        for cg, val in sc.cpu_max.items():
            _w(f"{self.cgroup}/{cg}/cpu.max", val + "\n")
        _w(f"{self.lock}/host-heavy-slot.lock", "")
        if sc.holder_line:
            _w(f"{self.lock}/host-heavy-slot.holder", sc.holder_line + "\n")
        locks = "1: POSIX  ADVISORY  WRITE 1 00:1a:999 0 EOF\n"
        if sc.flock_pid:
            st = os.stat(f"{self.lock}/host-heavy-slot.lock")
            ident = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
            locks += f"2: FLOCK  ADVISORY  WRITE {sc.flock_pid} {ident} 0 EOF\n"
            locks += f"2: -> FLOCK  ADVISORY  WRITE 999 {ident} 0 EOF\n"  # blocked waiter
        _w(f"{self.proc}/locks", locks)
        free_blocks = int(sc.disk_free_gb * 2**30 / 4096)
        self.fs = HostFS(
            proc_root=self.proc,
            cgroup_root=self.cgroup,
            lock_root=self.lock,
            disks=("/",),
            cpu_count=CORES,
            clk_tck=100,
            statvfs=lambda _p: os.statvfs_result(
                (4096, 4096, 10**7, free_blocks, free_blocks, 0, 0, 0, 0, 255)
            ),
        )


@pytest.fixture
def fake_host(tmp_path):
    count = iter(range(1000))

    def make(name: str, **overrides) -> FakeHost:
        sc = dataclasses.replace(SCENARIOS[name], **overrides)
        return FakeHost(str(tmp_path / f"{name}-{next(count)}"), sc)

    return make
