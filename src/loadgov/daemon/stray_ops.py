"""Stray-process guard runtime: the check on the daemon tick, the events, and the owner kill.

Detection (`engines/strays.py`) is read-only and runs when `[strays] check_s` has passed. The only action is
the owner's `stray_kill` (socket, `loadctl stray --kill`; REST `POST /v1/stray/kill`): it never runs on its
own, and it sends signals only with `[strays] enforce = true` (AGENTS.md rule 2); without it the request is
refused and audited as `WOULD KILL`. The target is re-verified at the moment of the request (it must be in
the current stray list, its start time must match, it must not be `claude`, and the orphan conditions are
evaluated again), the signal goes through a pidfd (`host/signals.py`), and SIGKILL follows on the tick 30 s
after SIGTERM if the process is still alive.
"""

from __future__ import annotations

import logging
import signal

from loadgov.daemon.principal import caller_info, owner_denial
from loadgov.daemon.slot_ops import _err, _text
from loadgov.engines.limits import iso
from loadgov.engines.strays import StrayDetector
from loadgov.host import proctree
from loadgov.host.signals import AGENT_COMM, Signals

log = logging.getLogger("loadgovd")

KILL_AFTER_S = 30.0  # SIGKILL this long after SIGTERM when the process is still alive
ITEM_KEYS = ("pid", "start_time", "comm", "cmdline", "cwd", "cgroup", "project", "cpu_pct", "hot_since",
             "age_s", "orphan", "session")  # fmt: skip


def _dur(s: int) -> str:
    return f"{s // 60} min" if s < 7200 else f"{s / 3600:.1f} h"


def _where(it: dict) -> str:
    return it["project"] or it["cwd"] or it["cgroup"]


class StrayRuntime:
    def __init__(self, d, **options):
        """`options` are test-only: opener, sender, closer (the pidfd layer, as for the memory guard)."""
        self.d, self.cfg = d, d.config.strays
        sig_kw = {k: options[k] for k in ("opener", "sender", "closer") if k in options}
        self.signals = Signals(d.fs, self.cfg.enforce, **sig_kw)
        cwds = {n: gp.agent_cwd for n, gp in d.config.memguard.projects.items() if gp.agent_cwd}
        self.detector = StrayDetector(d.fs, self.cfg, cwds, lambda: [j.cgroup for j in d.jobs.jobs.values()])
        self.items: list[dict] = []
        self.last_check: float | None = None
        self._alerted: dict[tuple[int, int], dict] = {}  # (pid, start) -> the item the phone was told about
        self._term: dict[tuple[int, int], dict] = {}  # SIGTERM sent: {"at", "item", "who"}

    # ---- the tick --------------------------------------------------------------------------------

    def tick(self) -> None:
        now = self.d.clock()
        self._followups(now)
        if not self.cfg.enabled:
            return
        if self.last_check is not None and now - self.last_check < self.cfg.check_s:
            return
        self.last_check = now
        self.items = self.detector.check(now, self.d.sampler.proc_ticks)
        current = {(it["pid"], it["start_time"]): it for it in self.items}
        for key, it in current.items():
            if key not in self._alerted:
                self._alerted[key] = it
                self._alert(it)
        for key in [k for k in self._alerted if k not in current]:
            self._resolve(self._alerted.pop(key), "no longer hot or gone")

    def _alert(self, it: dict) -> None:
        msg = (f"{it['comm']} (pid {it['pid']}) in {_where(it)} has used {it['cpu_pct']:g}% CPU for "
               f"{_dur(it['age_s'])}; its agent session is gone")  # fmt: skip
        self.d.emit_event("stray", {
            "key": f"stray:{it['pid']}", "title": "Stray process", "message": msg,
            "actions": [f"kill_stray:{it['pid']}:{it['start_time']}"],
        })  # fmt: skip
        self._audit("detected", it, "system", {"source": "strays"}, f"STRAY {msg}")

    def _resolve(self, it: dict, why: str) -> None:
        self.d.emit_resolved(f"stray:{it['pid']}", "Stray process")
        self._audit("resolved", it, "system", {"source": "strays"}, f"STRAY resolved: {it['comm']} {why}")

    def _audit(self, result: str, it: dict, principal: str, ctx: dict, reason: str, **extra) -> None:
        self.d.audit.append({
            "ts": iso(self.d.clock()), "op": "stray", "principal": principal, "result": result,
            "reason": reason, "project": it["project"], "pid": ctx.get("pid", it["pid"]),
            "request": {"pid": it["pid"], "start_time": it["start_time"], "comm": it["comm"]},
            **{k: v for k, v in ctx.items() if k != "pid"}, **extra,
        })  # fmt: skip

    def _followups(self, now: float) -> None:
        """SIGKILL what survived SIGTERM for KILL_AFTER_S; say how it ended."""
        for key, t in list(self._term.items()):
            if now - t["at"] < KILL_AFTER_S:
                continue
            del self._term[key]
            it, who = t["item"], t["who"]
            if not self.signals.alive(it["pid"], it["start_time"]):
                self._finish(it, who, "SIGTERM", "exited after SIGTERM", "terminated")
                continue
            res = self.signals.send(it["pid"], it["start_time"], signal.SIGKILL)
            self._finish(it, who, "SIGKILL", f"still running {KILL_AFTER_S:g} s after SIGTERM: {res}",
                         "applied" if res == "sent" else "failed")  # fmt: skip

    def _finish(self, it: dict, who: str, sig: str, result: str, audit_result: str) -> None:
        msg = f"{it['comm']} (pid {it['pid']}, {_where(it)}): {sig}, {result}; asked by {who}"
        self.d.emit_info(f"stray_killed:{it['pid']}", "Stray process killed", msg)
        self._audit(audit_result, it, "system", {"source": "strays"}, f"STRAY {msg}")

    def status(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "enforce": self.cfg.enforce,
            "check_s": self.cfg.check_s,
            "last_check": None if self.last_check is None else iso(self.last_check),
            "items": [{k: it[k] for k in ITEM_KEYS} for it in self.items] if self.cfg.enabled else [],
        }

    # ---- the owner kill --------------------------------------------------------------------------

    def kill(self, args: dict, ctx: dict, principal: str, who: str, reason: str) -> dict:
        d = self.d
        pid, start = args["pid"], args.get("start_time")
        cands = [it for it in self.items if it["pid"] == pid and start in (None, it["start_time"])]
        ctx = {**ctx, "principal": principal, "reason": reason}

        def refuse(why: str, it: dict | None = None, result: str = "refused") -> dict:
            d.audit.append({"ts": iso(d.clock()), **ctx, "project": it and it["project"], "result": result,
                            "reason": why})  # fmt: skip
            return {"ok": False, "refused": True, "reason": why}

        if len(cands) != 1:
            return refuse("not a current stray")
        it = cands[0]
        start = it["start_time"]
        st = self.signals.stat(pid)
        if st is None:
            return refuse("the process is gone", it)
        if st[2] != start:
            return refuse("the process changed", it)
        if st[0] == AGENT_COMM:
            return refuse("the process is the agent (claude): never killed", it)
        if st[0] != it["comm"]:
            return refuse("the process changed", it)
        fresh = self.detector.classify(proctree.table(d.fs), pid, start)
        if fresh is None:
            return refuse("not a current stray: it no longer meets the stray conditions", it)
        if not self.cfg.enforce:
            why = f"WOULD KILL stray {it['comm']} (pid {pid}): [strays] enforce is off"
            d.audit.append({"ts": iso(d.clock()), **ctx, "project": it["project"], "result": "would",
                            "reason": why})  # fmt: skip
            return {"ok": False, "refused": True, "reason": "[strays] enforce is off: nothing was signalled"}
        res = self.signals.send(pid, start, signal.SIGTERM)
        if res != "sent":
            why = {"gone": "the process is gone", "changed": "the process changed",
                   "refused": "the signal guard refused this process"}  # fmt: skip
            return refuse(why.get(res, res), it, "failed")
        key = (pid, start)
        self._term[key] = {"at": d.clock(), "item": it, "who": who}
        self.items = [x for x in self.items if (x["pid"], x["start_time"]) != key]
        self._alerted.pop(key, None)
        msg = f"{it['comm']} (pid {pid}, {_where(it)}): SIGTERM, sent; asked by {who}"
        d.audit.append({"ts": iso(d.clock()), **ctx, "project": it["project"], "result": "applied",
                        "reason": f"KILL stray {msg}: {reason}", "after": f"SIGTERM pid {pid}"})  # fmt: skip
        d.emit_info(f"stray_killed:{pid}", "Stray process killed", msg)
        d.emit_resolved(f"stray:{pid}", "Stray process")
        return {"ok": True, "killed": True, "signal": "SIGTERM", "message":
                f"sent SIGTERM to {it['comm']} (pid {pid}); SIGKILL follows after {KILL_AFTER_S:g} s if it "
                "is still running"}  # fmt: skip


# ---- socket / REST op ----------------------------------------------------------------------------


def handle(d, args: dict, peer_pid: int | None) -> dict:
    return _dispatch(d, args, peer_pid, None)


def rest(d, args: dict, peer: str) -> dict:
    return _dispatch(d, args, None, peer)


def _dispatch(d, args: dict, peer_pid: int | None, rest_peer: str | None) -> dict:
    unknown = sorted(set(args) - {"pid", "start_time", "reason", "owner"})
    if unknown:
        return _err("bad_request", f"unknown argument(s): {', '.join(unknown)}")
    pid, start = args.get("pid"), args.get("start_time")
    if pid is None:
        return _err("invalid_args", "pid is required")
    for name, v in (("pid", pid), ("start_time", start)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            return _err("bad_request", f"{name} must be an integer")
    if pid < 2 or (start is not None and start < 0):
        return _err("invalid_args", "pid must be > 1 and start_time >= 0")
    if "owner" in args and not isinstance(args["owner"], bool):
        return _err("bad_request", "owner must be a boolean")
    reason, bad = _text(args, "reason", required=False)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": "stray", "request": args}
    if rest_peer is not None:
        ctx.update(source="rest", peer=rest_peer)
        who = f"owner via REST from {rest_peer}"
    else:
        why = owner_denial(d.fs, peer_pid, d.config.principal)
        if why or args.get("owner") is not True:
            why = why or "owner: true is required for this operation"
            d.audit.append({"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused",
                            "reason": why})  # fmt: skip
            return _err("owner_denied", why)
        who = f"owner (pid {ctx['pid']}, {ctx['cmdline']})"
    reason = reason or (f"REST stray kill from {rest_peer}" if rest_peer else "loadctl stray --kill")
    return d.strays.kill(args, ctx, "owner", who, reason)
