"""STUCK = held for more than 45 min AND no CPU progress for 10 min. Fake /proc and cgroup tree, fake clock."""

import os

import pytest
from test_watch import Clock, ev, kinds, sample

from loadgov.engines.jobs import Job, JobTable
from loadgov.engines.slots import SlotManager
from loadgov.engines.watch import Watch
from loadgov.host.procfs import HostFS

T0 = 1_762_800_000.0
CG = "/build.slice/build-work.slice/build-work-proj-a.slice/loadgov-gate-x-1.scope"
UNIT = "loadgov-gate-x-1.scope"
MIN = 60


class Rig:
    """A slot manager over a fake host; `step(minutes, cpu_cores)` advances the clock one tick a minute."""

    def __init__(self, tmp_path):
        self.t = T0
        self.proc, self.cg = tmp_path / "proc", tmp_path / "cgroup"
        self.proc.mkdir()
        (tmp_path / "lock").mkdir()
        self.fs = HostFS(
            proc_root=str(self.proc), cgroup_root=str(self.cg), lock_root=str(tmp_path / "lock"), clk_tck=100
        )
        self.cpu_us = 0
        self.ticks = {}  # pid -> [utime, stime] in clock ticks
        self.m = SlotManager(self.fs, clock=lambda: self.t, stat_reader=self.read_stat, self_pid=999)
        self.proc_add(999)  # the daemon itself, the holder of an owner hold

    def read_stat(self, pid):
        try:
            return (self.proc / str(pid) / "stat").read_text()
        except OSError:
            return None

    def proc_add(self, pid, ppid=1, utime=0):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        self.ticks[pid] = utime
        self.write_stat(pid, ppid)

    def write_stat(self, pid, ppid=1):
        ut = self.ticks[pid]
        text = f"{pid} (x) S {ppid} " + "0 " * 9 + f"{ut} 0 0 0 " + "0 " * 4 + f"{1000 + pid} 0\n"
        (self.proc / str(pid) / "stat").write_text(text)

    def scope(self, procs="200\n", usage=True):
        d = self.cg / CG.lstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        (d / "cgroup.procs").write_text(procs)
        if usage:
            self.write_usage()

    def write_usage(self):
        (self.cg / CG.lstrip("/") / "cpu.stat").write_text(f"usage_usec {int(self.cpu_us)}\nuser_usec 0\n")

    def hold_gate(self, with_scope=True):
        self.proc_add(100)
        self.proc_add(200, 100)
        assert self.m.acquire("heavy", "proj-a", "gate", 100, True)["status"] == "granted"
        if with_scope:
            self.scope()
            assert self.m.bind_scope("heavy", 100, UNIT, CG, 200)

    def step(self, minutes, cores=0.0, tree=False):
        """One tick a minute: the scope (or the pid tree) uses `cores` cores."""
        for _ in range(int(minutes)):
            self.t += MIN
            if tree:
                self.ticks[201] += int(cores * MIN * 100)
                self.write_stat(201, 200)
            elif (self.cg / CG.lstrip("/") / "cpu.stat").exists():
                self.cpu_us += cores * MIN * 1_000_000
                self.write_usage()
            self.m.tick()

    def heavy(self):
        return self.m.summary()["heavy"]


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_a_long_hold_with_cpu_is_not_stuck(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(50, cores=0.52)  # a healthy long gate: about half a core for 50 minutes
    s = rig.heavy()
    assert s["held_s"] == 50 * MIN and s["idle_s"] is not None and s["idle_s"] <= MIN
    assert s["stuck"] is False


def test_a_long_hold_idle_for_10_minutes_is_stuck(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(40, cores=0.5)
    rig.step(9)  # idle, not yet 10 minutes, and not held 45 minutes either
    assert rig.heavy()["stuck"] is False
    rig.step(2)  # held 51 min, idle 11 min
    s = rig.heavy()
    assert s["held_s"] > 45 * MIN and s["idle_s"] >= 10 * MIN and s["stuck"] is True
    rig.step(1, cores=0.5)  # it works again: no longer stuck
    assert rig.heavy()["stuck"] is False and rig.heavy()["idle_s"] <= MIN


def test_idle_but_held_less_than_45_minutes_is_not_stuck(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(30)
    s = rig.heavy()
    assert s["idle_s"] >= 10 * MIN and s["held_s"] == 30 * MIN and s["stuck"] is False


def test_less_than_one_percent_of_a_core_is_no_progress_one_percent_is(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(50, cores=0.005)
    assert rig.heavy()["stuck"] is True
    rig.step(1, cores=0.011)
    assert rig.heavy()["stuck"] is False


def test_unreadable_progress_is_not_stuck(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(5, cores=0.5)
    os.remove(rig.cg / CG.lstrip("/") / "cpu.stat")  # unreadable from now on
    rig.step(60)
    s = rig.heavy()
    assert s["held_s"] > 45 * MIN and s["idle_s"] is None and s["stuck"] is False
    rig.write_usage()  # readable again: a new reading only restarts the comparison
    rig.step(1)
    assert rig.heavy()["idle_s"] is not None


def test_a_holder_without_a_scope_is_judged_by_its_process_tree(rig):
    rig.hold_gate(with_scope=False)
    rig.proc_add(201, 200, utime=5000)  # a grandchild of the holder does the work
    rig.m.tick()
    rig.step(50, cores=0.5, tree=True)
    assert rig.heavy()["stuck"] is False and rig.heavy()["idle_s"] <= MIN
    rig.step(11, tree=True)
    assert rig.heavy()["stuck"] is True


def test_a_new_holder_starts_a_new_observation(rig):
    rig.hold_gate()
    rig.m.tick()
    rig.step(60)
    assert rig.heavy()["stuck"] is True
    rig.m.release("heavy", 100)
    rig.proc_add(300)
    assert rig.m.acquire("heavy", "proj-a", "next", 300, True)["status"] == "granted"
    rig.m.tick()
    s = rig.heavy()
    assert s["held_s"] == 0 and s["idle_s"] == 0 and s["stuck"] is False


def test_free_slot_and_owner_hold_have_no_progress_and_are_never_stuck(rig):
    assert rig.heavy()["idle_s"] is None and rig.heavy()["held_s"] is None
    rig.m.hold_on(["heavy"], "update")
    rig.step(70)
    s = rig.heavy()
    assert s["state"] == "held-by-hold" and s["held_s"] > 45 * MIN and s["idle_s"] is None
    assert s["stuck"] is False


def test_the_alert_fires_only_in_the_stuck_case(rig):
    clock = Clock()
    watch = Watch(clock)
    rig.hold_gate()
    rig.m.tick()
    rig.step(50, cores=0.5)  # long hold, busy: no alert
    assert ev(watch, clock, s=sample(slots=rig.m.summary())) == []
    rig.step(11)  # idle for 11 minutes
    out = ev(watch, clock, s=sample(slots=rig.m.summary()))
    assert kinds(out) == [("alert", "stuck_holder:heavy")]
    assert out[0]["actions"] == ["cancel_holder"] and "no CPU progress for 11 min" in out[0]["message"]
    # the heavy_slot age flag alone (the sampler's old rule) no longer alerts
    rig.step(1, cores=0.5)
    assert ev(watch, clock, s=sample(slots=rig.m.summary(), heavy_stuck=True)) == []
    out = ev(watch, clock, s=sample(slots=rig.m.summary(), heavy_stuck=True))
    assert kinds(out) == [("resolved", "stuck_holder:heavy")]


CID_A, CID_B = "a" * 64, "b" * 64


class DockerRig(Rig):
    """The gate's scope plus Docker scopes under /system.slice, attributed by the real usage meter."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.docker = tmp_path / "docker"
        self.docker.mkdir()
        self.jt = JobTable(self.fs, lambda: self.t, docker_root=str(self.docker))
        self.m.docker_cpu = self.jt.docker_cpu
        self.container_us = {}

    def hold_gate_job(self):
        self.hold_gate()
        self.jt.add(Job("gate", "proj-a", "l", UNIT, CG, 200, 1200, 100, "heavy", self.t))

    def container(self, cid, name, project):
        d = self.docker / cid
        d.mkdir(exist_ok=True)
        labels = {"com.docker.compose.project": project}
        (d / "config.v2.json").write_text(
            __import__("json").dumps({"Name": f"/{name}", "Config": {"Labels": labels}})
        )
        self.container_us[cid] = 0

    def grow(self, minutes, cid, cores):
        for _ in range(minutes):
            self.container_us[cid] += cores * MIN * 1_000_000
            sc = self.cg / "system.slice" / f"docker-{cid}.scope"
            sc.mkdir(parents=True, exist_ok=True)
            (sc / "cpu.stat").write_text(f"usage_usec {int(self.container_us[cid])}\n")
            self.step(1)
            self.jt.meter.sample()


@pytest.fixture
def drig(tmp_path):
    r = DockerRig(tmp_path)
    r.hold_gate_job()
    r.jt.meter.sample()
    r.m.tick()
    return r


def test_docker_cpu_attributed_to_the_job_is_progress(drig):
    drig.container(CID_A, "proj-a-builder", "proj-a")  # a flat scope, a busy image build
    drig.grow(60, CID_A, 0.5)
    s = drig.heavy()
    assert s["held_s"] >= 60 * MIN and s["idle_s"] <= 2 * MIN and s["stuck"] is False


def test_the_same_with_docker_flat_is_stuck(drig):
    drig.container(CID_A, "proj-a-builder", "proj-a")
    drig.grow(60, CID_A, 0.0)
    assert drig.heavy()["stuck"] is True


def test_a_container_of_another_project_does_not_count(drig):
    # another project's job runs too: its container is attributed to it (project match wins over the
    # heavy-slot fallback), so the quiet gate stays idle however busy that container is
    drig.jt.add(Job("impl", "proj-b", "l", "loadgov-impl-2.scope", "/system.slice/loadgov-impl-2.scope",
                    300, 1, 300, None, drig.t))  # fmt: skip
    drig.container(CID_B, "proj-b-db", "proj-b")
    drig.grow(60, CID_B, 0.5)
    assert drig.jt.jobs[UNIT].docker_us == 0 and drig.jt.jobs["loadgov-impl-2.scope"].docker_us > 0
    assert drig.heavy()["stuck"] is True
