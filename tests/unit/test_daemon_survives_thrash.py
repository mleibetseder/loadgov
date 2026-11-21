"""READY before the slow first tick, a watchdog thread that survives a stall, memory guard first."""

import asyncio
import signal

import pytest

from loadgov.daemon import main as dmain
from loadgov.daemon.main import Daemon, Heartbeat, watchdog_interval_s
from loadgov.host.procfs import HostFS


@pytest.fixture
def daemon(fake_host, tmp_path):
    host = fake_host("busy")
    fs = HostFS(proc_root=host.proc, cgroup_root=host.cgroup, lock_root=host.lock)
    d = Daemon(fs, str(tmp_path / "run" / "s.sock"), str(tmp_path / "run"), None, 0.05,
               state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None)  # fmt: skip
    return d


def test_ready_is_sent_before_the_first_tick(daemon, monkeypatch):
    events = []
    monkeypatch.setattr(dmain, "sd_notify", lambda m: events.append(m))
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: type("L", (), {"add_signal_handler": lambda *a: None})()
    )
    monkeypatch.setattr(signal, "signal", lambda *a: None)

    def slow_tick():  # stands in for a first tick blocked on docker ps or page faults
        events.append("tick")
        daemon._stop.set()
        return {}

    daemon.tick = slow_tick
    asyncio.run(asyncio.wait_for(daemon.run(), 20))
    assert events.index("READY=1") < events.index("tick")
    assert events[-1] == "STOPPING=1"


def test_heartbeat_pings_while_ticks_are_fresh_and_stops_when_stalled(monkeypatch):
    sent, now = [], [100.0]
    monkeypatch.setattr(dmain, "sd_notify", sent.append)
    hb = Heartbeat(5, stall_s=300, clock=lambda: now[0])
    hb.ping()
    now[0] += 299  # a long pressure stall that still ends in a good tick: alive
    hb.ping()
    assert sent == ["WATCHDOG=1", "WATCHDOG=1"]
    now[0] += 2  # no good tick for more than the limit: a real hang, let systemd act
    hb.ping()
    assert len(sent) == 2
    hb.beat()
    hb.ping()
    assert len(sent) == 3


def test_heartbeat_thread_pings_without_the_event_loop(monkeypatch):
    import threading

    got = threading.Event()
    monkeypatch.setattr(dmain, "sd_notify", lambda m: got.set())
    hb = Heartbeat(0.01)
    hb.start()
    try:
        assert got.wait(5)  # no asyncio loop runs here at all: the keep-alive does not need it
    finally:
        hb.stop()
        hb.join(5)


def test_watchdog_interval_is_a_quarter_of_watchdog_usec():
    assert watchdog_interval_s({"WATCHDOG_USEC": "120000000"}) == 30.0
    assert watchdog_interval_s({}) == 15.0
    assert watchdog_interval_s({"WATCHDOG_USEC": "x"}) == 15.0


def test_memory_guard_runs_right_after_the_sample(daemon):
    import os

    os.makedirs(daemon.run_dir, exist_ok=True)
    order = []
    for name in ("_memguard_step", "_expire", "fast_tick", "_watch_step", "_governor_step", "_backstop_step",
                 "_strays_step", "_history_step"):  # fmt: skip
        setattr(daemon, name, lambda *a, n=name: order.append(n))
    real = daemon.sampler.sample
    daemon.sampler.sample = lambda: order.append("sample") or real()
    daemon.sampler.prime()
    daemon.tick()
    assert order[:3] == ["sample", "_expire", "_memguard_step"]
    assert order.index("_memguard_step") < order.index("_backstop_step")


def test_unit_file_reserves_memory_and_keeps_oom_protection():
    import pathlib

    unit = (pathlib.Path(__file__).parents[2] / "packaging/systemd/loadgovd.service").read_text()
    for line in ("MemoryMin=96M", "OOMScoreAdjust=-900", "WatchdogSec=120", "TimeoutStartSec=300"):
        assert line in unit.splitlines()


# ---- the window between READY and the first tick: clients retry "no sample yet" -----------------------


class NoTickYet:
    """A unix socket that answers like a daemon that is listening but has not sampled yet."""

    def __init__(self, path, not_ready_replies):
        import socket
        import threading

        self.left, self.seen = not_ready_replies, []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        from loadgov.protocol import decode, encode

        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                req = decode(conn.makefile("rb").readline())
                self.seen.append(req["op"])
                if self.left > 0:
                    self.left -= 1
                    reply = {"ok": False, "error": "not_ready", "message": "no sample yet"}
                else:
                    reply = {"ok": True, "status": {"level": "OK", "at": "x"}}
                conn.sendall(encode(reply))

    def close(self):
        self.srv.close()


@pytest.fixture
def fast_ready(monkeypatch):
    from loadgov.cli import client

    monkeypatch.setattr(client, "READY_POLL_S", 0.01)
    return client


def test_level_wait_retries_not_ready_until_the_first_tick(fast_ready, monkeypatch, tmp_path):
    import io

    from loadgov.cli import loadctl

    sock = str(tmp_path / "s")
    srv = NoTickYet(sock, 3)
    try:
        a = loadctl._parser().parse_args(["--socket", sock, "level-wait", "5"])
        monkeypatch.setattr(loadctl, "format_hwmon", lambda s: "hwmon ok")
        assert loadctl.level_wait(a, sleep=lambda _s: None, out=io.StringIO()) == 0
    finally:
        srv.close()
    assert srv.seen == ["status"] * 5  # three not_ready answers retried, then two good samples in a row


def test_run_waits_for_the_first_tick_then_gives_up_with_exit_3(fast_ready, monkeypatch, tmp_path):
    from loadgov.cli import loadctl, run

    monkeypatch.setattr(fast_ready, "READY_WAIT_S", 0.2)
    sock = str(tmp_path / "s")
    srv = NoTickYet(sock, 10**6)  # never ticks
    launched = []
    try:
        a = loadctl._parser().parse_args(
            ["--socket", sock, "run", "--kind", "impl", "--project", "p", "--label", "l", "--", "true"]
        )
        deps = run.Deps(popen=lambda *x, **k: launched.append(x))
        assert run.run_command(a, deps) == 3
    finally:
        srv.close()
    assert launched == [] and len(srv.seen) > 2 and set(srv.seen) == {"status"}


def test_stale_sample_not_ready_is_not_retried_by_the_client(fast_ready, tmp_path):
    import socket
    import threading

    from loadgov.cli.client import request
    from loadgov.protocol import encode

    sock = str(tmp_path / "s")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.makefile("rb").readline()
            conn.sendall(encode({"ok": False, "error": "not_ready", "message": "stale sample (99 s old)"}))

    threading.Thread(target=serve, daemon=True).start()
    assert request(sock, "headroom", {"kind": "gate"})["error"] == "not_ready"
    srv.close()
