"""loadctl run: one job in its own systemd scope under build-work-<project>.slice.

    loadctl run --kind impl|review|gate|compile --project P --label L [--floor-mb N --on-floor kill]
                [--no-wait] -- cmd...

loadctl stays the parent of the job: the exit code passes through, SIGINT and SIGTERM are forwarded to
the scope. Order: `gate` takes the heavy slot and `compile` the compile slot first, then both wait
for headroom while holding it; `impl` and `review` only wait for headroom. Under `[slots] enforce`
loadctl opens the lock file itself and the job inherits the descriptor, so the flock lives as long as
loadctl run or the job does and never only in the daemon. The daemon binds the slot to the job's
scope (`job_start`), so it is released when the scope empties, even if loadctl died.

Exit codes: the job's (128+n when killed by signal n), 137 after `--on-floor kill`, 3 daemon not
reachable (the job did not start), 5 daemon error, 2 usage, 1 headroom wait timed out, 75 `--no-wait` and
the host has no room or the slot is busy.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from loadgov.cli import loadctl
from loadgov.cli.client import DaemonUnavailable
from loadgov.config import KINDS

KIND_SLOT = {"gate": "heavy", "compile": "compile"}
GATE_CONFIRM_S = 30.0  # a gate that already holds the slot confirms after 30 s, others use the config (60)
FLOOR_POLL_S = 2.0
LEVEL_POLL_S = 15.0  # legacy guarded polled hwmon about every 15 s
HEADROOM_MODES = ("full", "level", "none")
EXIT_FLOOR, EXIT_BUSY, EXIT_UNSTARTABLE = 137, 75, 127
JVM_PATTERN = r"^[^ ]*/java .*(GradleDaemon|KotlinCompileDaemon)"  # same as legacy guarded
GATE_JOBS_BYTES_MB = 8192  # CARGO_BUILD_JOBS=3 needs GATE_JOBS=3 and at least 8 GB available


# ---- pure helpers ---------------------------------------------------------------------------------


def sanitize(text: str, limit: int = 40) -> str:
    """A fragment of a unit name: letters, digits, '_' and '.' kept, anything else becomes '-'."""
    return re.sub(r"[^A-Za-z0-9_.]+", "-", text).strip("-.")[:limit].strip("-.")


def slice_name(project: str) -> str:
    return f"build-work-{sanitize(project, 60) or 'default'}.slice"


def unit_name(kind: str, label: str, ts: str) -> str:
    return f"loadgov-{kind}-{sanitize(label) or 'job'}-{ts}"


def scope_cgroup(slice_: str, unit: str) -> str:
    """The cgroup path systemd gives a scope: a slice `a-b-c.slice` nests under `a.slice` and `a-b.slice`."""
    parts = slice_.removesuffix(".slice").split("-")
    chain = ["-".join(parts[: i + 1]) + ".slice" for i in range(len(parts))]
    return "/" + "/".join([*chain, f"{unit}.scope"])


def systemd_run_argv(
    unit: str, slice_: str, kind: str, cmd: list[str], rustc_running: bool = False
) -> list[str]:
    """The launcher command. Like the legacy gate wrapper: gates and compiles go to the idle io class while a
    rustc runs, everything else uses best-effort class 2 priority 7. All jobs run at nice 10."""
    io = ["ionice", "-c", "3"] if kind in KIND_SLOT and rustc_running else ["ionice", "-c", "2", "-n", "7"]
    return [
        "systemd-run", "--scope", "--quiet", "--collect", f"--slice={slice_}", f"--unit={unit}", "--",
        "nice", "-n", "10", *io, *cmd,
    ]  # fmt: skip


def job_env(base: dict, effective: dict, kind: str, avail_mb: int, scope_unit: str) -> dict:
    """The profile env for the job, from the effective limits (the caller's own values do not win)."""
    jobs = effective["cargo_build_jobs"]
    if kind == "gate" and base.get("GATE_JOBS") == "3" and avail_mb >= GATE_JOBS_BYTES_MB and jobs >= 2:
        jobs = 3  # never above a restricted profile's 1
    return {
        **base,
        "CARGO_BUILD_JOBS": str(jobs),
        "VITEST_MAX_WORKERS": str(effective["vitest_max_workers"]),
        "GATE_SHARD_BUDGET": str(effective["gate_shard_budget"]),
        "GATE_SHARD_THREADS": base.get("GATE_SHARD_THREADS") or "2",
        "LOADGOV_JOB": scope_unit,
    }


# ---- host seams -----------------------------------------------------------------------------------


def _meminfo_avail_mb(proc_root: str = "/proc") -> int:
    with open(os.path.join(proc_root, "meminfo")) as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    raise OSError("no MemAvailable in meminfo")


def _find_cgroup(pid: int, scope_unit: str, slice_: str, proc_root: str = "/proc", sleep=time.sleep) -> str:
    """The job cgroup from /proc/<pid>/cgroup once systemd moved it into the scope, else the computed one."""
    for _ in range(30):
        try:
            with open(os.path.join(proc_root, str(pid), "cgroup")) as f:
                for line in f:
                    path = line.strip().partition("::")[2]
                    if line.startswith("0::") and path.endswith("/" + scope_unit):
                        return path
        except OSError:
            break
        sleep(0.1)
    return scope_cgroup(slice_, scope_unit.removesuffix(".scope"))


def _rustc_running() -> bool:
    return subprocess.run(["pgrep", "-x", "rustc"], capture_output=True).returncode == 0


def _run_quiet(argv: list[str]) -> int:
    try:
        return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except OSError:
        return 127


def _spawn(argv: list[str], env: dict):
    """Start the launcher. The job inherits the caller's descriptors like the legacy `exec` did (cargo's
    jobserver pipes, the lock fd of a gate), so close_fds is off; loadctl's own descriptors (daemon
    sockets, files) are close-on-exec, and only the lock fd was made inheritable on purpose.

    The job gets its own session (like legacy guarded's setsid): a terminal Ctrl-C reaches only loadctl,
    which forwards one signal through the scope, so the job is never interrupted twice."""
    return subprocess.Popen(argv, env=env, close_fds=False, start_new_session=True)


@dataclass
class Deps:
    """Everything with a side effect on the host; tests replace these."""

    popen: Callable = None  # set below: _spawn
    run: Callable = _run_quiet  # argv -> exit code: systemctl kill, pkill
    sleep: Callable = time.sleep
    now: Callable = time.time
    avail_mb: Callable = _meminfo_avail_mb
    rustc_running: Callable = _rustc_running
    find_cgroup: Callable = _find_cgroup
    pid: int = field(default_factory=os.getpid)

    def __post_init__(self):
        self.popen = self.popen or _spawn


# ---- the command ----------------------------------------------------------------------------------


def add_run_parser(sub) -> None:
    r = sub.add_parser("run", help="run a job in its own systemd scope after the slot and headroom wait")
    r.add_argument("--kind", required=True, choices=KINDS)
    r.add_argument("--project", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--floor-mb", type=int, help="kill the job when MemAvailable falls below this (MB)")
    r.add_argument("--on-floor", choices=["kill"], default="kill")
    r.add_argument(
        "--headroom", choices=HEADROOM_MODES, default="full",
        help="full: 2-sample headroom wait; level: hwmon level OK on 2 samples (legacy guarded); "
        "none: slot only (legacy rustc-slot.sh)",
    )  # fmt: skip
    r.add_argument("--no-wait", action="store_true", help="exit 75 instead of waiting for slot or headroom")
    r.add_argument("--slice", help=argparse.SUPPRESS)  # tests: a private slice instead of build-work-*
    r.add_argument("job_cmd", nargs=argparse.REMAINDER, metavar="cmd", help="-- command and arguments")


def _release(a, slot: str | None, pid: int) -> None:
    if slot:  # when the daemon is down it releases once our pid is gone
        with contextlib.suppress(DaemonUnavailable):
            loadctl.request(a.socket, "slot_release", {"slot": slot, "pid": pid})


def _error(r: dict) -> int:
    print(f"loadctl: {r.get('error')}: {r.get('message')}", file=sys.stderr)
    return loadctl.EXIT_ERROR


def _headroom(a, d: Deps, slot: str | None) -> int:
    if a.headroom == "none":
        return 0
    if a.headroom == "level":  # legacy guarded: wait (forever) for hwmon level OK
        if a.no_wait:
            r = loadctl.request(a.socket, "status")
            if not r.get("ok"):
                return _error(r)
            if r["status"]["level"] == "OK":
                return 0
            print(f"hwmon level {r['status']['level']}: no room, not waiting", file=sys.stderr)
            return EXIT_BUSY
        ns = argparse.Namespace(socket=a.socket, max_s=float("inf"), poll=LEVEL_POLL_S, require="OK")
        return loadctl.level_wait(ns, d.sleep, out=sys.stderr)
    if a.no_wait:
        r = loadctl.request(a.socket, "headroom", {"kind": a.kind, "project": a.project})
        if not r.get("ok"):
            return _error(r)
        if r["decision"]["pass"]:
            return 0
        print(f"HEADROOM_WAIT {a.label} {loadctl.headroom_state(r)}: no room, not waiting", file=sys.stderr)
        return EXIT_BUSY
    ns = argparse.Namespace(
        socket=a.socket, kind=a.kind, project=a.project, label=a.label, timeout=None, poll=None,
        confirm=GATE_CONFIRM_S if a.kind == "gate" and slot else None,
    )  # fmt: skip
    return loadctl.wait_headroom(ns, d.sleep, out=sys.stderr)


def _floor_avail(a, d: Deps) -> int:
    """MemAvailable from the daemon's status, straight from /proc/meminfo when the daemon is down."""
    try:
        r = loadctl.request(a.socket, "status")
        if r.get("ok"):
            return r["status"]["memory"]["avail_mb"]
    except DaemonUnavailable:
        pass
    return d.avail_mb()


def _wait_job(proc, a, d: Deps, scope_unit: str) -> int:
    if a.floor_mb is None:
        return proc.wait()
    while True:
        try:
            return proc.wait(timeout=FLOOR_POLL_S)
        except subprocess.TimeoutExpired:
            pass
        avail = _floor_avail(a, d)
        if avail < a.floor_mb:
            print(
                f"loadctl: only {avail} MB available (floor {a.floor_mb} MB): killing {scope_unit}",
                file=sys.stderr,
            )
            # Only the job's own scope, plus the Gradle/Kotlin daemons that live outside it.
            d.run(["systemctl", "kill", "--signal=SIGKILL", scope_unit])
            d.run(["pkill", "-f", JVM_PATTERN])
            proc.wait()
            return EXIT_FLOOR


def run_command(a, deps: Deps | None = None) -> int:
    d = deps or Deps()
    cmd = a.job_cmd[1:] if a.job_cmd[:1] == ["--"] else list(a.job_cmd)
    if not cmd:
        print(
            "loadctl run: no command (use: loadctl run --kind K --project P --label L -- cmd...)",
            file=sys.stderr,
        )
        return 2
    if a.floor_mb is not None and a.floor_mb <= 0:
        print("loadctl run: --floor-mb must be > 0", file=sys.stderr)
        return 2
    slot = KIND_SLOT.get(a.kind)
    try:
        r = loadctl.request(a.socket, "status")
        if not r.get("ok"):
            return _error(r)
        held = False
        if slot:  # slot first, then the headroom wait while holding it
            ns = argparse.Namespace(
                socket=a.socket, slot=slot, project=a.project, label=a.label, pid=d.pid,
                wait=not a.no_wait, fd=None, open_lock=True, timeout_s=None,
            )  # fmt: skip
            rc = loadctl.slot_acquire(ns, out=sys.stderr, sleep=d.sleep)
            if rc:
                return EXIT_BUSY if rc == 1 else rc
            held = True
        lock_fd = ns.lock_fd if slot else None
        bound = {}
        try:
            return _run_held(a, d, cmd, slot, lock_fd, bound)
        finally:
            if held and not bound.get("slot"):  # a bound slot is released when the scope empties
                _release(a, slot, d.pid)
            if lock_fd is not None:
                os.close(lock_fd)
    except DaemonUnavailable as e:
        print(f"loadctl: {e}", file=sys.stderr)
        return loadctl.EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        return 130


def _run_held(a, d: Deps, cmd: list[str], slot: str | None, lock_fd: int | None, bound: dict) -> int:
    rc = _headroom(a, d, slot)
    if rc:
        return rc
    r = loadctl.request(a.socket, "status")  # fresh limits and memory after a possibly long wait
    if not r.get("ok"):
        return _error(r)
    st = r["status"]
    if (
        a.kind == "gate"
    ):  # idle JVM build daemons hold gigabytes: reap them when memory is short (enforce only)
        with contextlib.suppress(DaemonUnavailable):
            loadctl.request(a.socket, "reap", {"pre_gate": True, "reason": f"pre-gate {a.label}"})
    ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(d.now())) + f"-{d.pid}"
    unit = unit_name(a.kind, a.label, ts)
    scope_unit = unit + ".scope"
    slice_ = a.slice or slice_name(a.project)
    argv = systemd_run_argv(unit, slice_, a.kind, cmd, d.rustc_running() if slot else False)
    env = job_env(os.environ, st["limits"]["effective"], a.kind, st["memory"]["avail_mb"], scope_unit)
    caught: list[int] = []

    def kill_scope(signum: int) -> None:
        d.run(["systemctl", "kill", f"--signal={signal.Signals(signum).name}", scope_unit])

    def forward(signum, _frame):  # the scope, not just systemd-run's pid, gets the signal
        caught.append(signum)
        kill_scope(signum)

    # Handlers go in before the launcher starts (exec resets them in the child), so there is no window in
    # which a signal kills loadctl and leaves an unregistered job behind.
    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old[sig] = signal.signal(sig, forward)
        except ValueError:  # not the main thread (tests): no forwarding
            break
    registered = False
    try:
        try:
            proc = d.popen(argv, env=env)
        except OSError as e:
            print(f"loadctl: cannot start {argv[0]}: {e}", file=sys.stderr)
            return EXIT_UNSTARTABLE
        try:
            cgroup = d.find_cgroup(proc.pid, scope_unit, slice_)
            for signum in dict.fromkeys(caught):  # one that came before the scope existed
                kill_scope(signum)
            reply = loadctl.request(
                a.socket, "job_start",
                {"kind": a.kind, "project": a.project, "label": a.label, "unit": scope_unit,
                 "cgroup": cgroup, "pid": proc.pid, "owner_pid": d.pid, **({"slot": slot} if slot else {})},
            )  # fmt: skip
            registered = bool(reply.get("ok"))
            if registered and slot:
                bound["slot"] = bool(reply.get("bound"))
            if not registered:
                print(f"loadctl: job not registered: {reply.get('message')}", file=sys.stderr)
        except DaemonUnavailable:
            print("loadctl: daemon went away; the job runs unregistered", file=sys.stderr)
        code = _wait_job(proc, a, d, scope_unit)
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        if registered:
            with contextlib.suppress(DaemonUnavailable):  # else the daemon notices the empty scope
                loadctl.request(a.socket, "job_end", {"unit": scope_unit})
    return 128 - code if code < 0 else code
