"""Minimal async client of the loadgovd REST surface (docs/protocol.md)."""

from __future__ import annotations

from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT_S


class LoadgovError(Exception):
    """Base class; `str(err)` is the daemon's message when there is one."""


class LoadgovConnectionError(LoadgovError):
    """The daemon cannot be reached (refused, unreachable, timeout)."""


class LoadgovAuthError(LoadgovError):
    """401 (wrong token) or a 403 that is not the daemon's own source-address refusal."""


class LoadgovSourceError(LoadgovError):
    """403 from the daemon: this source address is not allowed (the token is not the problem)."""


class LoadgovUnavailable(LoadgovError):
    """503: the daemon has no sample yet; retry later."""


class LoadgovRefused(LoadgovError):
    """409: the daemon refused the change (a human restriction is active)."""


class LoadgovApi:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                json=body,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}
                status = resp.status
        except (TimeoutError, aiohttp.ClientError) as err:
            raise LoadgovConnectionError(f"cannot reach loadgovd: {err}") from err
        if not isinstance(data, dict):
            data = {}
        message = str(data.get("reason") or data.get("message") or f"HTTP {status}")
        if status == 403 and data.get("error") == "forbidden":
            raise LoadgovSourceError(message)  # the daemon refuses by source address before it reads a token
        if status in (401, 403):
            raise LoadgovAuthError(message)
        if status == 409:
            raise LoadgovRefused(message)
        if status == 503:
            raise LoadgovUnavailable(message)
        if status >= 400:
            raise LoadgovError(message)
        return data

    async def status(self) -> dict[str, Any]:
        data = await self._request("GET", "/v1/status")
        status = data.get("status")
        if not isinstance(status, dict):
            raise LoadgovError("unexpected status reply")
        return status

    async def post(self, path: str, body: dict | None = None) -> dict[str, Any]:
        return await self._request("POST", path, body or {})
