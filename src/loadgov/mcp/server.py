"""loadgov-mcp: stdio MCP server for agents.

Every tool talks to loadgovd over the unix socket as the *agent* principal. The server never sends
`owner: true` and has no owner tool, so owner actions (ceiling, reset, hold, `--owner`) are impossible
through it. The tool logic is plain Python (class `Tools`); the `mcp` SDK (optional extra) is imported
only by `build_server` / `main`.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import os
import sys
import time
from typing import Any

from loadgov.cli.client import DaemonUnavailable, request
from loadgov.cli.loadctl import wait_headroom
from loadgov.config import KINDS
from loadgov.protocol import DEFAULT_SOCKET

MAX_WAIT_S = 1800.0
OPTIONAL_TOOLS = {"load_reap_idle_daemons": "reap", "load_freeze": "freeze", "load_unfreeze": "freeze"}
# top-level status sections copied as they are when present
OPTIONAL_STATUS_KEYS = ("memguard",)


class ToolFailure(Exception):
    """A clear tool error (the MCP layer reports the message, never a trace)."""


def _tagged(reason: str, agent_id: str | None) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ToolFailure("reason is required")
    return f"[agent {agent_id}] {reason}" if agent_id else reason


def _who(info: dict | None, none: str) -> str:
    if not info:
        return none
    return (
        f"{info.get('principal')} ({info.get('reason')}), since {info.get('set_at')}, "
        f"until {info.get('expires_at') or 'no expiry'}"
    )


class Tools:
    def __init__(self, socket_path: str | None = None, sleep=time.sleep):
        self.socket = socket_path or os.environ.get("LOADGOV_SOCKET", DEFAULT_SOCKET)
        self.sleep = sleep

    # -- daemon access ----------------------------------------------------------------------------
    def call(self, op: str, args: dict | None = None) -> dict:
        """One request as the agent principal. Never sends `owner`."""
        args = {k: v for k, v in (args or {}).items() if v is not None and k != "owner"}
        try:
            return request(self.socket, op, args)
        except DaemonUnavailable as e:
            raise ToolFailure(f"loadgovd not reachable: {e}") from e

    def _ok(self, op: str, args: dict | None = None) -> dict:
        r = self.call(op, args)
        if not r.get("ok"):
            raise ToolFailure(f"loadgovd {op}: {r.get('error')}: {r.get('message')}")
        return r

    def capabilities(self) -> list[str]:
        try:
            return list(self._ok("status")["status"].get("capabilities") or [])
        except ToolFailure:
            return []

    # -- tools ------------------------------------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        s = self._ok("status")["status"]
        lim = s.get("limits", {})
        out: dict[str, Any] = {
            "at": s.get("at"),
            "level": s.get("level"),
            "levels": s.get("levels"),
            "cpu": s.get("cpu"),
            "memory": s.get("memory"),
            "swap": s.get("swap"),
            "io": s.get("io"),
            "disk": s.get("disk"),
            "limits": {
                "profile": lim.get("profile"),
                "effective": lim.get("effective"),
                "ceiling": lim.get("ceiling"),
                "ceiling_info": lim.get("ceiling_info"),
                "profile_info": lim.get("profile_info"),
                "restricted": lim.get("restricted"),
                "restricted_by": lim.get("restricted_by"),
                "freeze": lim.get("freeze"),
            },
            "slots": s.get("slots"),
            "jobs": s.get("jobs"),
            "capabilities": s.get("capabilities") or [],
        }
        out.update({k: s[k] for k in OPTIONAL_STATUS_KEYS if k in s})
        return out

    def set_limits(
        self,
        reason: str,
        profile: str | None = None,
        max_cores: float | None = None,
        target_cpu_pct: float | None = None,
        load_max: float | None = None,
        max_implementers: int | None = None,
        ram_floor_gb: float | None = None,
        ttl: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        project: str | None = None,
    ) -> dict[str, Any]:
        vals = {
            "max_cores": max_cores,
            "target_cpu_pct": target_cpu_pct,
            "load_max": load_max,
            "max_implementers": max_implementers,
            "ram_floor_gb": ram_floor_gb,
        }
        if profile is None and all(v is None for v in vals.values()):
            raise ToolFailure("give a profile or at least one limit")
        args = {
            "profile": profile,
            **vals,
            "ttl": ttl,
            "until": until,
            "reason": _tagged(reason, agent_id),
            "project": project,
        }
        r = self.call("set", args)
        if r.get("refused") or r.get("ok"):
            return r  # a refusal is a result the agent must report, not an error
        raise ToolFailure(f"loadgovd set: {r.get('error')}: {r.get('message')}")

    def wait_headroom(self, kind: str, timeout_sec: float, project: str | None = None) -> dict[str, Any]:
        if kind not in KINDS:
            raise ToolFailure(f"kind must be one of {', '.join(KINDS)}")
        if not isinstance(timeout_sec, int | float) or not math.isfinite(timeout_sec) or timeout_sec < 1:
            raise ToolFailure("timeout_sec must be a finite number of at least 1")
        a = argparse.Namespace(
            socket=self.socket,
            kind=kind,
            project=project,
            label="mcp",
            timeout=min(float(timeout_sec), MAX_WAIT_S),
            confirm=None,
            poll=None,
            **dict.fromkeys(
                ("load1_max", "load5_max", "psi_max", "mem_min_gb", "swap_max_mb", "mem_psi_max")
            ),
        )
        last: dict = {}
        out, err = io.StringIO(), io.StringIO()
        rc = wait_headroom(a, self.sleep, out, last, err)
        if rc == 3:
            raise ToolFailure(f"loadgovd not reachable at {self.socket}")
        if rc not in (0, 1):
            raise ToolFailure(err.getvalue().strip() or "loadgovd returned an error")
        d = last.get("reply", {}).get("decision", {})
        return {
            "result": "ok" if rc == 0 else "timeout",
            "kind": kind,
            "reasons": d.get("reasons", []),
            "line": (out.getvalue() or err.getvalue()).strip().splitlines()[-1:],
            "capped_at_sec": MAX_WAIT_S if timeout_sec > MAX_WAIT_S else None,
            "hint": None if rc == 0 else "not started: call again, or use `loadctl wait` for a longer wait",
        }

    def explain(self) -> dict[str, Any]:
        full = self._ok("status")["status"]
        s = full.get("limits", {})
        lim = self._ok("limits")["limits"]
        paused = (full.get("memguard") or {}).get("paused") or []
        freeze = lim.get("freeze") or s.get("freeze")
        overrides = s.get("overrides") or lim.get("overrides") or []
        r = lim.get("restriction") or {}
        restricted = bool(s.get("restricted") or r.get("active"))
        why = [f"profile {s.get('profile')}: " + _who(s.get("profile_info"), "default or restored")]
        if s.get("ceiling_info"):
            why.append(f"ceiling {s['ceiling']}: " + _who(s["ceiling_info"], ""))
        why += [
            f"{o.get('principal')} set {o.get('param')}={o.get('value')} "
            f"until {o.get('expires_at') or 'cleared'}: {o.get('reason')}"
            for o in overrides
        ]
        if restricted:
            why.append(f"human restriction ({r.get('kind') or s.get('restricted_by')}): {r.get('reason')}")
        if freeze:
            why.append(f"workers frozen: {freeze}")
        if paused:
            why.append(f"memory guard paused {len(paused)} worker(s)")
        return {
            "profile": s.get("profile"),
            "profile_info": s.get("profile_info"),
            "ceiling": s.get("ceiling"),
            "ceiling_info": s.get("ceiling_info"),
            "overrides": overrides,
            "restricted": restricted,
            "restricted_by": s.get("restricted_by"),
            "restriction": r,
            "hold": lim.get("hold"),
            "freeze": freeze,
            "memguard_paused": paused,
            "effective": s.get("effective"),
            "why": why,
        }

    def _optional(self, op: str, reason: str, agent_id: str | None, **extra) -> dict:
        # verbatim, including a refusal or owner_denied: never fake success
        return self.call(op, {"reason": _tagged(reason or f"{op} by agent", agent_id), **extra})

    def reap_idle_daemons(self, reason: str = "", agent_id: str | None = None):
        return self._optional("reap", reason, agent_id)

    def freeze(
        self,
        reason: str = "",
        agent_id: str | None = None,
        project: str | None = None,
        ttl: str | None = None,
    ):
        return self._optional("freeze", reason, agent_id, project=project, ttl=ttl)

    def unfreeze(self, reason: str = "", agent_id: str | None = None, project: str | None = None):
        return self._optional("unfreeze", reason, agent_id, project=project)


DOC_WAIT = (
    "Block until the host has headroom for one more heavy job (two passing samples). Returns "
    "result=ok or result=timeout plus the last decision reasons. One call waits at most 1800 s: call "
    "again, or use `loadctl wait` for longer waits. Poll sleeps never pass the timeout; "
    "a started confirmation completes, so a call may overrun by up to confirm_s."
)


def build_server(tools: Tools | None = None):
    """The FastMCP server. Optional tools are (un)registered from the daemon's `capabilities` at start
    and again on every tools/list."""
    from mcp.server.fastmcp import FastMCP

    t = tools or Tools()

    class Server(FastMCP):
        async def list_tools(self):
            caps = await asyncio.to_thread(t.capabilities)
            for name, cap in OPTIONAL_TOOLS.items():
                have = name in self._tool_manager._tools
                if cap in caps and not have:
                    self.add_tool(optional[name], name=name, description=optional[name].__doc__)
                elif cap not in caps and have:
                    self.remove_tool(name)
            return await super().list_tools()

    srv = Server("loadgov", instructions="Host load governor. Acts as the agent principal only.")

    @srv.tool()
    async def load_get_status() -> dict:
        """Compact snapshot: cpu/psi/memory/swap/io, levels, effective limits, profile, ceiling,
        restriction, slots, jobs, capabilities."""
        return await asyncio.to_thread(t.get_status)

    @srv.tool()
    async def load_set_limits(
        reason: str,
        profile: str | None = None,
        max_cores: float | None = None,
        target_cpu_pct: float | None = None,
        load_max: float | None = None,
        max_implementers: int | None = None,
        ram_floor_gb: float | None = None,
        ttl: str | None = None,
        until: str | None = None,
        agent_id: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Change the profile or single limits as an agent. Lowering is always accepted (default TTL
        2 h, max 12 h). Raising is refused (`refused: true`, `restrictedBy`, the owner's reason) while a
        human restriction is active: report that to the user and do not retry. `reason` is required."""
        return await asyncio.to_thread(
            t.set_limits,
            reason,
            profile,
            max_cores,
            target_cpu_pct,
            load_max,
            max_implementers,
            ram_floor_gb,
            ttl,
            until,
            agent_id,
            project,
        )

    @srv.tool(description=DOC_WAIT)
    async def load_wait_headroom(kind: str, timeout_sec: float, project: str | None = None) -> dict:
        return await asyncio.to_thread(t.wait_headroom, kind, timeout_sec, project)

    @srv.tool()
    async def load_explain() -> dict:
        """Why the limits are what they are: profile, ceiling, overrides (who, why, expiry) and
        restriction flags. Use it to tell the user who restricted a limit and why."""
        return await asyncio.to_thread(t.explain)

    async def load_reap_idle_daemons(reason: str = "", agent_id: str | None = None) -> dict:
        """Reap idle build daemons (the daemon's reply is returned verbatim)."""
        return await asyncio.to_thread(t.reap_idle_daemons, reason, agent_id)

    async def load_freeze(
        reason: str = "", agent_id: str | None = None, project: str | None = None, ttl: str | None = None
    ) -> dict:
        """Freeze workers. The daemon allows this to the owner only, so expect owner_denied; its reply
        is returned verbatim. Never freeze other agents' work."""
        return await asyncio.to_thread(t.freeze, reason, agent_id, project, ttl)

    async def load_unfreeze(
        reason: str = "", agent_id: str | None = None, project: str | None = None
    ) -> dict:
        """Unfreeze workers (the daemon's reply is returned verbatim)."""
        return await asyncio.to_thread(t.unfreeze, reason, agent_id, project)

    optional = {f.__name__: f for f in (load_reap_idle_daemons, load_freeze, load_unfreeze)}
    caps = t.capabilities()
    for name, cap in OPTIONAL_TOOLS.items():
        if cap in caps:
            srv.add_tool(optional[name], name=name, description=optional[name].__doc__)
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loadgov-mcp", description="loadgov MCP server (stdio).")
    ap.add_argument("--socket", default=os.environ.get("LOADGOV_SOCKET", DEFAULT_SOCKET))
    a = ap.parse_args(argv)
    try:
        server = build_server(Tools(a.socket))
    except ImportError:
        print("loadgov-mcp: the MCP SDK is missing: pip install 'loadgov[mcp]'", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
