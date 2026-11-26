"""The per-host web UI listener: the built app (static files) and read-only JSON, stdlib asyncio only.

A listener of its own, apart from `[rest]`: REST is a bearer-token control surface that can change the host,
this one has no token and no write path at all. Safety is the source allowlist (the sign-in proxy), GET and
HEAD only, and files resolved strictly inside the build directory. One request per connection.
Trust model: docs/protocol.md "Web UI". Audit-grade at best, not a security boundary (AGENTS.md rule 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import stat
import time
from urllib.parse import parse_qs, unquote

from loadgov import __version__
from loadgov.config import ConfigError, UiConfig, check_rest_bind
from loadgov.daemon import runs_api
from loadgov.daemon.rest import (
    BACKOFF_S,
    HEADER_TIMEOUT_S,
    MAX_HEADER_BYTES,
    READ_TIMEOUT_S,
    REASONS,
    RateLimiter,
    _norm,
)
from loadgov.daemon.runs_api import (  # noqa: F401 - re-exported: tests and callers import these from here
    LOG_DEFAULT,
    LOG_MAX,
    RUNS_MAX,
    Reply,
    decode_clean,
    levels_of,
)
from loadgov.workflow.records import RunStore, default_runs_dir

log = logging.getLogger("loadgovd.ui")

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 1024
INDEX = "index.html"
HASHED = re.compile(r"(-[A-Z0-9]{8}|\.[0-9a-f]{16,})\.[a-z0-9]+$")  # Angular build hashes in the file name

TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}  # fmt: skip
# Angular injects component styles as <style> elements at run time, so style-src needs 'unsafe-inline';
# scripts stay strict (the build must not inline them: `optimization.styles.inlineCritical = false`).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Resource-Policy": "same-origin",
}
NOT_BUILT = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>loadgov UI not built</title>'
    "</head><body><h1>The loadgov UI is not built</h1><p>The daemon serves the web UI from its build "
    "directory, and no build is there. Build it and install it (ui/README.md, docs/deployment.md), then "
    "reload this page.</p></body></html>"
)


UI_REASONS = {**REASONS, 304: "Not Modified", 422: "Unprocessable Entity"}


class UiServer:
    """HTTP handler plus supervisor. `daemon` needs `.dispatch(req)` and `.fs`; nothing here mutates it."""

    def __init__(
        self,
        daemon,
        cfg: UiConfig,
        *,
        clock=time.monotonic,
        backoff: tuple[float, float] = BACKOFF_S,
        allow_loopback: bool = False,  # test-only: bind loopback
        peer_addr: str | None = None,  # test-only
    ):
        self.daemon, self.cfg, self.backoff = daemon, cfg, backoff
        self._allow_loopback, self._peer_override = allow_loopback, peer_addr
        self._allow = [ipaddress.ip_network(a, strict=False) for a in cfg.allow]
        self._limiter = RateLimiter(cfg.rate_per_s, clock)
        self._active, self._per_source = 0, {}
        self.state, self.error, self.port = ("down" if cfg.enabled else "disabled"), None, cfg.port

    @property
    def built(self) -> bool:
        return os.path.isfile(os.path.join(self.cfg.dir, INDEX))

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "state": self.state,
            "error": self.error,
            "bind": self.cfg.bind,
            "port": self.port,
            "built": self.built if self.cfg.enabled else False,
        }

    def _down(self, error: str) -> None:
        if (self.state, self.error) != ("down", error):
            log.warning("ui down: %s", error)
        self.state, self.error = "down", error

    async def supervise(self) -> None:
        delay = self.backoff[0]
        while True:
            try:
                if not self._allow_loopback:
                    check_rest_bind(self.cfg.bind, self.cfg.allow_private_bridge)
                server = await asyncio.start_server(
                    self._handle, self.cfg.bind, self.cfg.port, limit=MAX_HEADER_BYTES
                )
            except (ConfigError, OSError) as e:
                self._down(str(e))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep supervising whatever went wrong
                log.exception("ui start failed")
                self._down(f"{type(e).__name__}: {e}")
            else:
                self.port = server.sockets[0].getsockname()[1]
                self.state, self.error = "up", None
                log.info("ui listening on %s:%d", self.cfg.bind, self.port)
                if not self.built:
                    log.warning("ui: no build at %s (serving the 'not built' page)", self.cfg.dir)
                delay = self.backoff[0]
                try:
                    await server.serve_forever()
                except asyncio.CancelledError:
                    server.close()
                    raise
                except Exception as e:
                    log.exception("ui listener failed")
                    self._down(f"{type(e).__name__}: {e}")
                finally:
                    server.close()
                continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff[1])

    # --- connections ---

    def _peer_refusal(self, peer: str) -> str | None:
        ip = _norm(peer)
        if ip is None:
            return "unparseable source address"
        if not any(ip in net for net in self._allow):  # the allowlist is never empty when enabled
            return "source address is not allowed"
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        src = self._peer_override or (peername[0] if peername else "")
        cap = max(1, self.cfg.max_connections // 2)
        if self._active >= self.cfg.max_connections or self._per_source.get(src, 0) >= cap:
            writer.transport.abort()
            return
        self._active += 1
        self._per_source[src] = self._per_source.get(src, 0) + 1
        try:
            await self._handle_one(reader, writer, src)
        finally:
            self._active -= 1
            if self._per_source[src] <= 1:
                del self._per_source[src]
            else:
                self._per_source[src] -= 1

    async def _handle_one(self, reader, writer, peer: str) -> None:
        method = "GET"
        try:
            try:
                method, code, headers, body = await asyncio.wait_for(
                    self._serve(reader, peer), READ_TIMEOUT_S
                )
            except TimeoutError:
                code, headers, body = self._json(408, {"ok": False, "error": "timeout", "message": "timeout"})
            except Exception:  # a bad request must not take the listener down
                log.exception("ui request failed")
                code, headers, body = self._json(
                    500, {"ok": False, "error": "internal", "message": "internal error"}
                )
            length = int(headers.pop("Content-Length", len(body)))
            head = [f"HTTP/1.1 {code} {UI_REASONS.get(code, 'Error')}", f"Content-Length: {length}",
                    "Connection: close"]  # fmt: skip
            head += [f"{k}: {v}" for k, v in {**SECURITY, **headers}.items()]
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + (b"" if method == "HEAD" else body))
            await writer.drain()
            if code >= 400 and code != 408:
                with contextlib.suppress(TimeoutError, ConnectionError, OSError):
                    await asyncio.wait_for(reader.read(65536), 0.2)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    @staticmethod
    def _json(code: int, obj, extra: dict | None = None):
        body = json.dumps(obj, separators=(",", ":")).encode()
        return code, {"Content-Type": "application/json", "Cache-Control": "no-store", **(extra or {})}, body

    async def _serve(self, reader, peer: str):
        def err(code, error, message, extra=None):
            return "GET", *self._json(code, {"ok": False, "error": error, "message": message}, extra)

        why = self._peer_refusal(peer)
        if why:
            log.warning("ui: refused %s: %s", peer, why)
            return err(403, "forbidden", why)  # nothing of the request is read
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except asyncio.LimitOverrunError:
            return err(413, "too_large", f"header block over {MAX_HEADER_BYTES} bytes")
        except asyncio.IncompleteReadError:
            return err(400, "bad_request", "incomplete request")
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
            return err(400, "bad_request", "malformed request")
        if not version.startswith("HTTP/1."):
            return err(400, "bad_request", "HTTP/1.x only")
        if method not in ("GET", "HEAD"):
            return err(405, "method_not_allowed", "use GET or HEAD", {"Allow": "GET, HEAD"})
        if not self._limiter.allow():
            return err(429, "rate_limited", "too many requests", {"Retry-After": "1"})
        raw_path, _, query = target.partition("?")
        if len(raw_path) > MAX_PATH_CHARS or not raw_path.startswith("/"):
            return err(400, "bad_request", "bad path")
        segs = self._segments(raw_path)
        if segs is None:
            return err(400, "bad_request", "bad path")
        if segs and segs[0] == "api":
            return method, *await self._api(segs[1:], parse_qs(query))
        return method, *await asyncio.to_thread(self._static, segs, headers)

    # --- static files ---

    @staticmethod
    def _segments(raw_path: str) -> list[str] | None:
        """Decoded path segments; None for anything that could leave the build dir: a dot-segment (`.`, `..`,
        any name starting with a dot), an empty segment, a separator or NUL after decoding."""
        segs = raw_path.split("/")[1:]
        if segs == [""]:
            return []
        out = []
        for s in segs:
            try:
                d = unquote(s, errors="strict") if "%" in s else s
            except UnicodeDecodeError:
                return None
            if not d or d.startswith(".") or "/" in d or "\\" in d or "\0" in d or not d.isprintable():
                return None
            out.append(d)
        return out

    def _static(self, segs: list[str], req_headers: dict):
        def page(code, body, extra=None):
            return code, {"Content-Type": TYPES[".html"], "Cache-Control": "no-store", **(extra or {})}, body

        root = os.path.realpath(self.cfg.dir)
        if not os.path.isfile(os.path.join(root, INDEX)):
            return page(503, NOT_BUILT.encode(), {"Retry-After": "60"})
        found = self._open(root, segs) if segs else None
        if found is None:
            if segs and "." in segs[-1]:  # a file name with an extension that is not there: a real 404
                return page(404, b"<!doctype html><title>Not found</title><h1>404 Not found</h1>")
            found, segs = self._open(root, [INDEX]), [INDEX]  # an app route: the single-page app takes over
        if found is None:
            return page(503, NOT_BUILT.encode(), {"Retry-After": "60"})
        data, st = found
        if data is False:
            return page(413, b"<!doctype html><title>Too large</title><h1>413 File too large</h1>")
        name = segs[-1]
        ext = os.path.splitext(name)[1].lower()
        etag = f'"{st.st_size:x}-{int(st.st_mtime):x}"'
        cache = (
            "public, max-age=31536000, immutable" if HASHED.search(name) and ext != ".html" else "no-cache"
        )
        head = {
            "Content-Type": TYPES.get(ext, "application/octet-stream"),
            "Cache-Control": cache, "ETag": etag,
        }  # fmt: skip
        if req_headers.get("if-none-match") == etag:
            return 304, {**head, "Content-Length": "0"}, b""
        return 200, head, data

    @staticmethod
    def _open(root: str, segs: list[str]):
        """(bytes, stat) of a regular file below `root`, False when it is over the size cap, None when
        there is no such file. Every symlink is refused: the real path must equal the lexical one."""
        path = os.path.join(root, *segs)
        try:
            if os.path.realpath(path) != path:
                return None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # a directory or a FIFO: not a file
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_size > MAX_FILE_BYTES:
                return False, st
            return f.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES], st

    # --- JSON ---

    async def _api(self, parts: list[str], q: dict):
        try:
            if parts == ["config"]:
                return self._json(200, self._config())
            if parts == ["queue"]:
                return self._json(200, self._queue())
            store = RunStore(self.cfg.runs_dir or default_runs_dir())
            code, body = await asyncio.to_thread(runs_api.serve, store, parts, q, "ui")
            return self._json(code, body)
        except Reply as r:  # from _queue
            return self._json(r.code, {"ok": False, "error": r.error, "message": r.message})

    def _config(self) -> dict:
        reply = self.daemon.dispatch({"v": 1, "op": "status"})
        host = (reply.get("status") or {}).get("host") or {}
        return {
            "ok": True, "version": __version__, "host_id": host.get("id"), "mode": host.get("mode"),
            "tempo_url": self.cfg.tempo_url, "api_base": "/api",
            "poll_s": {"queue": 5, "runs": 5, "log": 2},
            "limits": {"runs_max": RUNS_MAX, "log_default_bytes": LOG_DEFAULT, "log_max_bytes": LOG_MAX},
        }  # fmt: skip

    def _queue(self) -> dict:
        reply = self.daemon.dispatch({"v": 1, "op": "status"})
        if not reply.get("ok"):
            raise Reply(503, reply.get("error", "not_ready"), reply.get("message", "daemon not ready"))
        s = reply["status"]
        image = {k: v for k, v in (s.get("image") or {}).items() if k != "env_allow"}
        return {
            "ok": True, "at": s.get("at"), "host": s.get("host"), "level": s.get("level"),
            "slots": s.get("slots"), "light": s.get("light"), "image": image,
        }  # fmt: skip
