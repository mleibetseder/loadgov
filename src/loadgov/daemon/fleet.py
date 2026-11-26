"""The fleet view: this host from its own status, every other host of `[fleet]` from its REST `/v1/status`.

Read-only. A background thread polls each remote host (one GET, a few seconds' timeout) and keeps the last
result per host; the tick never waits for the network and `fleet_view` only reads that memory. Nothing here
decides, writes or sends anything to another host: each `loadgovd` decides only for its own host (DEC-1).
A host's bearer token is read from `tokens.env` by the variable name `token_env` on every poll and is never
stored, logged or put into an error text.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import math
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from loadgov.config import FLEET_MAX, ConfigError, FleetHost, check_host_id
from loadgov.daemon.rest import DEFAULT_TOKENS_FILE, TokenError, read_env_value
from loadgov.engines.limits import iso
from loadgov.protocol import LEVEL

log = logging.getLogger("loadgovd.fleet")

INTERVAL_S = 15.0
TIMEOUT_S = 3.0
MAX_REPLY_BYTES = 1024 * 1024
VERSION = 1


class FetchError(Exception):
    """A short, token-free reason a host could not be read."""


def _fetch(url: str, token: str, timeout: float, box: dict) -> dict:
    u = urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = box["conn"] = cls(u.hostname, u.port, timeout=timeout)
    try:
        conn.request(
            "GET", "/v1/status", headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )
        resp = conn.getresponse()
        body = resp.read(MAX_REPLY_BYTES + 1)
    except TimeoutError:
        raise FetchError("timeout") from None
    except ConnectionRefusedError:
        raise FetchError("connection refused") from None
    except (OSError, http.client.HTTPException):
        raise FetchError("unreachable") from None
    finally:
        conn.close()
    if resp.status in (401, 403):
        raise FetchError(f"HTTP {resp.status}: token or source address refused")
    if resp.status != 200:
        raise FetchError(f"HTTP {resp.status}")
    if len(body) > MAX_REPLY_BYTES:
        raise FetchError("reply too large")
    try:
        status = json.loads(body)["status"]
    except (ValueError, KeyError, TypeError, RecursionError):
        raise FetchError("not a loadgov status reply") from None
    if not isinstance(status, dict):
        raise FetchError("not a loadgov status reply")
    return status


_inflight: dict[str, threading.Thread] = {}  # url -> its helper thread while it is alive
_inflight_lock = threading.Lock()


def fetch_status(url: str, token: str, timeout: float = TIMEOUT_S) -> dict:
    """GET `<url>/v1/status` with the bearer token; the status object, or FetchError. `timeout` bounds the
    whole request (name lookup, connect, TLS, read), not each step: the work runs in a helper thread that
    is abandoned at the deadline (its socket is shut down, which frees a read or a TLS handshake). A name
    lookup cannot be interrupted, so its thread lives until the resolver gives up: therefore at most one
    helper per url is ever alive (and at most FLEET_MAX in all). While one is, a new fetch of that url fails
    at once with "previous fetch still running" and starts no thread."""
    box: dict = {}

    def work():
        try:
            box["out"] = _fetch(url, token, timeout, box)
        except FetchError as e:
            box["err"] = e
        except Exception as e:  # never a traceback or exception text in a log or an error: no token in them
            log.debug("fleet fetch failed: %s", type(e).__name__)
            box["err"] = FetchError("unreachable")

    with _inflight_lock:
        for k in [k for k, th in _inflight.items() if not th.is_alive()]:
            del _inflight[k]
        if url in _inflight or len(_inflight) >= FLEET_MAX:
            raise FetchError("previous fetch still running")
        t = _inflight[url] = threading.Thread(target=work, name="loadgov-fleet-fetch", daemon=True)
        t.start()
    t.join(timeout)
    if t.is_alive():
        conn = box.get("conn")
        with contextlib.suppress(Exception):
            conn.sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read
        raise FetchError("timeout")
    if "err" in box:
        raise box["err"]
    return box["out"]


# ---- remote values are untrusted: every field is type-checked and bounded, else null ----------------------

STR_MAX = 120
NUM_MAX = 1e12
LIST_MAX = 100
SLOT_STATES = ("free", "held", "held-by-hold")
_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _get(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _num(v):
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and abs(v) <= NUM_MAX
    return v if ok else None


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and abs(v) <= NUM_MAX else None


def _text(v, limit: int = STR_MAX):
    return v if isinstance(v, str) and len(v) <= limit and v.isprintable() else None


def _one_of(v, allowed):
    return v if isinstance(v, str) and v in allowed else None


def _slot(status: dict, name: str) -> dict:
    sl = _get(status, "slots", name)
    return {
        "state": _one_of(_get(sl, "state"), SLOT_STATES),
        "holder": _text(_get(sl, "holder")),
        "queue": _int(_get(sl, "queue")),
    }


def summarize(status: dict) -> dict:
    """The fleet fields of one host out of its status; a missing, mistyped or oversized field is None."""
    queue = _get(status, "image", "queue")
    nxt = None
    if isinstance(queue, list):
        for q in queue[:LIST_MAX]:
            ts = q.get("planned_start") if isinstance(q, dict) else None
            if isinstance(ts, str) and _TS_RE.match(ts):
                nxt = ts
                break
    return {
        "level": _one_of(_get(status, "level"), LEVEL),
        "cores": _int(_get(status, "host", "cores")),
        "cpu": {
            "busy_pct": _num(_get(status, "cpu", "busy_pct")),
            "load1_per_core": _num(_get(status, "cpu", "load1_per_core")),
        },
        "memory": {
            "avail_mb": _int(_get(status, "memory", "avail_mb")),
            "total_mb": _int(_get(status, "memory", "total_mb")),
        },
        "disk": {
            "used_pct": _num(_get(status, "disk", "used_pct")),
            "free_gb": _num(_get(status, "disk", "free_gb")),
        },
        "light": {
            "running": _int(_get(status, "light", "running")),
            "max": _int(_get(status, "light", "max")),
        },
        "heavy": _slot(status, "heavy"),
        "compile": _slot(status, "compile"),
        "image": {"queued": _int(_get(status, "image", "queued")), "next_start": nxt},
    }


EMPTY = summarize({})


def _age(now: float, fetched_at: float | None) -> int | None:
    return None if fetched_at is None else max(0, int(now - fetched_at))


def build_view(local_id: str, local_status: dict | None, hosts: tuple[FleetHost, ...], results: dict,
               now: float) -> dict:  # fmt: skip
    """The fleet JSON: this host first (from its own status, never a REST call), then the registry hosts
    in file order (a registry entry with the local id is skipped)."""
    local = {
        "id": local_id, "local": True,
        "mode": _get(local_status, "host", "mode") or "govern",
        "reachable": local_status is not None, "fetched_at": iso(now), "age_s": 0 if local_status else None,
        "error": None if local_status is not None else "starting: no sample yet",
        "id_mismatch": False, "reported_id": local_id,
        **(summarize(local_status) if local_status is not None else EMPTY),
    }  # fmt: skip
    rows = [local]
    for h in hosts:
        if h.id == local_id:
            continue
        r = results.get(h.id) or {}
        rows.append({
            "id": h.id, "local": False, "mode": h.mode,
            "reachable": bool(r.get("reachable")), "fetched_at": r.get("fetched_at"),
            "age_s": _age(now, r.get("fetched_ts")),
            "error": r.get("error", "not polled yet"),
            "id_mismatch": bool(r.get("bad_id")) or bool(r.get("reported_id") not in (None, h.id)),
            "reported_id": r.get("reported_id"),
            **(r.get("summary") or EMPTY),
        })  # fmt: skip
    return {"v": VERSION, "at": iso(now), "host": local_id, "hosts": rows}


class FleetCollector:
    """Polls the registry hosts in a daemon thread; `poll_once` is the unit of work (and what tests call)."""

    def __init__(self, hosts, local_id, tokens_file: str = DEFAULT_TOKENS_FILE, *, clock=time.time,
                 fetch=fetch_status, interval: float = INTERVAL_S, timeout: float = TIMEOUT_S):  # fmt: skip
        self.hosts, self.local_id, self.tokens_file = (
            hosts,
            local_id,
            tokens_file,
        )  # hosts, local_id: callables
        self.clock, self.fetch, self.interval, self.timeout = clock, fetch, interval, timeout
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None

    def results(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._results.items()}

    def _token(self, h: FleetHost) -> str:
        try:
            value = read_env_value(self.tokens_file, h.token_env)
        except TokenError as e:
            raise FetchError(str(e)) from None
        if not value:
            raise FetchError(f"{h.token_env} is not set in the tokens file")
        return value

    def poll_host(self, h: FleetHost) -> None:
        token, now = "", self.clock()
        try:
            token = self._token(h)
            status = self.fetch(h.url, token, self.timeout)
            summary = summarize(status)
            reported, bad_id = _get(status, "host", "id"), False
            try:
                reported = check_host_id(reported) if reported is not None else None
            except ConfigError:
                reported, bad_id = None, True
            entry = {"reachable": True, "fetched_ts": now, "fetched_at": iso(now), "summary": summary,
                     "error": "remote reported an invalid host id" if bad_id else None,
                     "reported_id": reported, "bad_id": bad_id}  # fmt: skip
        except FetchError as e:
            error = str(e).replace(token, "***") if token else str(e)
            with self._lock:
                prev = self._results.get(h.id) or {}
                entry = {**prev, "reachable": False, "error": error[:200]}  # the last good data stays, aged
        except Exception:  # a bug here must never stop the poller
            log.exception("fleet poll of %s failed", h.id)
            with self._lock:
                entry = {**(self._results.get(h.id) or {}), "reachable": False, "error": "internal error"}
        with self._lock:
            was = (self._results.get(h.id) or {}).get("reachable")
            self._results[h.id] = entry
        if was != entry["reachable"]:
            log.info("fleet host %s %s%s", h.id, "reachable" if entry["reachable"] else "unreachable",
                     f": {entry['error']}" if entry["error"] else "")  # fmt: skip

    def poll_once(self) -> None:
        hosts = [h for h in self.hosts() if h.id != self.local_id()]
        for h in hosts:
            self.poll_host(h)
        with self._lock:  # a host removed from the registry (reload) leaves the view
            for gone in set(self._results) - {h.id for h in hosts}:
                del self._results[gone]

    def poke(self) -> None:
        """Poll soon (after a reload changed the registry)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("fleet poll failed")
            self._wake.wait(self.interval)
            self._wake.clear()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="loadgov-fleet", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
