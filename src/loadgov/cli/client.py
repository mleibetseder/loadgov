"""Blocking client for the loadgovd unix socket."""

from __future__ import annotations

import socket

from loadgov.protocol import VERSION, decode, encode


class DaemonUnavailable(Exception):
    pass


def request(socket_path: str, op: str, args: dict | None = None, timeout: float = 10.0) -> dict:
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
