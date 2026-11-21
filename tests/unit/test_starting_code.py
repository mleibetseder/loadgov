"""The start-up error code: the daemon and the client library agree on it; docker ps never blocks."""

import threading
import time

from test_daemon_survives_thrash import NoTickYet
from test_headroom import make_daemon

from loadgov.cli import client
from loadgov.host.docker import NAMES_TTL_S, Docker
from loadgov.protocol import STARTING


def test_daemon_and_client_share_the_starting_code(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.status = None  # before the first sample
    for req in ({"op": "status"}, {"op": "headroom", "args": {"kind": "impl"}}):
        reply = d.dispatch({"v": 1, **req})
        assert reply["error"] == STARTING == "starting"
        assert client.is_starting(reply)


def test_client_still_retries_the_old_not_ready_text():
    assert client.is_starting({"ok": False, "error": "not_ready", "message": "no sample yet"})
    assert not client.is_starting({"ok": False, "error": "not_ready", "message": "stale sample (9 s old)"})
    assert not client.is_starting({"ok": True})


def test_request_retries_old_text_and_ready_wait_zero_does_not(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "READY_POLL_S", 0.01)
    sock = str(tmp_path / "s")
    srv = NoTickYet(sock, 2)  # answers the old not_ready "no sample yet" twice
    try:
        assert client.request(sock, "status")["ok"] is True
        assert srv.seen == ["status"] * 3
        srv.left = 1
        assert client.request(sock, "status", ready_wait=0)["error"] == "not_ready"  # returned, not retried
    finally:
        srv.close()


class SlowDocker:
    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, argv):
        self.calls += 1
        self.release.wait(5)
        return 0, "web-1\nweb-2\n;bad\n"


def test_docker_ps_runs_off_the_caller_and_the_last_list_is_served():
    runner, now = SlowDocker(), [100.0]
    d = Docker(runner, clock=lambda: now[0])
    t0 = time.monotonic()
    assert d.cached_names() == ([], None)  # no list yet, and the blocked docker ps did not hold the call
    assert d.cached_names() == ([], None)  # a refresh is already running: no second one
    assert time.monotonic() - t0 < 1
    runner.release.set()
    for _ in range(200):
        names, age = d.cached_names()
        if age is not None:
            break
        time.sleep(0.01)
    assert names == ["web-1", "web-2"] and age == 0
    assert runner.calls == 1
    now[0] += NAMES_TTL_S - 1
    assert d.cached_names() == (["web-1", "web-2"], NAMES_TTL_S - 1)  # fresh enough: no new docker ps
    now[0] += 1
    runner.release.clear()
    names, age = d.cached_names()  # stale: a refresh starts, the old list (with its age) is still served
    assert names == ["web-1", "web-2"] and age == NAMES_TTL_S
    runner.release.set()


def test_failed_refresh_keeps_the_last_list_and_clears_busy():
    calls = []

    def runner(argv):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return 0, "a1"

    now = [0.0]
    d = Docker(runner, spawn=lambda f: f(), clock=lambda: now[0])
    d.cached_names()
    assert d.cached_names()[0] == ["a1"]
    now[0] = 100.0
    assert d.cached_names()[0] == ["a1"]  # the refresh raised; the old list stays
    assert d.cached_names()[0] == ["a1"] and len(calls) == 3  # and the next call retries


def test_spawn_failure_clears_busy_serves_the_last_list_and_retries():
    fail = [False]
    now = [0.0]

    def spawn(fn):
        if fail[0]:
            fail[0] = False
            raise RuntimeError("can't start new thread")
        fn()

    d = Docker(lambda argv: (0, "a1"), spawn=spawn, clock=lambda: now[0])
    assert d.cached_names()[0] == ["a1"]
    now[0] = 100.0
    fail[0] = True
    assert d.cached_names()[0] == ["a1"]  # spawn raised: the old list is served, no exception
    assert d.cached_names() == (["a1"], 0.0)  # busy was cleared: the next call refreshed
