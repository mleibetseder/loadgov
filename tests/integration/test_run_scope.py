"""Host test of `loadctl run`: a real loadgovd on tmp paths, a real systemd scope on loadgov-test.slice.

Runs only with LOADGOV_INTEGRATION=1 as root. Touches nothing but the scopes it starts itself
(`sleep` and `sh -c 'exit 7'`) in `loadgov-test.slice`; never a build-work*.slice."""

import os
import re
import subprocess
import sys
import time

import pytest

from loadgov.cli.client import DaemonUnavailable
from loadgov.cli.client import request as ask

pytestmark = pytest.mark.skipif(
    os.environ.get("LOADGOV_INTEGRATION") != "1" or os.geteuid() != 0,
    reason="needs LOADGOV_INTEGRATION=1 and root",
)

SLICE = "loadgov-test.slice"
CONFIG = """
[headroom.kinds.impl]
load1_max = 100000
load5_max = false
psi_max = 101
mem_min_gb = 0
swap_max_mb = 10000000
mem_psi_max = 101
confirm_s = 1
poll_s = 1
max_wait_s = 60
"""


@pytest.fixture
def daemon(tmp_path, request: pytest.FixtureRequest):
    sock = str(tmp_path / "run" / "loadgovd.sock")
    cfg = tmp_path / "loadgov.toml"
    cfg.write_text(CONFIG + getattr(request, "param", ""))
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "loadgov.daemon.main", "--socket", sock, "--run-dir", str(tmp_path / "run"),
            "--compat-link", "", "--config", str(cfg), "--state-dir", str(tmp_path / "state"),
            "--log-dir", str(tmp_path / "log"), "--env-file", "", "--lock-root", str(tmp_path / "lock"),
            "--tick", "1",
        ]
    )  # fmt: skip
    (tmp_path / "lock").mkdir()
    for _ in range(100):
        try:
            if ask(sock, "status").get("ok"):
                break
        except DaemonUnavailable:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("loadgovd did not come up")
    yield sock
    proc.terminate()
    proc.wait(timeout=10)


def loadctl(sock, label, *cmd, kind="impl", extra=()):
    return subprocess.Popen(
        [sys.executable, "-m", "loadgov.cli.loadctl", "--socket", sock, "run", "--kind", kind, "--project",
         "test", "--label", label, "--slice", SLICE, *extra, "--", *cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def systemctl_show(unit, prop):
    out = subprocess.run(["systemctl", "show", unit, "-p", prop, "--value"], capture_output=True, text=True)
    return out.stdout.strip()


def test_run_creates_a_scope_registers_the_job_and_passes_the_exit_code(daemon):
    p = loadctl(daemon, "itest-sleep", "sleep", "3")
    job = None
    for _ in range(100):
        jobs = ask(daemon, "status")["status"]["jobs"]
        if jobs:
            job = jobs[0]
            break
        time.sleep(0.1)
    assert job, "job never showed in status.jobs"
    unit = job["unit"]
    assert unit.startswith("loadgov-impl-itest-sleep-") and unit.endswith(".scope")
    assert job["cgroup"].startswith(f"/loadgov.slice/{SLICE}/") and job["slot"] is None
    assert systemctl_show(unit, "Slice") == SLICE
    procs = open(f"/sys/fs/cgroup{job['cgroup']}/cgroup.procs").read().split()
    assert str(job["pid"]) in procs
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert "HEADROOM_OK itest-sleep" in err and out == ""
    # the scope is gone and the job left the registry
    for _ in range(50):
        if not ask(daemon, "status")["status"]["jobs"] and not os.path.exists(
            f"/sys/fs/cgroup{job['cgroup']}"
        ):
            break
        time.sleep(0.2)
    assert ask(daemon, "status")["status"]["jobs"] == []
    assert not os.path.exists(f"/sys/fs/cgroup{job['cgroup']}")
    assert systemctl_show(unit, "ActiveState") in ("inactive", "")


def test_exit_code_passthrough(daemon):
    p = loadctl(daemon, "itest-exit", "sh", "-c", "exit 7")
    _out, err = p.communicate(timeout=30)
    assert p.returncode == 7, err


ENFORCE = "\n[slots]\nenforce = true\n"


def lock_held(path):
    """True while somebody holds the flock: a non-blocking `flock` on it fails."""
    return subprocess.run(["flock", "-n", path, "true"]).returncode != 0


def slot_state(sock):
    return ask(sock, "status")["status"]["slots"]["heavy"]["state"]


def wait_for(cond, seconds, what):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    pytest.fail(f"timeout: {what}")


@pytest.mark.parametrize("daemon", ["", ENFORCE], ids=["enforce-off", "enforce-on"], indirect=True)
def test_enforced_gate_lock_survives_the_death_of_loadctl_run(daemon, tmp_path):
    """The flock is held by loadctl run and inherited by the job, never only by the daemon."""
    lock = str(tmp_path / "lock" / "host-heavy-slot.lock")
    holder = tmp_path / "lock" / "host-heavy-slot.holder"
    p = loadctl(daemon, "itest-gate", "sleep", "4", kind="gate", extra=("--headroom", "none"))
    try:
        job = wait_for(
            lambda: (ask(daemon, "status")["status"]["jobs"] or [None])[0], 20, "job in status.jobs"
        )
        assert job["slot"] == "heavy"
        # (a) the lock is held and the holder line is the legacy format naming the loadctl run pid
        assert lock_held(lock)
        assert re.fullmatch(
            rf"test itest-gate pid={p.pid} since=\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
            holder.read_text().strip(),
        )
        # (b) the job inside the scope holds the lock file open (inherited descriptor)
        fds = [os.path.realpath(f"/proc/{job['pid']}/fd/{n}") for n in os.listdir(f"/proc/{job['pid']}/fd")]
        assert os.path.realpath(lock) in fds
        # (c) kill -9 the parent: the scope's sleep lives on, so the lock and the slot stay
        p.kill()
        p.wait()
        assert os.path.exists(f"/proc/{job['pid']}")
        time.sleep(2.5)  # more than two daemon ticks
        assert lock_held(lock) and slot_state(daemon) == "held"
        # (d) the job ends: the lock is free and the slot released within ~2 s
        wait_for(lambda: not os.path.exists(f"/proc/{job['pid']}"), 15, "sleep to end")
        t0 = time.monotonic()
        wait_for(
            lambda: not lock_held(lock) and slot_state(daemon) == "free", 2.5, "lock free and slot released"
        )
        assert time.monotonic() - t0 < 2.5
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
