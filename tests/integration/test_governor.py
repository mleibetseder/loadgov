"""Host test of the CPU governor: real `cpu.max` steps on a private test slice.

Runs only with LOADGOV_INTEGRATION=1 as root. The governor's target is `loadgov.slice/loadgov-test.slice`,
passed as a test-only constructor argument (never config); `build-work.slice` and `loop-a` are never
named and no mirror cgroup is configured. The busy share is injected (synthetic /proc/stat jiffies)."""

import os
import subprocess
import time

import pytest

from loadgov.engines.governor import Governor
from loadgov.host.procfs import HostFS

pytestmark = pytest.mark.skipif(
    os.environ.get("LOADGOV_INTEGRATION") != "1" or os.geteuid() != 0,
    reason="needs LOADGOV_INTEGRATION=1 and root",
)

SLICE = "loadgov-test.slice"
SCOPE = "loadgov-test-governor.scope"
TARGET = f"loadgov.slice/{SLICE}"
CPU_MAX = f"/sys/fs/cgroup/{TARGET}/cpu.max"


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def slice_is_empty() -> bool:
    try:
        with open("/sys/fs/cgroup/loadgov.slice/cgroup.events") as f:
            return "populated 0" in f.read()
    except OSError:
        return True


@pytest.fixture
def test_slice():
    proc = subprocess.Popen(
        [
            "systemd-run",
            "--quiet",
            "--slice",
            SLICE,
            "--unit",
            SCOPE.removesuffix(".scope"),
            "--scope",
            "-p",
            "CPUAccounting=yes",
            "sleep",
            "60",
        ]  # fmt: skip
    )
    for _ in range(50):
        if os.path.exists(CPU_MAX):
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("the test slice did not appear")
    r = sh("systemctl", "set-property", "--runtime", SLICE, "CPUQuota=200%")  # start at 2.0 cores
    assert r.returncode == 0, r.stderr
    assert open(CPU_MAX).read() == "200000 100000\n"
    yield
    sh("systemctl", "stop", SCOPE)
    proc.wait(timeout=10)
    sh("systemctl", "stop", SLICE)
    if slice_is_empty():  # loadgov.slice is shared with `loadctl run` tests: stop it only when empty
        sh("systemctl", "stop", "loadgov.slice")


class Feed:
    """Synthetic /proc/stat: one 10 s window at the given busy %."""

    def __init__(self, gov, clock):
        self.gov, self.clock, self.total, self.idle = gov, clock, 0, 0

    def __call__(self, busy, max_cores=3.5, target=65):
        self.clock[0] += 10
        self.total += 1000
        self.idle += round(1000 * (100 - busy) / 100)
        self.gov.tick((self.total, self.idle), max_cores, target)


def test_enforce_steps_the_real_cpu_max_and_stop_leaves_the_cap(test_slice):
    clock = [1000.0]
    gov = Governor(
        HostFS(),
        lambda: clock[0],
        enforce=True,
        primary=TARGET,
        monotonic=lambda: clock[0],
    )
    feed = Feed(gov, clock)
    feed(0)  # baseline
    assert gov.cap == 2.0  # the start value is what the slice has
    seen = []
    for busy in (82, 83, 75):
        feed(busy)
        seen.append(open(CPU_MAX).read())
    assert seen == ["150000 100000\n", "100000 100000\n", "100000 100000\n"]  # 2 -> 1.5 -> 1 (floor)
    for _ in range(8):
        feed(40)
    assert open(CPU_MAX).read() == "100000 100000\n"  # eight calm samples: nothing
    feed(40)
    assert open(CPU_MAX).read() == "150000 100000\n"  # the ninth: +0.5
    gov.tick((feed.total, feed.idle), 1.0, 65)  # max_cores lowered: re-applied without a sample
    assert open(CPU_MAX).read() == "100000 100000\n"
    del gov  # "stop the daemon": nothing is written, the cap stays (never `max`)
    assert open(CPU_MAX).read() == "100000 100000\n"
