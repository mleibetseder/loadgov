"""Turn the host's /proc and cgroup files into one status sample (read-only)."""

from __future__ import annotations

import calendar
import os
import re
import time
from dataclasses import dataclass

from loadgov.engines.levels import evaluate
from loadgov.host.procfs import HostFS

JVM_DAEMONS = ("GradleDaemon", "KotlinCompileDaemon")
# status key -> cgroup path below the cgroup root. systemd nests a dashed slice name by prefix, so
# build-work.slice lives under build.slice (ADR-0001, "Slice naming"). Mirrored cgroups
# ([compat] mirror_cgroups) are watched too, under their own path as the key.
WATCHED_CGROUPS = {"build-work.slice": "build.slice/build-work.slice"}
STOPPED_UNDER = ("/build.slice/build-work.slice",)
HEAVY_SLOT_LOCK = "host-heavy-slot.lock"
HEAVY_SLOT_HOLDER = "host-heavy-slot.holder"
STUCK_HOLDER_S = 45 * 60


def parse_cpu_times(stat_text: str) -> tuple[int, int]:
    """Total and idle jiffies from the aggregate `cpu` line (idle = idle + iowait). The total sums every
    field after `cpu`, exactly like the legacy governor's awk (`for(i=2;i<=NF;i++) t+=$i`), so `guest` and
    `guest_nice` count too even though the kernel already includes them in `user` and `nice`."""
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            return sum(v), v[3] + v[4]
    raise ValueError("no aggregate cpu line in /proc/stat")


def busy_pct(prev: tuple[int, int], cur: tuple[int, int]) -> float:
    dt, di = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return 0.0
    return round(100.0 * (dt - di) / dt, 1)


def parse_meminfo_mb(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) // 1024
    return out


def parse_pressure(text: str) -> dict[str, float]:
    """`some avg10=…` / `full avg60=…` → {"some_avg10": …, "full_avg60": …}."""
    out = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        for f in fields:
            k, _, v = f.partition("=")
            if k.startswith("avg"):
                out[f"{kind}_{k}"] = float(v)
    return out


def parse_loadavg(text: str) -> tuple[float, float]:
    p = text.split()
    return float(p[0]), float(p[1])


def parse_cpu_max(text: str | None) -> dict | None:
    if text is None:
        return None
    quota, _, period = text.strip().partition(" ")
    period_us = int(period or 100000)
    cores = None if quota == "max" else round(int(quota) / period_us, 2)
    return {"cpu_max": text.strip(), "cores": cores}


_HOLDER_RE = re.compile(r"^(?P<who>.*?)\s+pid=(?P<pid>\d+)(?:\s+since=(?P<since>\S+))?\s*$")


def parse_holder(line: str) -> dict | None:
    m = _HOLDER_RE.match(line.strip())
    if not m:
        return None
    return {"who": m["who"], "pid": int(m["pid"]), "since": m["since"]}


def _since_epoch(since: str | None) -> float | None:
    if not since:
        return None
    try:
        return calendar.timegm(time.strptime(since, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


@dataclass
class Proc:
    pid: int
    comm: str
    state: str
    pcpu: float
    rss_mb: int
    cgroup: str
    cmdline: str
    ticks: int = 0  # utime + stime, for interval CPU shares between two samples
    start: int = 0  # start time in ticks (identifies the process across pid reuse)


def _read_proc(fs: HostFS, pid: int, uptime_s: float) -> Proc | None:
    try:
        stat = fs.proc(f"{pid}/stat")
        status = fs.proc(f"{pid}/status")
        cgroup = fs.proc(f"{pid}/cgroup")
        lpar, rpar = stat.index("("), stat.rindex(")")
        comm = stat[lpar + 1 : rpar]
        # cmdline takes the target's mmap lock and can block on a process stuck in D state under swap
        # pressure, so it is read only where it is needed: telling Gradle/Kotlin daemons from other JVMs.
        cmdline = ""
        if comm == "java":
            cmdline = fs.proc_bytes(f"{pid}/cmdline").replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return None  # process exited between listdir and read
    rest = stat[rpar + 2 :].split()
    state = rest[0]
    utime, stime, start = int(rest[11]), int(rest[12]), int(rest[19])
    # Lifetime CPU share, the same number `ps -o pcpu` prints (what the legacy watcher reported).
    elapsed = uptime_s - start / fs.clk_tck
    pcpu = round(100.0 * (utime + stime) / fs.clk_tck / elapsed, 1) if elapsed > 0 else 0.0
    rss_kb = 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = int(line.split()[1])
            break
    cg = ""
    for line in cgroup.splitlines():
        if line.startswith("0::"):
            cg = line[3:].strip()
    return Proc(pid, comm, state, pcpu, rss_kb // 1024, cg, cmdline.strip(), utime + stime, start)


def _lock_identity(fs: HostFS, lock_name: str) -> tuple[int, int] | None:
    try:
        st = fs.stat(fs.lock_path(lock_name))
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _kernel_lockers(fs: HostFS, ident: tuple[int, int]) -> list[int]:
    """Live pids /proc/locks lists as holding a FLOCK on the file (read-only; never takes the lock).

    Only lockers that are still alive appear: the kernel records the pid that called flock(2) and hides
    the entry once that pid is gone. The legacy bash protocol (`exec 9>>lock; flock -n 9`) locks from
    the short-lived flock(1) helper, so its locks never show here; see _holder_has_lock_open.
    """
    try:
        locks = fs.proc("locks")
    except OSError:
        return []
    dev, ino = ident
    want = f"{os.major(dev):02x}:{os.minor(dev):02x}:{ino}"
    pids = []
    for line in locks.splitlines():
        p = line.split()
        if "->" in p:  # blocked waiter
            continue
        if len(p) >= 6 and p[1] == "FLOCK" and p[5] == want:
            pids.append(int(p[4]))
    return pids


def _holder_has_lock_open(fs: HostFS, pid: int, ident: tuple[int, int]) -> bool:
    """True if `pid` is alive and has a file descriptor open on the lock file (stat of /proc/<pid>/fd/*)."""
    fd_dir = os.path.join(fs.proc_root, str(pid), "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return False
    for fd in fds:
        try:
            st = fs.stat(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == ident:
            return True
    return False


class Sampler:
    """Keeps the previous /proc/stat reading so each sample reports busy % over the last interval."""

    def __init__(self, fs: HostFS | None = None, mirrors: tuple[str, ...] = ()):
        self.fs = fs or HostFS()
        self.watched = {**{m: m for m in mirrors}, **WATCHED_CGROUPS}
        self.stopped_under = tuple(f"/{m}" for m in mirrors) + STOPPED_UNDER
        self._prev_cpu: tuple[int, int] | None = None
        self.cpu_times: tuple[int, int] | None = None  # (total, idle) jiffies of the latest sample
        # pid -> (start, cpu ticks, comm, rss_mb) of the latest sample; the alert engine diffs two of them
        self.proc_ticks: dict[int, tuple[int, int, str, int]] = {}

    def prime(self, stat_text: str | None = None) -> None:
        self._prev_cpu = parse_cpu_times(stat_text if stat_text is not None else self.fs.proc("stat"))

    def sample(self, now: float | None = None) -> dict:
        fs = self.fs
        now = time.time() if now is None else now
        cur = parse_cpu_times(fs.proc("stat"))
        busy = busy_pct(self._prev_cpu, cur) if self._prev_cpu else 0.0
        self._prev_cpu = self.cpu_times = cur
        load1, load5 = parse_loadavg(fs.proc("loadavg"))
        mem = parse_meminfo_mb(fs.proc("meminfo"))
        psi = {k: _safe_pressure(fs, k) for k in ("cpu", "memory", "io")}
        swap_total = mem.get("SwapTotal", 0)
        swap_used = swap_total - mem.get("SwapFree", 0)
        disk_free = min(round(v.f_bavail * v.f_frsize / 2**30, 1) for v in (fs.statvfs(d) for d in fs.disks))
        uptime = float(fs.proc("uptime").split()[0])
        procs = [p for p in (_read_proc(fs, pid, uptime) for pid in fs.pids()) if p]
        self.proc_ticks = {p.pid: (p.start, p.ticks, p.comm, p.rss_mb) for p in procs}

        s = {
            "v": 1,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "at_ms": int(now * 1000),
            "host": {"cores": fs.cpu_count},
            "cpu": {
                "busy_pct": busy,
                "psi_some_avg10": psi["cpu"].get("some_avg10", 0.0),
                "psi_some_avg60": psi["cpu"].get("some_avg60", 0.0),
                "load1": load1,
                "load5": load5,
                "load1_per_core": round(load1 / fs.cpu_count, 2),
            },
            "memory": {
                "avail_mb": mem["MemAvailable"],
                "total_mb": mem["MemTotal"],
                "psi_some_avg10": psi["memory"].get("some_avg10", 0.0),
                "psi_some_avg60": psi["memory"].get("some_avg60", 0.0),
                "psi_full_avg60": psi["memory"].get("full_avg60", 0.0),
            },
            "swap": {
                "total_mb": swap_total,
                "used_mb": swap_used,
                "used_pct": round(100.0 * swap_used / swap_total, 1) if swap_total else 0.0,
            },
            "io": {
                "psi_some_avg10": psi["io"].get("some_avg10", 0.0),
                "psi_some_avg60": psi["io"].get("some_avg60", 0.0),
                "psi_full_avg60": psi["io"].get("full_avg60", 0.0),
            },
            "disk": {"free_gb": disk_free, "paths": list(fs.disks)},
            "top": [
                {
                    "pid": p.pid,
                    "comm": p.comm,
                    "pcpu": p.pcpu,
                    "rss_mb": p.rss_mb,
                    "cgroup": p.cgroup,
                }
                for p in sorted(procs, key=lambda p: p.pcpu, reverse=True)[:3]
            ],
            "jvm_daemons": sum(1 for p in procs if any(d in p.cmdline for d in JVM_DAEMONS)),
            "heavy_slot": self._heavy_slot(now),
            "cgroups": {
                name: parse_cpu_max(fs.cgroup(path, "cpu.max")) for name, path in self.watched.items()
            },
            "stopped": [
                {"pid": p.pid, "comm": p.comm, "cgroup": p.cgroup}
                for p in procs
                if p.state == "T" and p.cgroup.startswith(self.stopped_under)
            ],
        }
        s["levels"], s["level"] = evaluate(s)
        return s

    def _heavy_slot(self, now: float) -> dict:
        """Held = a live kernel-visible locker, or the holder line's pid is alive with the lock file open.

        The legacy holders keep the lock on fd 9 of the gate shell whose pid the holder line names, so
        that pid having the file open is the evidence. Waiters also have the file open, but they are
        never named in the holder line (it is written only after the lock is taken).
        """
        ident = _lock_identity(self.fs, HEAVY_SLOT_LOCK)
        line = (self.fs.read_lock_file(HEAVY_SLOT_HOLDER) or "").strip()
        parsed = parse_holder(line) if line else None
        lockers = _kernel_lockers(self.fs, ident) if ident else []
        holder_pid = None
        if (
            ident
            and parsed
            and (parsed["pid"] in lockers or _holder_has_lock_open(self.fs, parsed["pid"], ident))
        ):
            holder_pid = parsed["pid"]
        held = bool(lockers) or holder_pid is not None
        named = held and holder_pid is not None
        since = _since_epoch(parsed["since"]) if named else None
        age = int(now - since) if since is not None else None
        return {
            "held": held,
            "holder_pid": holder_pid,
            "lock_pids": lockers,
            "holder": line if named else None,
            "last_holder": None if named else (line or None),
            "since": parsed["since"] if named else None,
            "age_s": age,
            "stuck": age is not None and age > STUCK_HOLDER_S,
        }


def _safe_pressure(fs: HostFS, kind: str) -> dict[str, float]:
    try:
        return parse_pressure(fs.proc(f"pressure/{kind}"))
    except OSError:
        return {}
