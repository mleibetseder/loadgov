"""Push alert events to a Home Assistant webhook: a bounded queue and one background task.

Off unless `[ha] enabled = true`. The webhook id is a secret: it is read from tokens.env
(`LOADGOV_HA_WEBHOOK_ID`) on every delivery and never goes into the config, the status, the audit log
or a log line. `enqueue` only appends to a deque, so the tick never waits for the network; the POST runs
in a worker thread behind `await`, and a failed delivery is retried with backoff before it is dropped.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import re
import time
from collections import deque
from urllib.parse import urlsplit

from loadgov.config import HaConfig
from loadgov.daemon.rest import DEFAULT_TOKENS_FILE, TokenError, read_env_value
from loadgov.engines.limits import iso

log = logging.getLogger("loadgovd.ha")

WEBHOOK_VAR = "LOADGOV_HA_WEBHOOK_ID"
QUEUE_MAX = 64
BACKOFF_S = (1.0, 4.0, 15.0)  # three retries after the first attempt
TIMEOUT_S = 5.0
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class DeliveryError(Exception):
    pass


def load_webhook_id(path: str) -> str:
    value = read_env_value(path, WEBHOOK_VAR)
    if value is None:
        raise TokenError(f"{WEBHOOK_VAR} is not set in {path}")
    if not _ID_RE.match(value):
        raise TokenError(f"{WEBHOOK_VAR} must be 1-128 characters of A-Z a-z 0-9 _ -")
    return value


class HaPush:
    def __init__(self, cfg: HaConfig, tokens_file: str = DEFAULT_TOKENS_FILE, *, clock=time.time,
                 sleep=asyncio.sleep, backoff: tuple[float, ...] = BACKOFF_S,
                 timeout: float = TIMEOUT_S, queue_max: int = QUEUE_MAX):  # fmt: skip
        self.cfg, self.tokens_file, self.clock, self._sleep = cfg, tokens_file, clock, sleep
        self.backoff, self.timeout, self.queue_max = backoff, timeout, queue_max
        self._q: deque[dict] = deque()
        self._wake: asyncio.Event | None = None
        self.dropped = 0
        self.failed = 0
        self.state = "up" if cfg.enabled else "disabled"
        self.last_ok: float | None = None
        self.last_error: str | None = None
        u = urlsplit(cfg.url)
        self._https, self._host, self._port = u.scheme == "https", u.hostname, u.port
        self._prefix = u.path.rstrip("/")

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "state": self.state,
            "queued": len(self._q),
            "dropped": self.dropped,
            "failed": self.failed,
            "last_ok": iso(self.last_ok) if self.last_ok else None,
            "last_error": self.last_error,
        }

    def enqueue(self, event: dict) -> None:
        """Never blocks and never raises; when full the oldest event is dropped and counted."""
        if not self.cfg.enabled:
            return
        if len(self._q) >= self.queue_max:
            self._q.popleft()
            self.dropped += 1
        self._q.append(event)
        if self._wake is not None:
            self._wake.set()

    async def run(self) -> None:
        try:
            await self._loop()
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # never exit silently: the status must say the push is not running
            self.state, self.last_error = "down", f"push task stopped: {type(e).__name__}"
            log.error("ha push task stopped: %s", type(e).__name__)
            raise

    async def _loop(self) -> None:
        self._wake = asyncio.Event()
        while True:
            if not self._q:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._deliver(self._q.popleft())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # one bad event (e.g. not JSON-encodable) must not stop the push
                self.failed += 1
                self.state, self.last_error = "down", f"{type(e).__name__} while sending an event"
                log.warning("ha push: %s", self.last_error)

    async def _deliver(self, event: dict) -> None:
        body = json.dumps({"v": 1, **event}, separators=(",", ":")).encode()
        error = "unknown"
        for attempt in range(len(self.backoff) + 1):
            try:
                wid = load_webhook_id(self.tokens_file)
            except TokenError as e:  # a config problem: retrying in a few seconds will not fix it
                error = str(e)
                break
            try:
                await asyncio.to_thread(self._post, wid, body)
            except (OSError, http.client.HTTPException, DeliveryError) as e:
                error = self._describe(e, wid)
            else:
                self.state, self.last_ok, self.last_error = "up", self.clock(), None
                return
            if attempt < len(self.backoff):
                await self._sleep(self.backoff[attempt])
        self.state, self.last_error = "down", error
        self.failed += 1
        log.warning("ha push failed: %s", error)

    @staticmethod
    def _describe(e: Exception, wid: str) -> str:
        """The error class and OS reason only; never str(e), which may echo the URL (and the id)."""
        if isinstance(e, DeliveryError):
            return str(e)
        reason = getattr(e, "strerror", None) or ""
        return f"{type(e).__name__}{': ' + reason if reason else ''}".replace(wid, "***")

    def _post(self, wid: str, body: bytes) -> None:
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=self.timeout)  # no proxy, no redirects
        try:
            conn.request("POST", f"{self._prefix}/api/webhook/{wid}", body,
                         {"Content-Type": "application/json", "User-Agent": "loadgovd"})  # fmt: skip
            resp = conn.getresponse()
            resp.read(1024)
            if not 200 <= resp.status < 300:
                raise DeliveryError(f"HTTP {resp.status}")
        finally:
            conn.close()
