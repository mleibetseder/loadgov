"""Host test of the memory guard: a real `cgroup.freeze` on a real job scope in loadgov-test.slice.

Runs only with LOADGOV_INTEGRATION=1 as root. The daemon is an in-process object on tmp state, serving a
tmp unix socket for `loadctl run`; the low-memory threshold is injected (test-only constructor arguments,
never config) and `restrict_cgroup` limits the guard to scopes below `/loadgov.slice/loadgov-test.slice/`:
no legacy tree, container or JVM daemon of the host is ever a candidate, and the pre-gate reap is off.
Everything the test starts lives in `loadgov-test.slice` and is stopped at the end."""

import asyncio
import contextlib
import os
import subprocess
import sys
import threading
import time

import pytest

from loadgov.cli.client import request as ask
from loadgov.config import Config, MemguardConfig
from loadgov.daemon.main import Daemon
from loadgov.host.procfs import HostFS

pytestmark = pytest.mark.skipif(
    os.environ.get("LOADGOV_INTEGRATION") != "1" or os.geteuid() != 0,
    reason="needs LOADGOV_INTEGRATION=1 and root",
)

SLICE = "loadgov-test.slice"
PREFIX = f"/loadgov.slice/{SLICE}/"
BALLOON = "loadgov-test-balloon"
CALM = (1, 2)  # LOW 1 MB, RESUME 2 MB: always calm


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def avail_mb() -> int:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise AssertionError("no MemAvailable")


def wait_for(cond, seconds, what):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    pytest.fail(f"timeout: {what}")


class Host:
    """An in-process Daemon whose socket and ticks run on one event-loop thread (single-threaded state)."""

    def __init__(self, tmp_path, low_high):
        self.tmp, self.sock = tmp_path, str(tmp_path / "run" / "loadgovd.sock")
        self.low_high = low_high
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.d: Daemon | None = None
        self.server = None

    def start(self):
        def make():
            d = Daemon(
                HostFS(lock_root=str(self.tmp / "lock")), self.sock, str(self.tmp / "run"), None, 1.0,
                Config(memguard=MemguardConfig(True, 0.0)), str(self.tmp / "state"), str(self.tmp / "log"),
                None, tokens_file=str(self.tmp / "tokens.env"),
                memguard_options={"restrict_cgroup": PREFIX, "override": self.low_high},
            )  # fmt: skip
            d.sampler.prime()
            return d

        os.makedirs(self.tmp / "run", exist_ok=True)
        os.makedirs(self.tmp / "lock", exist_ok=True)
        self.d = self.call(make)

        async def serve():
            return await asyncio.start_unix_server(self.d.handle, path=self.sock)

        self.server = asyncio.run_coroutine_threadsafe(serve(), self.loop).result(10)
        self.tick()  # the first status, so `loadctl run` finds the daemon ready

    def call(self, fn):
        async def go():
            return fn()

        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(30)

    def tick(self):
        return self.call(self.d.tick)

    def crash(self):
        """kill -9: the object is dropped; nothing runs on exit (no thaw, no state write)."""

        async def close():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
        if os.path.exists(self.sock):
            os.unlink(self.sock)
        self.d = None

    def stop(self):
        if self.d is not None:
            self.crash()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


def freeze_file(cgroup):
    return f"/sys/fs/cgroup{cgroup}/cgroup.freeze"


def frozen(cgroup) -> str:
    return open(freeze_file(cgroup)).read().strip()


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path, (avail_mb() + 1500, avail_mb() + 3500))  # LOW above what is available: pressure
    h.start()
    procs = []
    h.procs = procs
    yield h
    for cg in list(getattr(h, "cgroups", [])):  # thaw first: a frozen scope cannot exit
        with contextlib.suppress(OSError):
            open(freeze_file(cg), "w").write("0\n")
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)
    h.stop()
    for unit in (BALLOON, *getattr(h, "units", [])):
        sh("systemctl", "stop", unit if unit.endswith(".scope") else unit + ".scope")
    sh("systemctl", "stop", SLICE)
    try:
        empty = "populated 0" in open("/sys/fs/cgroup/loadgov.slice/cgroup.events").read()
    except OSError:
        empty = True
    if empty:  # loadgov.slice is shared with the other host tests: stop it only when empty
        sh("systemctl", "stop", "loadgov.slice")


def start_gate(h):
    p = subprocess.Popen(
        [sys.executable, "-m", "loadgov.cli.loadctl", "--socket", h.sock, "run", "--kind", "gate", "--project",
         "test", "--label", "itest-guard", "--slice", SLICE, "--headroom", "none", "--", "sleep", "30"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    h.procs.append(p)
    job = wait_for(lambda: (ask(h.sock, "status")["status"]["jobs"] or [None])[0], 30, "job in status.jobs")
    h.cgroups, h.units = [job["cgroup"]], [job["unit"]]
    assert job["cgroup"].startswith(PREFIX) and job["kind"] == "gate"
    return job


def start_balloon(h):
    """~150 MB of touched memory in its own scope, capped by MemoryMax so it cannot grow."""
    code = "b = bytearray(150 * 2**20); b[::4096] = b'x' * len(b[::4096]); import time; time.sleep(60)"
    p = subprocess.Popen(
        ["systemd-run", "--scope", "--quiet", f"--slice={SLICE}", f"--unit={BALLOON}", "-p", "MemoryMax=300M",
         sys.executable, "-c", code],
    )  # fmt: skip
    h.procs.append(p)
    wait_for(lambda: os.path.exists(f"/sys/fs/cgroup/loadgov.slice/{SLICE}/{BALLOON}.scope"), 10, "balloon")
    cg = f"/loadgov.slice/{SLICE}/{BALLOON}.scope"
    wait_for(
        lambda: int(open(f"/sys/fs/cgroup{cg}/memory.current").read()) > 100 * 2**20, 10, "balloon memory"
    )
    return cg


def audit_events(h, kind):
    import json

    path = h.tmp / "log" / "audit.jsonl"
    rows = [json.loads(x) for x in open(path)] if path.exists() else []
    return [r for r in rows if r["op"] == "memguard" and r["request"]["event"] == kind]


def test_pressure_freezes_the_gate_scope_crash_keeps_it_frozen_restart_adopts_and_recover_thaws(host):
    job = start_gate(host)
    balloon = start_balloon(host)
    assert frozen(job["cgroup"]) == "0" and frozen(balloon) == "0"

    # pressure (injected LOW above the available memory): the registered gate scope is frozen, nothing else
    wait_for(lambda: host.tick()["memguard"]["paused"], 10, "a pause")
    assert frozen(job["cgroup"]) == "1"
    assert frozen(balloon) == "0"  # not a registered job: never a candidate
    assert [p["how"] for p in host.d.status["memguard"]["paused"]] == ["freeze"]
    assert audit_events(host, "PAUSE")[0]["result"] == "applied"

    # kill -9 the daemon object: the scope stays frozen
    host.crash()
    time.sleep(1.5)
    assert frozen(job["cgroup"]) == "1"

    # restart: re-adopted (ADOPTED), still frozen, still held while memory is low
    host.start()
    host.tick()
    assert audit_events(host, "ADOPTED")
    assert frozen(job["cgroup"]) == "1"
    assert [p["id"] for p in host.d.status["memguard"]["paused"]]

    # memory recovers (injected calm levels): the normal rule resumes it after 3 samples
    host.call(lambda: setattr(host.d.memguard.engine, "override", CALM))
    for _ in range(4):
        host.tick()
    assert frozen(job["cgroup"]) == "0"
    assert audit_events(host, "RESUME")

    # frozen again, then the daemon dies and `loadgovd --recover --force` thaws it
    host.call(lambda: setattr(host.d.memguard.engine, "override", (avail_mb() + 1500, avail_mb() + 3500)))
    host.call(lambda: setattr(host.d.memguard.engine, "cooldown_until", 0.0))
    wait_for(lambda: host.tick()["memguard"]["paused"], 10, "a second pause")
    assert frozen(job["cgroup"]) == "1"
    host.crash()
    r = subprocess.run(
        [sys.executable, "-m", "loadgov.daemon.main", "--recover", "--force", "--state-dir",
         str(host.tmp / "state"), "--socket", host.sock, "--lock-root", str(host.tmp / "lock")],
        capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert "registry cleared" in r.stdout and job["unit"] in r.stdout
    assert frozen(job["cgroup"]) == "0"
