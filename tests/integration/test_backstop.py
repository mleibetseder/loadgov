"""Host test of the memory backstop (balloon acceptance): real memory.max / memory.oom.group / OOM kill.

Runs only with LOADGOV_INTEGRATION=1 as root. The backstop's target slice is `loadgov.slice/loadgov-test.slice`,
passed as a test-only constructor argument; `build-work.slice` is never named. Two scopes live in the test
slice: a balloon scope (a python process that allocates far above the 256 MB limit, plus a `sleep` in the same
scope) and a sibling scope with only a `sleep`. Expected: the balloon scope is killed as a group (both its
processes), the sibling scope survives, one BACKSTOP event is emitted; then the limits are removed (`max`)
and the test slices are stopped."""

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from loadgov.config import BackstopConfig
from loadgov.daemon.backstop_ops import MIB, BackstopRuntime
from loadgov.host.procfs import HostFS

pytestmark = pytest.mark.skipif(
    os.environ.get("LOADGOV_INTEGRATION") != "1" or os.geteuid() != 0,
    reason="needs LOADGOV_INTEGRATION=1 and root",
)

SLICE = "loadgov-test.slice"
TARGET = f"loadgov.slice/{SLICE}"
ROOT = "/sys/fs/cgroup"
BALLOON, SIBLING = "loadgov-test-balloon.scope", "loadgov-test-sibling.scope"
LIMIT_MB = 256

BALLOON_CODE = """
import os, sys, time
while not os.path.exists(sys.argv[1]):
    time.sleep(0.05)
b = bytearray(900 * 1024 * 1024)
for i in range(0, len(b), 4096):
    b[i] = 1
time.sleep(600)
"""


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def procs(unit: str) -> list[int]:
    try:
        with open(f"{ROOT}/{TARGET}/{unit}/cgroup.procs") as f:
            return [int(x) for x in f.read().split()]
    except OSError:
        return []


def slice_is_empty() -> bool:
    try:
        with open(f"{ROOT}/loadgov.slice/cgroup.events") as f:
            return "populated 0" in f.read()
    except OSError:
        return True


def wait_for(cond, secs=30.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def start_scope(unit: str, *cmd: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "systemd-run",
            "--quiet",
            "--collect",  # a scope that was OOM-killed is "failed": collect it
            "--scope",
            "--slice",
            SLICE,
            "--unit",
            unit.removesuffix(".scope"),
            "-p",
            "MemoryAccounting=yes",
            *cmd,
        ]  # fmt: skip
    )


@pytest.fixture
def scopes(tmp_path):
    gate = tmp_path / "go"
    sibling = start_scope(SIBLING, "sleep", "600")
    balloon = start_scope(
        BALLOON, "sh", "-c", f"sleep 600 & exec python3 -c '{BALLOON_CODE}' {gate}"
    )  # a second process in the scope proves the group kill
    assert wait_for(lambda: len(procs(BALLOON)) >= 2 and procs(SIBLING)), "the test scopes did not start"
    assert wait_for(lambda: os.path.exists(f"{ROOT}/{TARGET}/memory.max")), (
        "no memory controller on the slice"
    )
    yield SimpleNamespace(gate=gate)
    for unit in (BALLOON, SIBLING):
        sh("systemctl", "stop", unit)
    for p in (balloon, sibling):
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sh("systemctl", "stop", SLICE)
    if slice_is_empty():  # loadgov.slice is shared with the other host tests: stop it only when empty
        sh("systemctl", "stop", "loadgov.slice")


def test_balloon_in_a_job_scope_is_group_killed_sibling_survives_and_backstop_fires(scopes, tmp_path):
    fs, events, audits = HostFS(), [], []
    jobs = {
        unit: SimpleNamespace(cgroup=f"/{TARGET}/{unit}")
        for unit in (BALLOON, SIBLING)
    }  # fmt: skip

    def runtime(cfg):
        return BackstopRuntime(
            fs, cfg, time.time, str(tmp_path), lambda k, p: events.append((k, p)), audits.append,
            lambda: jobs, slice_path=TARGET,
        )  # fmt: skip

    rt = runtime(BackstopConfig(True, LIMIT_MB, 0))
    rt.tick()  # apply + memory.oom.group on both scopes + the detection baseline
    cg = f"{ROOT}/{TARGET}"
    assert open(f"{cg}/memory.max").read().strip() == str(LIMIT_MB * MIB)
    assert open(f"{cg}/memory.swap.max").read().strip() == "0"
    assert open(f"{cg}/{BALLOON}/memory.oom.group").read().strip() == "1"
    assert rt.status()["applied"] is True
    rt.tick()
    assert events == []
    sibling_pids = procs(SIBLING)
    assert sibling_pids

    scopes.gate.write_text("go")  # the balloon allocates 900 MB against a 256 MB limit
    assert wait_for(lambda: not procs(BALLOON)), "the balloon scope was not killed"
    assert procs(SIBLING) == sibling_pids  # the sibling scope was not touched
    assert os.path.exists(f"/proc/{sibling_pids[0]}")

    del jobs[BALLOON]  # the daemon drops a job whose scope emptied
    rt.tick()
    assert len(events) == 1
    kind, payload = events[0]
    assert kind == "backstop" and BALLOON in payload["scope"] and payload["memory_peak_mb"] > 100
    assert rt.status()["oom_kills"] >= 1 and BALLOON in rt.status()["last_event"]
    assert audits[-1]["op"] == "backstop" and audits[-1]["result"] == "event"

    runtime(BackstopConfig()).tick()  # rollback: the table is gone
    assert open(f"{cg}/memory.max").read().strip() == "max"
    assert open(f"{cg}/memory.swap.max").read().strip() == "max"
    assert not os.path.exists(rt.marker)
