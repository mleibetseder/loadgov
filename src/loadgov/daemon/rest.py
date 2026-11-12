"""REST control surface for Home Assistant: a minimal HTTP/1.1 server on asyncio, stdlib only.

One request per connection (`Connection: close`), JSON bodies only. Every caller acts as the owner,
through the same `Daemon._mutate` path as the socket, so restriction rules and the audit apply
identically. The listener is a supervised task: a bind or token problem never stops the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time

from loadgov.config import ConfigError, RestConfig, check_rest_bind
from loadgov.host.procfs import HostFS

log = logging.getLogger("loadgovd.rest")

DEFAULT_TOKENS_FILE = "/etc/loadgov/tokens.env"
TOKEN_VAR = "LOADGOV_REST_TOKEN"
MIN_TOKEN_CHARS = 43  # secrets.token_urlsafe(32): 32 random bytes
MAX_HEADER_BYTES = 8 * 1024
MAX_BODY_BYTES = 16 * 1024
READ_TIMEOUT_S = 10  # whole request, body included
HEADER_TIMEOUT_S = 5
BACKOFF_S = (2.0, 60.0)

REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict", 413: "Payload Too Large",
    429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable",
}  # fmt: skip
SET_KEYS = {
    "profile", "max_cores", "target_cpu_pct", "load_max", "max_implementers", "ram_floor_gb",
    "ttl", "until", "reason", "project",
}  # fmt: skip
POST_KEYS = {
    "/v1/set": SET_KEYS,
    "/v1/reset": {"params", "all", "reason"},
    "/v1/ceiling": {"profile", "reason", "ttl", "until", "clear"},
    "/v1/action": {"action", "reason"},
    "/v1/cancel-holder": {"slot", "confirm", "reason"},
    "/v1/hold": {"on", "slot", "reason"},
    "/v1/freeze": {"project", "reason", "ttl"},
    "/v1/unfreeze": {"project", "reason"},
    "/v1/reap": {"reason"},
}
ACTION_RE = re.compile(r"^profile=([a-z][a-z0-9_-]*)$")


class TokenError(Exception):
    pass


def read_env_value(path: str, var: str) -> str | None:
    """Read `var` from a 0600 env file (None when unset); raise TokenError when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as e:
        raise TokenError(f"cannot read tokens file {path}: {e.strerror}") from None
    with os.fdopen(fd) as f:
        mode = os.fstat(f.fileno()).st_mode & 0o777
        if mode & 0o077:
            raise TokenError(f"tokens file {path} is accessible by group/other (mode {mode:04o}); chmod 600")
        try:
            lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError) as e:
            raise TokenError(f"cannot read tokens file {path}: {e}") from None
    value = None
    for line in lines:
        line = line.strip().removeprefix("export ").strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == var:
            value = val.strip().strip("\"'")
    return value


def load_token(path: str) -> bytes:
    """Read LOADGOV_REST_TOKEN from a 0600 env file; raise TokenError with a clear message."""
    value = read_env_value(path, TOKEN_VAR)
    if value is None:
        raise TokenError(f"{TOKEN_VAR} is not set in {path}")
    if len(value) < MIN_TOKEN_CHARS or not value.isascii() or any(c.isspace() for c in value):
        raise TokenError(
            f"{TOKEN_VAR} must be at least 32 random bytes ({MIN_TOKEN_CHARS}+ URL-safe characters)"
        )
    return value.encode()


class RateLimiter:
    """Token bucket over all callers: `rate` requests per second, burst of one second."""

    def __init__(self, rate: float, clock=time.monotonic):
        self.rate, self.clock = rate, clock
        self.burst = max(1.0, rate)
        self.tokens, self.last = self.burst, clock()

    def allow(self) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def _norm(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip


def status_for(reply: dict) -> int:
    if reply.get("ok"):
        return 200
    if reply.get("refused"):
        return 409
    return {"not_ready": 503, "internal": 500}.get(reply.get("error"), 400)


class RestServer:
    """HTTP handler plus supervisor. `daemon` needs `.dispatch(req)` and `.rest_call(op, args, peer)`."""

    def __init__(
        self,
        daemon,
        cfg: RestConfig,
        tokens_file: str = DEFAULT_TOKENS_FILE,
        fs: HostFS | None = None,
        *,
        clock=time.monotonic,
        backoff: tuple[float, float] = BACKOFF_S,
        # Test-only: never reachable from config or the command line.
        own_addresses: set[str] | None = None,
        allow_loopback: bool = False,
        peer_addr: str | None = None,
    ):
        self.daemon, self.cfg, self.tokens_file = daemon, cfg, tokens_file
        self.fs = fs or getattr(daemon, "fs", None) or HostFS()
        self.backoff = backoff
        self._own, self._allow_loopback, self._peer_override = own_addresses, allow_loopback, peer_addr
        self._allow = [ipaddress.ip_network(a, strict=False) for a in cfg.allow]
        self._limiter = RateLimiter(cfg.rate_per_s, clock)
        self._token = b""
        self._active = 0
        self._per_source: dict[str, int] = {}
        self.state, self.error, self.port = ("down" if cfg.enabled else "disabled"), None, cfg.port

    @property
    def source_cap(self) -> int:
        """Connections one source address may hold at once: half of `max_connections`, at least one."""
        return max(1, self.cfg.max_connections // 2)

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "state": self.state,
            "error": self.error,
            "bind": self.cfg.bind,
            "port": self.port,
        }

    def _down(self, error: str) -> None:
        if (self.state, self.error) != ("down", error):
            log.warning("rest down: %s", error)
        self.state, self.error = "down", error

    async def supervise(self) -> None:
        """Run until cancelled; any failure retries with backoff and leaves the daemon alone."""
        delay = self.backoff[0]
        while True:
            try:
                if not self._allow_loopback:
                    check_rest_bind(self.cfg.bind, self.cfg.allow_private_bridge)
                self._token = load_token(self.tokens_file)
                server = await asyncio.start_server(
                    self._handle, self.cfg.bind, self.cfg.port, limit=MAX_HEADER_BYTES
                )
            except (TokenError, ConfigError, OSError) as e:
                self._down(str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep supervising whatever went wrong
                log.exception("rest start failed")
                self._down(f"{type(e).__name__}: {e}")
            else:
                self.port = server.sockets[0].getsockname()[1]
                self.state, self.error = "up", None
                log.info("rest listening on %s:%d", self.cfg.bind, self.port)
                delay = self.backoff[0]
                try:
                    await server.serve_forever()
                except asyncio.CancelledError:
                    server.close()
                    raise
                except Exception as e:
                    log.exception("rest listener failed")
                    self._down(f"{type(e).__name__}: {e}")
                finally:
                    server.close()
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff[1])

    # --- request handling ---

    def _peer_refusal(self, peer: str) -> str | None:
        ip = _norm(peer)
        if ip is None:
            return "unparseable source address"
        try:
            own = self._own if self._own is not None else self.fs.local_addresses()
        except OSError as e:  # fail closed: without the address table nobody can be told from the host
            self.error = f"cannot read the host's addresses ({e}); refusing requests"
            return "host addresses unknown"
        if self.error and self.error.startswith("cannot read the host's addresses"):
            self.error = None
        if not self._allow_loopback:  # (test-only mode binds loopback, which is the peer)
            own = {*own, self.cfg.bind}
        if (ip.is_loopback and not self._allow_loopback) or str(ip) in own:
            return "requests from this host are refused"  # even with a valid token
        if self._allow and not any(ip in net for net in self._allow):
            return "source address is not allowed"
        return None

    def _authorized(self, header: str | None) -> bool:
        scheme, _, given = (header or "").partition(" ")
        if scheme.lower() != "bearer":
            return False
        try:
            return hmac.compare_digest(given.strip().encode("latin-1"), self._token)
        except UnicodeEncodeError:
            return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        src = self._peer_override or (peername[0] if peername else "")
        # over the cap, or over one source's share of it (half, at least one): close at once, read nothing
        if self._active >= self.cfg.max_connections or self._per_source.get(src, 0) >= self.source_cap:
            writer.transport.abort()
            return
        self._active += 1
        self._per_source[src] = self._per_source.get(src, 0) + 1
        try:
            await self._handle_one(reader, writer)
        finally:
            self._active -= 1
            if self._per_source[src] <= 1:
                del self._per_source[src]
            else:
                self._per_source[src] -= 1

    async def _handle_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peername = writer.get_extra_info("peername")
            peer = self._peer_override or (peername[0] if peername else "")
            try:
                code, body, extra = await asyncio.wait_for(self._serve(reader, peer), READ_TIMEOUT_S)
            except TimeoutError:
                code, body, extra = 408, {"ok": False, "error": "timeout", "message": "read timeout"}, {}
            except Exception:  # a bad request must not take the listener down
                log.exception("rest request failed")
                code, body, extra = 500, {"ok": False, "error": "internal", "message": "internal error"}, {}
            payload = json.dumps(body, separators=(",", ":")).encode()
            head = [f"HTTP/1.1 {code} {REASONS.get(code, 'Error')}", "Content-Type: application/json",
                    f"Content-Length: {len(payload)}", "Connection: close"]  # fmt: skip
            head += [f"{k}: {v}" for k, v in extra.items()]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + payload)
            await writer.drain()
            if code >= 400 and code != 408:  # swallow what the client still sends, so close() cannot reset
                with contextlib.suppress(TimeoutError, ConnectionError, OSError):
                    await asyncio.wait_for(reader.read(65536), 0.2)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    @staticmethod
    def _err(code: int, error: str, message: str, extra: dict | None = None):
        return code, {"ok": False, "error": error, "message": message}, extra or {}

    async def _serve(self, reader: asyncio.StreamReader, peer: str):
        why = self._peer_refusal(peer)
        if why:
            log.warning("rest: refused %s: %s", peer, why)
            return self._err(403, "forbidden", why)  # nothing of the request is read or trusted
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as e:
            if isinstance(e, asyncio.LimitOverrunError):
                return self._err(413, "too_large", f"header block over {MAX_HEADER_BYTES} bytes")
            return self._err(400, "bad_request", "incomplete request")
        if len(raw) > MAX_HEADER_BYTES:
            return self._err(413, "too_large", f"header block over {MAX_HEADER_BYTES} bytes")
        try:
            lines = raw[:-4].decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ")
            headers = {}
            for ln in lines[1:]:
                k, sep, v = ln.partition(":")
                if not sep or not k or k != k.strip():
                    raise ValueError(ln)
                headers[k.lower()] = v.strip()
        except ValueError:
            return self._err(400, "bad_request", "malformed request")
        if not version.startswith("HTTP/1."):
            return self._err(400, "bad_request", "HTTP/1.x only")
        if not self._authorized(headers.get("authorization")):
            log.warning("rest: 401 from %s", peer)  # never the presented token
            return self._err(401, "unauthorized", "bearer token required", {"WWW-Authenticate": "Bearer"})
        if not self._limiter.allow():
            return self._err(429, "rate_limited", "too many requests", {"Retry-After": "1"})
        path = target.split("?", 1)[0]
        if path == "/v1/status":
            if method != "GET":
                return self._err(405, "method_not_allowed", "use GET", {"Allow": "GET"})
            reply = self.daemon.dispatch({"v": 1, "op": "status"})
            return status_for(reply), reply, {}
        if path not in POST_KEYS:
            return self._err(404, "not_found", f"no such path {path}")
        if method != "POST":
            return self._err(405, "method_not_allowed", "use POST", {"Allow": "POST"})
        args = await self._read_json(reader, headers)
        if isinstance(args, tuple):  # an error response
            return args
        unknown = sorted(set(args) - POST_KEYS[path])
        if unknown:
            return self._err(400, "bad_request", f"unknown field(s): {', '.join(unknown)}")
        op = self._op_for(path, args, peer)
        if len(op) == 3:  # an error response
            return op
        reply = self.daemon.rest_call(op[0], op[1], peer)
        return status_for(reply), reply, {}

    async def _read_json(self, reader: asyncio.StreamReader, headers: dict):
        if "transfer-encoding" in headers:
            return self._err(400, "bad_request", "Transfer-Encoding is not supported; send Content-Length")
        cl = headers.get("content-length", "0")
        if not (cl.isascii() and cl.isdigit()):
            return self._err(400, "bad_request", "bad Content-Length")
        n = int(cl)
        if n > MAX_BODY_BYTES:
            return self._err(413, "too_large", f"body over {MAX_BODY_BYTES} bytes")
        try:
            data = await reader.readexactly(n) if n else b""
        except asyncio.IncompleteReadError:
            return self._err(400, "bad_request", "body shorter than Content-Length")
        if not data.strip():
            return {}
        try:
            obj = json.loads(data)
        except (ValueError, RecursionError):
            return self._err(400, "bad_request", "body is not valid JSON")
        if not isinstance(obj, dict):
            return self._err(400, "bad_request", "body must be a JSON object")
        return obj

    def _op_for(self, path: str, args: dict, peer: str):
        """(op, args) for the daemon, or an error response tuple."""
        if path in ("/v1/set", "/v1/reset", "/v1/ceiling") and args.get("reason") is None:
            args["reason"] = f"REST {path.rsplit('/', 1)[1]} from {peer}"  # the socket still requires one
        if path in ("/v1/freeze", "/v1/unfreeze", "/v1/reap"):
            return path.rsplit("/", 1)[1], args
        if path == "/v1/cancel-holder":
            return "cancel_holder", args
        if path == "/v1/hold":
            on = args.pop("on", None)
            if not isinstance(on, bool):
                return self._err(400, "bad_request", "on (boolean) is required")
            return "hold", {**args, "state": "on" if on else "off"}
        if path == "/v1/set":
            return "set", args
        if path == "/v1/reset":
            return "reset", args
        if path == "/v1/ceiling":
            clear = args.pop("clear", None)
            if clear is not None and not isinstance(clear, bool):
                return self._err(400, "bad_request", "clear must be a boolean")
            if clear:
                if set(args) - {"reason"}:
                    return self._err(400, "bad_request", "clear takes only a reason")
                return "ceiling_clear", args
            if not isinstance(args.get("profile"), str):
                return self._err(400, "bad_request", "profile (string) or clear: true is required")
            return "ceiling_set", args
        action = args.get("action")  # /v1/action
        m = ACTION_RE.match(action) if isinstance(action, str) else None
        if not m:
            return self._err(400, "bad_request", "action must be profile=<name>")
        reason = args.get("reason")
        if reason is not None and not isinstance(reason, str):
            return self._err(400, "bad_request", "reason must be a string")
        return "set", {"profile": m.group(1), "reason": reason or f"REST action {action}"}
