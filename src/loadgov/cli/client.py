"""Blocking client for the loadgovd unix socket."""

from __future__ import annotations

import socket
import time

from loadgov.protocol import STARTING, VERSION, decode, encode


class DaemonUnavailable(Exception):
    pass


READY_WAIT_S = 30.0  # a just-started daemon listens before its first sample; wait this long for it
READY_POLL_S = 0.5
NO_SAMPLE_YET = "no sample yet"  # message of an older daemon, which sent `not_ready` instead of `starting`


def is_starting(reply: dict) -> bool:
    """The daemon has no first sample yet: `starting`, or (older daemon) `not_ready` "no sample yet"."""
    return reply.get("error") == STARTING or (
        reply.get("error") == "not_ready" and reply.get("message") == NO_SAMPLE_YET
    )


def request(
    socket_path: str,
    op: str,
    args: dict | None = None,
    timeout: float = 10.0,
    ready_wait: float | None = None,
) -> dict:
    """One request. A `starting` reply (daemon just started) is retried every `READY_POLL_S` for up to
    `ready_wait` (default `READY_WAIT_S`), then raised as DaemonUnavailable (loadctl exit 3, the shims
    fall back). `ready_wait=0` never retries: the caller (`loadctl wait`) keeps its own start-up budget."""
    deadline = time.monotonic() + (READY_WAIT_S if ready_wait is None else ready_wait)
    while True:
        reply = _request_once(socket_path, op, args, timeout)
        if ready_wait == 0 or not is_starting(reply):
            return reply
        if time.monotonic() >= deadline:
            raise DaemonUnavailable(
                f"loadgovd at {socket_path} is not ready (no sample after {READY_WAIT_S:g} s)"
            )
        time.sleep(READY_POLL_S)


def _request_once(socket_path: str, op: str, args: dict | None, timeout: float) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket_path)
            s.sendall(encode({"v": VERSION, "op": op, "args": args or {}}))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except (
        FileNotFoundError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
        PermissionError,
        TimeoutError,
    ) as e:
        raise DaemonUnavailable(f"loadgovd not reachable at {socket_path}: {e}") from e
    if not buf:
        raise DaemonUnavailable(f"loadgovd closed the connection at {socket_path}")
    if not buf.endswith(b"\n"):
        raise DaemonUnavailable(f"loadgovd sent a truncated reply at {socket_path}")
    try:
        return decode(buf)
    except ValueError as e:  # json.JSONDecodeError and non-object replies
        raise DaemonUnavailable(f"loadgovd sent an invalid reply at {socket_path}: {e}") from e
