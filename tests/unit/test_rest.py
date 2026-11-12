"""REST control surface: auth, exposure, rate limit, owner semantics, supervision.

Every listener binds 127.0.0.1 on an ephemeral port; the "own addresses" set and the loopback
refusal are overridden through the test-only RestServer arguments, never through config.
"""

import asyncio
import contextlib
import json
import os
import secrets
import socket
import subprocess
import sys

import pytest

from loadgov.cli.client import request
from loadgov.config import Config, ConfigError, RestConfig, parse_config
from loadgov.daemon.main import Daemon
from loadgov.daemon.rest import RestServer, load_token
from loadgov.host.procfs import HostFS
from loadgov.protocol import VERSION, validate_status

TOKEN = secrets.token_urlsafe(32)
T0 = 1_762_800_000.0


class Clock:
    t = T0

    def __call__(self):
        return self.t


def tokens(tmp_path, token=TOKEN, mode=0o600):
    p = tmp_path / "tokens.env"
    p.write_text(f"# comment\nLOADGOV_REST_TOKEN={token}\n")
    p.chmod(mode)
    return str(p)


def make(tmp_path, fake_host=None, *, rest=None, token_file=None, **opts):
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True, exist_ok=True)
    (proc / "1" / "cgroup").write_text("0::/init.scope\n")
    fs = fake_host.fs if fake_host else HostFS(proc_root=str(proc), cgroup_root=str(tmp_path / "cg"))
    os.makedirs(tmp_path / "run", exist_ok=True)
    cfg = Config(rest=rest or RestConfig(enabled=True, bind="127.0.0.1", port=0, rate_per_s=1000))
    options = {"own_addresses": set(), "allow_loopback": True, "backoff": (0.05, 0.2), **opts}
    return Daemon(
        fs, str(tmp_path / "run" / "s.sock"), str(tmp_path / "run"), None, 0.2, cfg,
        str(tmp_path / "state"), str(tmp_path / "log"), None, clock=Clock(),
        tokens_file=token_file or tokens(tmp_path), rest_options=options,
    )  # fmt: skip


async def http(port, method="GET", path="/v1/status", body=None, token=TOKEN, raw_body=None, headers=()):
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
    head = [f"{method} {path} HTTP/1.1", "Host: x", f"Content-Length: {len(data)}", *headers]
    if token:
        head.append(f"Authorization: Bearer {token}")
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(("\r\n".join(head) + "\r\n\r\n").encode() + data)
    await w.drain()
    resp = await r.read()
    w.close()
    head_b, _, payload = resp.partition(b"\r\n\r\n")
    lines = head_b.decode().split("\r\n")
    hdrs = dict(ln.split(": ", 1) for ln in lines[1:])
    return int(lines[0].split()[1]), (json.loads(payload) if payload else None), hdrs


async def serving(d):
    task = asyncio.create_task(d.rest.supervise())
    for _ in range(200):
        if d.rest.state == "up":
            return task
        await asyncio.sleep(0.01)
    task.cancel()
    raise AssertionError(f"rest did not come up: {d.rest.status()}")


def run_rest(d, scenario):
    async def go():
        task = await serving(d)
        try:
            await scenario(d.rest.port)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def audit(d):
    return [json.loads(line) for line in open(d.audit.path)]


def test_401_without_or_with_wrong_token(tmp_path):
    d = make(tmp_path)

    async def scenario(port):
        code, _, hdrs = await http(port, token=None)
        assert (code, hdrs["WWW-Authenticate"]) == (401, "Bearer")
        assert (await http(port, token="x" * 43))[0] == 401
        assert (await http(port, token=TOKEN[:-1]))[0] == 401
        assert (await http(port, headers=["Authorization: Basic abc"], token=None))[0] == 401

    run_rest(d, scenario)


def test_403_when_source_not_allowlisted(tmp_path, fake_host):
    cfg = RestConfig(True, "127.0.0.1", 0, allow=("198.51.100.0/24",))
    d = make(tmp_path, fake_host("calm"), rest=cfg, peer_addr="203.0.113.1")

    async def scenario(port):
        assert (await http(port))[0] == 403

    run_rest(d, scenario)
    d = make(tmp_path, fake_host("calm"), rest=cfg, peer_addr="198.51.100.7")
    d.tick()
    run_rest(d, lambda port: _expect(http(port), 200))


async def _expect(coro, code):
    assert (await coro)[0] == code


def test_403_from_host_own_address_with_valid_token(tmp_path):
    d = make(tmp_path, own_addresses={"127.0.0.1"})
    run_rest(d, lambda port: _expect(http(port), 403))
    d = make(tmp_path, allow_loopback=False, own_addresses=set())  # loopback itself is refused
    assert d.rest._peer_refusal("127.0.0.1") and d.rest._peer_refusal("::1")
    assert d.rest._peer_refusal("::ffff:127.0.0.1") and d.rest._peer_refusal("198.51.100.50") is None


def test_429_over_rate(tmp_path):
    now = [100.0]
    d = make(tmp_path, rest=RestConfig(True, "127.0.0.1", 0, rate_per_s=2), clock=lambda: now[0])

    async def scenario(port):
        codes = [(await http(port))[0] for _ in range(4)]
        assert codes == [503, 503, 429, 429]  # 503: no sample yet; the first two used the burst
        now[0] += 1.0
        assert (await http(port))[0] == 503

    run_rest(d, scenario)


def test_set_silent_is_owner_via_rest_and_cancels_agent_loosening(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()
    agent = d.limits.set("agent", {"max_cores": 5.5}, "burst", None, None, None, None)
    assert agent["accepted"] and d.limits.effective()["max_cores"] == 5.5

    async def scenario(port):
        code, body, _ = await http(port, "POST", "/v1/set", {"profile": "silent", "reason": "t"})
        assert code == 200 and body["principal"] == "owner"
        assert d.limits.effective()["max_cores"] == 2.0
        # a later agent raise is refused: the owner profile is a human restriction
        r = d.limits.set("agent", {"max_cores": 5.5}, "again", None, None, None, None)
        assert r["refused"] is True
        code, body, _ = await http(port, "GET", "/v1/status")
        assert code == 200 and body["status"]["limits"]["profile"] == "silent"
        assert validate_status(body["status"]) == []
        assert body["status"]["rest"]["state"] == "up"

    run_rest(d, scenario)
    rows = audit(d)
    mine = [r for r in rows if r["principal"] == "owner" and r["source"] == "rest"]
    assert mine and mine[0]["peer"] == "127.0.0.1" and mine[0]["pid"] is None
    assert any(r["result"] == "cancelled" for r in rows)  # the agent raise from before


def test_ceiling_reset_and_action(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()

    async def scenario(port):
        c, b, _ = await http(port, "POST", "/v1/ceiling", {"profile": "silent", "reason": "tv"})
        assert c == 200 and d.limits_summary()["ceiling"] == "silent"
        c, b, _ = await http(port, "POST", "/v1/ceiling", {"clear": True, "reason": "off"})
        assert c == 200 and d.limits_summary()["ceiling"] is None
        c, b, _ = await http(port, "POST", "/v1/action", {"action": "profile=boost"})
        assert c == 200 and d.limits_summary()["profile"] == "boost"
        assert audit(d)[-1]["reason"] == "REST action profile=boost"
        c, b, _ = await http(port, "POST", "/v1/set", {"max_cores": 2.5, "reason": "x"})
        assert c == 200
        c, b, _ = await http(port, "POST", "/v1/reset", {"all": True, "reason": "x"})
        assert c == 200 and b["removed"]
        assert (await http(port, "POST", "/v1/action", {"action": "freeze"}))[0] == 400
        assert (await http(port, "POST", "/v1/action", {"action": "profile=nope"}))[0] == 400
        assert (await http(port, "POST", "/v1/ceiling", {"reason": "x"}))[0] == 400

    run_rest(d, scenario)


def test_bad_requests_and_limits(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()

    async def scenario(port):
        assert (await http(port, "POST", "/v1/set", raw_body=b"{nope"))[0] == 400
        assert (await http(port, "POST", "/v1/set", raw_body=b"[1]"))[0] == 400
        assert (await http(port, "POST", "/v1/set", {"max_cores": "2", "reason": "x"}))[0] == 400
        assert (await http(port, "POST", "/v1/set", {"profile": 3, "reason": "x"}))[0] == 400
        assert (await http(port, "POST", "/v1/set", {"owner": True, "profile": "silent", "reason": "x"}))[
            0
        ] == 400
        assert (await http(port, "POST", "/v1/set", {"max_cores": 99, "reason": "x"}))[0] == 400
        assert (await http(port, "POST", "/v1/set", {"profile": "silent", "reason": ""}))[0] == 400
        assert (await http(port, "POST", "/v1/set", headers=["Content-Length: \u00b2"], raw_body=b""))[
            0
        ] == 400
        big = b'{"reason":"' + b"a" * 20000 + b'"}'
        assert (await http(port, "POST", "/v1/set", raw_body=big))[0] == 413
        assert (await http(port, headers=["X-Pad: " + "a" * 9000]))[0] == 413
        assert (await http(port, "GET", "/v1/nothing"))[0] == 404
        c, _, h = await http(port, "GET", "/v1/set")
        assert (c, h["Allow"]) == (405, "POST")
        assert (await http(port, "POST", "/v1/status"))[0] == 405
        assert (await http(port, "POST", "/v1/prune-caches", {}))[0] == 404  # later phases
        assert d.limits_summary()["profile"] == "balanced"  # nothing above changed anything

    run_rest(d, scenario)


def test_set_without_reason_gets_a_default_and_is_audited(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()

    async def scenario(port):
        assert (await http(port, "POST", "/v1/set", {"profile": "silent"}))[0] == 200
        assert (await http(port, "POST", "/v1/ceiling", {"profile": "silent"}))[0] == 200
        assert (await http(port, "POST", "/v1/ceiling", {"clear": True}))[0] == 200
        assert (await http(port, "POST", "/v1/reset", {"all": True}))[0] == 200

    run_rest(d, scenario)
    rows = [r for r in audit(d) if r["source"] == "rest"]
    assert rows[0]["principal"] == "owner" and rows[0]["reason"] == "REST set from 127.0.0.1"
    assert {r["reason"] for r in rows} >= {"REST ceiling from 127.0.0.1", "REST reset from 127.0.0.1"}
    assert d.limits_summary()["profile"] == "silent"


def test_connection_cap_closes_extras_and_daemon_keeps_working(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"), rest=RestConfig(True, "127.0.0.1", 0, max_connections=4))
    d.tick()

    async def scenario(port):
        idle = []
        for src in ("192.0.2.1", "192.0.2.1", "192.0.2.2", "192.0.2.2"):  # two sources, half the cap each
            d.rest._peer_override = src
            idle.append(await asyncio.open_connection("127.0.0.1", port))
        await asyncio.sleep(0.1)  # all four are accepted and counted
        d.rest._peer_override = "192.0.2.3"
        r, w = await asyncio.open_connection("127.0.0.1", port)
        try:
            data = await asyncio.wait_for(r.read(), 1.0)  # over the total cap: closed at once, nothing served
        except ConnectionError:
            data = b""
        assert data == b""
        w.close()
        assert d.dispatch({"v": VERSION, "op": "status"})["ok"]  # the socket op path is unaffected
        d.tick()
        for _, iw in idle:  # freeing the slots lets requests through again
            iw.close()
        await asyncio.sleep(0.2)
        d.rest._peer_override = None
        assert (await http(port))[0] == 200

    run_rest(d, scenario)


def test_one_source_may_hold_at_most_half_of_the_connections(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"), rest=RestConfig(True, "127.0.0.1", 0, max_connections=4))
    d.tick()
    assert d.rest.source_cap == 2 and RestServer(d, RestConfig(max_connections=1)).source_cap == 1

    async def scenario(port):
        d.rest._peer_override = "192.0.2.1"
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
        await asyncio.sleep(0.1)
        r, w = await asyncio.open_connection("127.0.0.1", port)  # the same source's third: refused...
        try:
            data = await asyncio.wait_for(r.read(), 1.0)
        except ConnectionError:
            data = b""
        assert data == b""
        w.close()
        d.rest._peer_override = "192.0.2.2"  # ...while another source is still served (total 2 < 4)
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(b"GET /v1/status HTTP/1.1\r\n\r\n")
        assert b"401" in await asyncio.wait_for(r2.read(), 2.0)
        w2.close()
        for _, iw in idle:
            iw.close()

    run_rest(d, scenario)


def test_slowloris_header_is_closed_by_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("loadgov.daemon.rest.HEADER_TIMEOUT_S", 0.4)
    d = make(tmp_path)

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        start = asyncio.get_running_loop().time()
        with contextlib.suppress(ConnectionError):
            for ch in b"GET /v1/status HTTP/1.1\r\n":  # a byte every 0.15 s, never finishing
                w.write(bytes([ch]))
                await w.drain()
                await asyncio.sleep(0.15)
                if r.at_eof():
                    break
        data = b""
        with contextlib.suppress(ConnectionError):
            data = await asyncio.wait_for(r.read(), 2.0)
        assert data == b"" or data.startswith(b"HTTP/1.1 408")  # a reset may eat the reply
        assert asyncio.get_running_loop().time() - start < 1.5  # closed by the 0.4 s timeout
        with contextlib.suppress(ConnectionError):
            w.close()

    run_rest(d, scenario)


def test_unreadable_address_table_fails_closed(tmp_path):
    d = make(tmp_path, own_addresses=None)
    d.rest.fs = HostFS(proc_root=str(tmp_path / "empty"))  # no net/fib_trie
    assert d.rest._peer_refusal("203.0.113.1") == "host addresses unknown"
    assert "cannot read" in d.rest.status()["error"]
    net = tmp_path / "empty" / "net"
    net.mkdir(parents=True)
    (net / "fib_trie").write_text("  |-- 203.0.113.4\n     /32 host LOCAL\n")
    assert d.rest._peer_refusal("203.0.113.1") is None and d.rest.status()["error"] is None
    cfg = RestConfig(True, "192.0.2.10", 7420)
    d2 = make(tmp_path, rest=cfg, own_addresses=set(), allow_loopback=False)
    assert d2.rest._peer_refusal("192.0.2.10") is not None  # the configured bind is always "own"
    assert d2.rest._peer_refusal("198.51.100.50") is None


def test_max_connections_config():
    assert parse_config("[rest]\nmax_connections = 64\n").rest.max_connections == 64
    assert parse_config("").rest.max_connections == 16
    for bad in (0, 65, '"x"', "true"):
        with pytest.raises(ConfigError):
            parse_config(f"[rest]\nmax_connections = {bad}\n")


def test_slow_client_gets_408(tmp_path, monkeypatch):
    monkeypatch.setattr("loadgov.daemon.rest.READ_TIMEOUT_S", 0.3)
    d = make(tmp_path)

    async def scenario(port):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET /v1/status HTTP/1.1\r\n")  # never finishes the header block
        await w.drain()
        assert (await r.read()).startswith(b"HTTP/1.1 408")
        w.close()

    run_rest(d, scenario)


def test_bind_failure_never_stops_the_daemon_and_recovers(tmp_path, fake_host):
    host = fake_host("calm")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    d = make(tmp_path, host, rest=RestConfig(True, "127.0.0.1", port))

    async def go():
        main = asyncio.create_task(d.run())
        sock = d.socket_path
        for _ in range(100):
            if os.path.exists(sock) and d.status:
                break
            await asyncio.sleep(0.05)
        reply = await asyncio.to_thread(request, sock, "status")
        assert reply["ok"] and reply["status"]["rest"]["state"] == "down"
        assert reply["status"]["rest"]["error"]
        blocker.close()
        for _ in range(100):
            reply = await asyncio.to_thread(request, sock, "status")
            if reply["status"]["rest"]["state"] == "up":
                break
            await asyncio.sleep(0.05)
        assert reply["status"]["rest"]["state"] == "up" and reply["status"]["rest"]["error"] is None
        assert (await http(d.rest.port))[0] == 200
        d._stop.set()
        await main

    asyncio.run(go())


@pytest.mark.parametrize("bad", ["short", "world"])
def test_bad_token_file_keeps_rest_down_and_daemon_up(tmp_path, fake_host, bad):
    tf = tokens(tmp_path, "tooshort") if bad == "short" else tokens(tmp_path, mode=0o644)
    d = make(tmp_path, fake_host("calm"), token_file=tf)

    async def go():
        main = asyncio.create_task(d.run())
        for _ in range(100):
            if os.path.exists(d.socket_path) and d.status:
                break
            await asyncio.sleep(0.05)
        st = (await asyncio.to_thread(request, d.socket_path, "status"))["status"]
        assert st["rest"]["state"] == "down" and st["rest"]["enabled"] is True
        assert ("32" in st["rest"]["error"]) if bad == "short" else ("chmod 600" in st["rest"]["error"])
        assert TOKEN not in json.dumps(st) and "tooshort" not in json.dumps(st)
        d._stop.set()
        await main

    asyncio.run(go())


def test_disabled_listens_nowhere(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"), rest=RestConfig(enabled=False, bind="127.0.0.1", port=0))
    seen = []

    async def go():
        main = asyncio.create_task(d.run())
        for _ in range(100):
            if os.path.exists(d.socket_path) and d.status:
                break
            await asyncio.sleep(0.05)
        seen.append(await asyncio.to_thread(request, d.socket_path, "status"))
        pid = os.getpid()
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
        seen.append([ln for ln in out.splitlines() if f"pid={pid}," in ln])
        d._stop.set()
        await main

    asyncio.run(go())
    assert seen[0]["status"]["rest"] == {
        "enabled": False, "state": "disabled", "error": None, "bind": "127.0.0.1", "port": 0,
    }  # fmt: skip
    assert seen[1] == []  # `ss -ltnp`: this process has no TCP listener


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "127.0.0.1", "127.1.2.3", "::1", "localhost"])
def test_config_rejects_unsafe_bind(bind):
    with pytest.raises(ConfigError, match="rest.bind"):
        parse_config(f'[rest]\nbind = "{bind}"\n')


def test_config_rest_defaults_and_validation():
    c = parse_config("").rest
    assert (c.enabled, c.bind, c.port, c.allow, c.rate_per_s) == (
        False,
        None,
        7420,
        (),
        5.0,
    )  # no default bind
    with pytest.raises(ConfigError, match="rest.bind: required"):
        parse_config("[rest]\nenabled = true\n")
    c = parse_config(
        '[rest]\nenabled = true\nbind = "192.0.2.10"\nallow = ["198.51.100.50", "203.0.113.0/24"]\n'
    ).rest
    assert c.enabled and c.allow == ("198.51.100.50", "203.0.113.0/24")
    for bad in ("port = 0", 'allow = ["nope"]', "rate_per_s = 0", 'enabled = "yes"', "x = 1"):
        with pytest.raises(ConfigError):
            parse_config(f"[rest]\n{bad}\n")


def test_load_token(tmp_path):
    assert load_token(tokens(tmp_path)) == TOKEN.encode()
    p = tmp_path / "t2.env"
    p.write_text(f'export LOADGOV_REST_TOKEN="{TOKEN}"\n')
    p.chmod(0o600)
    assert load_token(str(p)) == TOKEN.encode()
    p.write_text("OTHER=1\n")
    with pytest.raises(Exception, match="not set"):
        load_token(str(p))
    with pytest.raises(Exception, match="cannot read"):
        load_token(str(tmp_path / "missing.env"))


def test_local_addresses_from_fixture_proc(tmp_path):
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True)
    (net / "fib_trie").write_text(
        "Local:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 0.0.0.0\n        /0 universe UNICAST\n"
        "     +-- 127.0.0.0/8 2 0 2\n        |-- 127.0.0.0\n           /8 host LOCAL\n"
        "        |-- 127.0.0.1\n           /32 host LOCAL\n"
        "     +-- 192.0.2.0/24 2 0 2\n        |-- 192.0.2.10\n           /32 host LOCAL\n"
        "        |-- 192.0.2.255\n           /32 link BROADCAST\n"
    )
    (net / "if_inet6").write_text("00000000000000000000000000000001 01 80 10 80 lo\n")
    assert HostFS(proc_root=str(tmp_path / "proc")).local_addresses() == {"127.0.0.1", "192.0.2.10", "::1"}


def test_own_address_read_via_hostfs_by_default(tmp_path):
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True)
    (net / "fib_trie").write_text("  |-- 203.0.113.4\n     /32 host LOCAL\n")
    d = make(tmp_path, own_addresses=None, peer_addr="203.0.113.4")
    d.rest.fs = HostFS(proc_root=str(tmp_path / "proc"))
    run_rest(d, lambda port: _expect(http(port), 403))


def test_daemon_process_passes_tokens_file_flag(tmp_path, fake_host):
    """The real entry point with REST enabled in config and a short token: up, REST down with a reason."""
    host = fake_host("calm")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[rest]\nenabled = true\nbind = "192.0.2.1"\nport = 17420\n')  # TEST-NET, never bound
    sock = tmp_path / "run" / "s.sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "loadgov.daemon.main", "--socket", str(sock),
         "--run-dir", str(tmp_path / "run"),
         "--compat-link", "", "--tick", "0.2", "--proc-root", host.proc, "--cgroup-root", host.cgroup,
         "--lock-root", host.lock, "--config", str(cfg), "--state-dir", str(tmp_path / "st"),
         "--log-dir", str(tmp_path / "log"), "--env-file", "",
         "--tokens-file", tokens(tmp_path, "short")],
        stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        for _ in range(100):
            if sock.exists():
                break
            import time

            time.sleep(0.05)
        r = request(str(sock), "status")
        assert r["status"]["rest"]["state"] == "down" and "32" in r["status"]["rest"]["error"]
        assert r["status"]["v"] == VERSION
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_cancel_holder_over_http_needs_confirm_and_a_holder(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()

    async def scenario(port):
        c, body, _ = await http(port, "POST", "/v1/cancel-holder", {"slot": "heavy"})
        assert (c, body["error"]) == (400, "bad_request")  # confirm is missing
        c, body, _ = await http(port, "POST", "/v1/cancel-holder", {"slot": "heavy", "confirm": True})
        assert (c, body["error"]) == (400, "invalid_args")  # nobody holds the slot
        assert (await http(port, "POST", "/v1/cancel-holder", {"confirm": True, "x": 1}))[0] == 400
        assert (await http(port, "GET", "/v1/cancel-holder"))[0] == 405

    run_rest(d, scenario)


def test_hold_over_http_is_owner_audited_and_validated(tmp_path, fake_host):
    d = make(tmp_path, fake_host("calm"))
    d.tick()

    async def scenario(port):
        c, body, _ = await http(port, "POST", "/v1/hold", {"on": True, "slot": "heavy"})
        assert (c, body["ok"], body["hold"]) == (200, True, "on")
        assert d.slots.owner_hold_reason() is not None
        assert d.limits.snapshot()["hold"] is not None
        c, body, _ = await http(port, "POST", "/v1/hold", {"on": False, "slot": "heavy"})
        assert (c, body["hold"]) == (200, "off")
        assert d.limits.snapshot()["hold"] is None
        assert (await http(port, "POST", "/v1/hold", {"slot": "heavy"}))[0] == 400  # on is required
        assert (await http(port, "POST", "/v1/hold", {"on": "yes"}))[0] == 400
        assert (await http(port, "POST", "/v1/hold", {"on": True, "slot": "nope"}))[0] == 400
        assert (await http(port, "POST", "/v1/hold", {"on": True, "x": 1}))[0] == 400
        assert (await http(port, "GET", "/v1/hold"))[0] == 405
        assert (await http(port, "POST", "/v1/hold", {"on": True}, token="x" * 43))[0] == 401

    run_rest(d, scenario)
    holds = [x for x in d.audit.tail(20) if x.get("op") == "hold"]
    assert holds and all(x["source"] == "rest" and x["principal"] == "owner" for x in holds)
    assert "REST hold from" in holds[0]["reason"]


@pytest.mark.parametrize("bind", ["172.16.0.1", "172.17.0.1", "172.31.255.254"])
def test_config_refuses_docker_bridge_range_unless_allowed(bind):
    with pytest.raises(ConfigError, match="172.16.0.0/12"):
        parse_config(f'[rest]\nbind = "{bind}"\n')
    cfg = parse_config(f'[rest]\nbind = "{bind}"\nallow_private_bridge = true\n').rest
    assert cfg.bind == bind and cfg.allow_private_bridge is True
    assert parse_config('[rest]\nbind = "172.32.0.1"\n').rest.bind == "172.32.0.1"  # outside /12
    assert parse_config('[rest]\nbind = "192.0.2.10"\n').rest.allow_private_bridge is False
    with pytest.raises(ConfigError, match="allow_private_bridge"):
        parse_config('[rest]\nallow_private_bridge = "yes"\n')
