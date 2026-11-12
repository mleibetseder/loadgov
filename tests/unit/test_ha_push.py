"""Home Assistant webhook push: a local HTTP server on 127.0.0.1, fake sleeps, no other host."""

import asyncio
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from loadgov.config import ConfigError, HaConfig, parse_config
from loadgov.daemon.ha_push import HaPush
from loadgov.engines.watch import Watch

GOLDEN = Path(__file__).parent / "golden" / "ha-alert-cpu.json"
WEBHOOK_ID = "loadgov-secret-webhook-id-0123456789"
T0 = 1_762_800_000.0


class Server:
    def __init__(self, status=200):
        self.requests: list[tuple[str, dict, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append((self.path, dict(self.headers), body))
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass

        self.status = status
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.fixture
def tokens(tmp_path):
    p = tmp_path / "tokens.env"
    p.write_text(f"LOADGOV_HA_WEBHOOK_ID={WEBHOOK_ID}\n")
    p.chmod(0o600)
    return str(p)


def cpu_alert():
    w = Watch(lambda: T0)
    sample = {
        "memory": {"avail_mb": 7000}, "swap": {"used_mb": 120}, "disk": {"free_gb": 48.0},
        "slots": {}, "heavy_slot": {"stuck": False},
    }  # fmt: skip
    top = [{"pid": 101, "comm": "rustc", "pcpu": 97.1, "rss_mb": 6800}]
    out = []
    for busy in (82, 83, 75):
        out += w.evaluate(sample, busy, 65, "balanced", None, top)
    assert len(out) == 1
    return out[0]


def drain(push, until, timeout=5.0):
    async def go():
        task = asyncio.create_task(push.run())
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_disabled_sends_nothing_and_queues_nothing(server, tokens):
    push = HaPush(HaConfig(False, server.url), tokens)
    push.enqueue(cpu_alert())
    assert push.status() == {"enabled": False, "state": "disabled", "queued": 0, "dropped": 0, "failed": 0,
                             "last_ok": None, "last_error": None}  # fmt: skip
    drain(push, lambda: False, timeout=0.2)
    assert server.requests == []


def test_enabled_posts_the_golden_payload(server, tokens):
    push = HaPush(HaConfig(True, server.url), tokens, clock=lambda: T0)
    push.enqueue(cpu_alert())
    drain(push, lambda: server.requests)
    path, headers, body = server.requests[0]
    assert path == f"/api/webhook/{WEBHOOK_ID}"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == json.loads(GOLDEN.read_text())
    st = push.status()
    assert st["state"] == "up" and st["queued"] == 0 and st["last_ok"] == "2025-11-10T18:40:00Z"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_down_retries_three_times_with_backoff_and_never_blocks_the_tick(tokens, caplog):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    push = HaPush(HaConfig(True, f"http://127.0.0.1:{free_port()}"), tokens, sleep=fake_sleep)
    t = time.monotonic()
    push.enqueue(cpu_alert())  # what the tick does: append and return
    assert time.monotonic() - t < 0.05
    with caplog.at_level(logging.DEBUG):
        drain(push, lambda: push.state == "down")
    assert sleeps == [1.0, 4.0, 15.0]
    st = push.status()
    assert st["state"] == "down" and st["queued"] == 0 and st["last_ok"] is None
    assert "ConnectionRefusedError" in st["last_error"]
    assert WEBHOOK_ID not in json.dumps(st) + caplog.text


def test_http_error_status_is_a_failed_delivery(tokens):
    s = Server(status=404)
    try:
        push = HaPush(HaConfig(True, s.url), tokens, sleep=lambda _s: asyncio.sleep(0))
        push.enqueue(cpu_alert())
        drain(push, lambda: push.state == "down")
        assert len(s.requests) == 4 and push.last_error == "HTTP 404"
        s.status = 200  # the next event goes through and the state recovers
        push.enqueue(cpu_alert())
        drain(push, lambda: push.state == "up")
        assert len(s.requests) == 5
    finally:
        s.close()


def test_queue_is_bounded_drops_oldest_and_counts(server, tokens):
    push = HaPush(HaConfig(True, server.url), tokens)
    for i in range(70):
        push.enqueue({"type": "alert", "key": f"k{i}", "at": "t"})
    assert push.status()["queued"] == 64 and push.status()["dropped"] == 6
    drain(push, lambda: len(server.requests) == 64)
    keys = [json.loads(b)["key"] for _, _, b in server.requests]
    assert keys[0] == "k6" and keys[-1] == "k69"  # the six oldest were dropped


@pytest.mark.parametrize("content", [None, "OTHER=1\n", "LOADGOV_HA_WEBHOOK_ID=../../etc\n"])
def test_missing_or_bad_webhook_id_is_down_without_retries(server, tmp_path, content):
    p = tmp_path / "t.env"
    if content is not None:
        p.write_text(content)
        p.chmod(0o600)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    push = HaPush(HaConfig(True, server.url), str(p), sleep=fake_sleep)
    push.enqueue(cpu_alert())
    drain(push, lambda: push.state == "down")
    assert sleeps == [] and server.requests == [] and push.last_error


def test_webhook_id_file_must_be_private(server, tokens):
    Path(tokens).chmod(0o644)
    push = HaPush(HaConfig(True, server.url), tokens)
    push.enqueue(cpu_alert())
    drain(push, lambda: push.state == "down")
    assert "chmod 600" in push.last_error and server.requests == []


@pytest.mark.parametrize(
    "url",
    ["ftp://h", "homeassistant.local", "http://", "http://u:p@h", "http://h?x=1", "file:///etc/passwd", 5],
)
def test_config_refuses_a_bad_ha_url(url):
    with pytest.raises(ConfigError):
        parse_config(f"[ha]\nurl = {json.dumps(url)}\n")


def test_config_defaults_and_valid_urls():
    cfg = parse_config("")
    assert cfg.ha == HaConfig(False, "http://homeassistant.local:8123") and cfg.compat.alert_log_paths == ()
    assert (
        parse_config('[ha]\nenabled = true\nurl = "https://ha.example:8123/"\n').ha.url
        == "https://ha.example:8123"
    )
    with pytest.raises(ConfigError):
        parse_config('[ha]\nwebhook_id = "x"\n')  # the id never lives in the TOML
    with pytest.raises(ConfigError):
        parse_config('[compat]\nalert_log_paths = ["relative.log"]\n')


def test_an_unencodable_event_is_counted_and_does_not_stop_the_push(server, tokens):
    push = HaPush(HaConfig(True, server.url), tokens)
    push.enqueue({"type": "alert", "key": "bad", "at": "t", "x": object()})
    push.enqueue(cpu_alert())
    drain(push, lambda: server.requests)
    st = push.status()
    assert st["failed"] == 1 and len(server.requests) == 1 and st["state"] == "up"
    assert st["queued"] == 0


def test_a_run_that_dies_shows_down(tokens, monkeypatch):
    push = HaPush(HaConfig(True, "http://127.0.0.1:9"), tokens)

    async def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(push, "_loop", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(push.run())
    assert push.state == "down" and "stopped" in push.last_error
