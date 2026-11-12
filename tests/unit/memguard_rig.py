"""A fake host for the memory guard tests: processes, job scopes, a fake pidfd layer and a fake docker.

Nothing here signals, freezes or pauses a real process or container: `Kernel` records what the daemon
asked for and mutates the fixture /proc tree (state `T` after SIGSTOP, `S` after SIGCONT).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
from dataclasses import dataclass, field, replace
from pathlib import Path

from conftest import parity_config

from loadgov.config import ReaperConfig
from loadgov.daemon.main import Daemon
from loadgov.engines.jobs import Job
from loadgov.protocol import VERSION

T0 = 1_762_800_000.0
OWNER, AGENT = 501, 600
SCOPE_ROOT = "/build.slice/build-work.slice/build-work-test.slice"
SNAP = "# Usage: dispatch.sh\n"


class Clock:
    t = T0

    def __call__(self):
        return self.t


@dataclass
class Spec:
    comm: str
    ppid: int
    rss: int
    state: str
    cgroup: str
    cmd: str
    start: int
    cwd: str | None = None


@dataclass
class Kernel:
    """Records every signal and docker call; `die_on` pids vanish when they get SIGTERM/SIGKILL."""

    rig: Rig
    sent: list = field(default_factory=list)
    docker: list = field(default_factory=list)
    containers: list = field(default_factory=list)
    die_on: set = field(default_factory=set)
    ignore_cont: bool = False  # SIGCONT is accepted but the process stays stopped (a stuck thaw)

    def opener(self, pid):
        if not (self.rig.proc / str(pid)).exists():
            raise ProcessLookupError(pid)
        return pid

    def sender(self, fd, sig):
        self.sent.append((fd, signal.Signals(sig).name))
        if sig == signal.SIGSTOP:
            self.rig.specs[fd].state = "T"
            self.rig.write(fd)
        elif sig == signal.SIGCONT and not self.ignore_cont:
            self.rig.specs[fd].state = "S"
            self.rig.write(fd)
        elif sig in (signal.SIGTERM, signal.SIGKILL) and fd in self.die_on:
            self.rig.remove(fd)

    def closer(self, fd):
        pass

    def docker_runner(self, argv):
        self.docker.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return 0, "\n".join(self.containers)
        return 0, ""

    def signals(self, name=None, pid=None):
        return [(p, s) for p, s in self.sent if (name is None or s == name) and (pid is None or p == pid)]

    def mutating_docker(self):
        return [a for a in self.docker if a[1] != "ps"]


class Rig:
    def __init__(
        self,
        tmp_path,
        fake_host,
        *,
        enforce=True,
        reaper=False,
        avail=12000,
        swap=0,
        projects=None,
        override=None,
        log_path=None,
        restrict=None,
    ):
        self.tmp, self.enforce = tmp_path, enforce
        self.host = fake_host("calm", avail_mb=avail, swap_used_mb=swap)
        self.proc = Path(self.host.proc)
        self.cg = Path(self.host.cgroup)
        self.specs: dict[int, Spec] = {}
        self.clock = Clock()
        self.kernel = Kernel(self)
        self.avail, self.swap = avail, swap
        base = parity_config()
        self.cfg = replace(
            base,
            memguard=replace(
                base.memguard, enforce=enforce, pre_gate_reap_gb=8.0, log_path=log_path,
                projects=base.memguard.projects if projects is None else projects,
            ),
            reaper=ReaperConfig(reaper),
        )  # fmt: skip
        self.override = override
        self.restrict = restrict
        self.add(1, "systemd", 0, cgroup="/init.scope", cmd="init")
        self.add(OWNER, "loadctl", 1, cgroup="/user.slice/s.scope", cmd="loadctl")
        self.add(AGENT, "claude", 1, cgroup="/loop-a", cmd="claude")
        self.d = self.new_daemon()

    # ---- daemon ----------------------------------------------------------------------------------

    def new_daemon(self, enforce=None, reaper=None) -> Daemon:
        cfg = self.cfg
        if enforce is not None or reaper is not None:
            cfg = replace(
                cfg,
                memguard=replace(cfg.memguard, enforce=cfg.memguard.enforce if enforce is None else enforce),
                reaper=ReaperConfig(cfg.reaper.enforce if reaper is None else reaper),
            )
        d = Daemon(
            self.host.fs, state_dir=str(self.tmp / "state"), log_dir=str(self.tmp / "log"), env_file=None,
            compat_link=None, run_dir=str(self.tmp / "run"), config=cfg, clock=self.clock,
            tokens_file=str(self.tmp / "tokens.env"),
            memguard_options={
                "opener": self.kernel.opener, "sender": self.kernel.sender, "closer": self.kernel.closer,
                "docker_runner": self.kernel.docker_runner, "snapshot_reader": lambda _p: SNAP,
                "monotonic": self.clock, "override": self.override,
                "restrict_cgroup": self.restrict,
            },
        )  # fmt: skip
        os.makedirs(d.run_dir, exist_ok=True)
        d.sampler.prime(self.host.prev_stat)
        d._memguard_step = lambda s: d.memguard.tick(s)  # let a bug fail the test instead of being logged
        self.d = d
        return d

    def restart(self, **kw) -> Daemon:
        """kill -9: the old object is simply dropped (nothing runs on exit), a new one reads state.json."""
        return self.new_daemon(**kw)

    def call(self, op, peer=OWNER, **args):
        return self.d.dispatch({"v": VERSION, "op": op, "args": args}, peer)

    def owner(self, op, **args):
        return self.call(op, OWNER, owner=True, **args)

    def audit(self):
        try:
            return [json.loads(x) for x in open(self.d.audit.path)]
        except FileNotFoundError:
            return []

    def events(self, kind=None):
        out = [a for a in self.audit() if a["op"] == "memguard"]
        return [a for a in out if kind is None or a["request"]["event"] == kind]

    # ---- time and memory -------------------------------------------------------------------------

    def set_mem(self, avail, swap=None):
        self.avail = avail
        self.swap = self.swap if swap is None else swap
        (self.proc / "meminfo").write_text(
            f"MemTotal: {16232 * 1024} kB\nMemFree: 100 kB\nMemAvailable: {avail * 1024} kB\n"
            f"SwapTotal: {8191 * 1024} kB\nSwapFree: {(8191 - self.swap) * 1024} kB\n"
        )

    def step(self, avail=None, swap=None, dt=5.0) -> dict:
        self.clock.t += dt
        if avail is not None or swap is not None:
            self.set_mem(self.avail if avail is None else avail, swap)
        return self.d.tick()["memguard"]

    # ---- processes and scopes --------------------------------------------------------------------

    def add(
        self, pid, comm, ppid=1, rss=10, state="S", cgroup="/user.slice/x", cmd=None, start=None, cwd=None
    ):
        self.specs[pid] = Spec(comm, ppid, rss, state, cgroup, cmd if cmd is not None else comm,
                               start or 1000 + pid, cwd)  # fmt: skip
        self.write(pid)

    def write(self, pid):
        s, d = self.specs[pid], self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(
            f"{pid} ({s.comm}) {s.state} {s.ppid} " + " ".join(["0"] * 17) + f" {s.start} 0 0\n"
        )
        (d / "status").write_text(f"Name:\t{s.comm}\nPPid:\t{s.ppid}\nVmRSS:\t{s.rss * 1024} kB\n")
        (d / "cgroup").write_text(f"0::{s.cgroup}\n")
        (d / "cmdline").write_bytes(s.cmd.replace(" ", "\0").encode() + b"\0")
        if s.cwd and not (d / "cwd").is_symlink():
            os.symlink(s.cwd, d / "cwd")

    def remove(self, pid):
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)
        self.specs.pop(pid, None)

    def state(self, pid):
        return self.specs[pid].state

    def scope(self, unit, kind, pids, project="test", owner_pid=None, mem_mb=None):
        """A registered job scope (`loadctl run`) with the given member pids."""
        cgp = f"{SCOPE_ROOT}/{unit}"
        d = self.cg / cgp.lstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in pids))
        (d / "cgroup.freeze").write_text("0\n")
        if mem_mb is not None:
            (d / "memory.current").write_text(f"{mem_mb * 2**20}\n")
        for p in pids:
            self.specs[p].cgroup = cgp
            self.write(p)
        if owner_pid is None:
            owner_pid = 900 + len(self.d.jobs.jobs)
            self.add(owner_pid, "loadctl", 1, cgroup="/user.slice/s.scope", cmd="loadctl run")
        self.d.jobs.jobs[unit] = Job(
            kind, project, unit, unit, cgp, pids[0], None, owner_pid, None, self.clock()
        )
        return cgp

    def freeze_value(self, unit) -> str:
        return (self.cg / SCOPE_ROOT.lstrip("/") / unit / "cgroup.freeze").read_text().strip()

    def impl_tree(self, base, rss_work=1500, rustc_mb=0):
        """A legacy implementer: bash snapshot shell -> claude -> work (node, or rustc for a compile)."""
        self.add(base, "bash", 1, 5, cmd=f"bash /tmp/loop-a-snap.{base} dispatch.sh")
        self.add(base + 1, "claude", base, 600, cmd="claude -p")
        if rustc_mb:
            self.add(base + 2, "rustc", base + 1, rustc_mb, cmd="rustc --crate-name backend")
        else:
            self.add(base + 2, "node", base + 1, rss_work, cmd="node vitest")
        self.add(base + 3, "sh", base + 2, 100)
        return {"shell": base, "claude": base + 1, "work": base + 2, "child": base + 3}
