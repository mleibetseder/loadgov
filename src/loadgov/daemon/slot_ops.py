"""Socket ops of the slot arbiter: slot_acquire, slot_release, slot_status, hold.

All ops are short and never block: a waiting client polls `slot_acquire` (the same call again) until it
is `granted`. The slot is bound to the holder pid in the request, not to the connection.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess

from loadgov.daemon.principal import caller_info, cgroup_of, is_self_or_descendant, owner_denial
from loadgov.engines.limits import iso
from loadgov.engines.slots import OWNER_ORIGIN, SLOT_FILES, parse_stat, pid_alive
from loadgov.sample import _holder_has_lock_open, _kernel_lockers, _lock_identity

_BAD_TEXT = re.compile(r"[\x00-\x1f\x7f]")


def _err(error: str, message: str) -> dict:
    return {"ok": False, "error": error, "message": message}


def _text(args: dict, key: str, required: bool = True) -> tuple[str | None, dict | None]:
    v = args.get(key)
    if v is None:
        return (None, _err("invalid_args", f"{key} is required") if required else None)
    if not isinstance(v, str):
        return None, _err("bad_request", f"{key} must be a string")
    if not v.strip() or len(v) > 200 or _BAD_TEXT.search(v):
        return None, _err("invalid_args", f"{key} must be 1-200 printable characters")
    return v.strip(), None


def _pid(args: dict) -> tuple[int | None, dict | None]:
    v = args.get("pid")
    if v is None:
        return None, _err("invalid_args", "pid is required (the holder process, e.g. the caller's shell)")
    if isinstance(v, bool) or not isinstance(v, int):
        return None, _err("bad_request", "pid must be an integer")
    if v < 2:
        return None, _err("invalid_args", "pid must be > 1")
    return v, None


def _slot(args: dict, allow_all: bool = False) -> tuple[list[str] | None, dict | None]:
    v = args.get("slot", "all" if allow_all else None)
    if v is None:
        return None, _err("invalid_args", "slot is required")
    if not isinstance(v, str):
        return None, _err("bad_request", "slot must be a string")
    if v == "all" and allow_all:
        return list(SLOT_FILES), None
    if v not in SLOT_FILES:
        names = ", ".join([*SLOT_FILES, *(["all"] if allow_all else [])])
        return None, _err("invalid_args", f"slot must be one of: {names}")
    return [v], None


def handle(d, op: str, args: dict, peer_pid: int | None) -> dict:
    """`d` is the Daemon (fs, slots, limits, audit, clock, _after_change)."""
    if op == "slot_status":
        return {"ok": True, "slots": d.slots.summary(), "queues": d.slots.queues()}
    if op == "hold":
        return _hold(d, args, peer_pid)
    if op == "cancel_holder":
        return cancel_holder(d, args, peer_pid)
    names, bad = _slot(args)
    if bad:
        return bad
    pid, bad = _pid(args)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": op, "request": args}
    if op == "slot_release":
        denied = _release_denial(d, names[0], pid, args, peer_pid)
        if denied:
            d.audit.append(
                {"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused", "reason": denied}
            )
            return _err("owner_denied", denied)
        reply = d.slots.release(names[0], pid)
        d._after_change(ctx)
        return reply
    project, bad = _text(args, "project")
    if bad:
        return bad
    label, bad = _text(args, "label")
    if bad:
        return bad
    wait, t = args.get("wait", False), args.get("timeout_s")
    if not isinstance(wait, bool):
        return _err("bad_request", "wait must be a boolean")
    if t is not None and (isinstance(t, bool) or not isinstance(t, int | float)):
        return _err("bad_request", "timeout_s must be a number")
    if t is not None and t <= 0:
        return _err("invalid_args", "timeout_s must be > 0")
    if not isinstance(args.get("client_lock", False), bool):
        return _err("bad_request", "client_lock must be a boolean")
    if d.slots.enforce and not args.get("client_lock"):
        return _err("invalid_args", "enforce mode requires --fd N or loadctl run: the client holds the lock")
    if not pid_alive(pid):
        return _err("invalid_args", f"holder pid {pid} is not running")
    reply = d.slots.acquire(names[0], project, label, pid, wait, t, args.get("client_lock") is True)
    d._after_change({**ctx, "project": project})
    return reply


def _release_denial(d, name: str, pid: int, args: dict, peer_pid: int | None) -> str | None:
    """Only the holder (or a process below it: its shell's `loadctl`, a `loadctl run` job in its scope)
    may release or leave the queue; anyone else needs an owner claim. None means allowed."""
    s = d.slots.slots[name]
    h = s.holder
    if not ((h and h.kind == "job" and h.pid == pid) or any(w.pid == pid for w in s.queue)):
        return None  # nothing to release for this pid: the no-op answer reveals nothing new
    if is_self_or_descendant(d.fs, peer_pid, pid):
        return None
    if (
        h
        and h.pid == pid
        and h.cgroup
        and peer_pid
        and (cgroup_of(d.fs, peer_pid) or "").startswith(h.cgroup)
    ):
        return None  # the job inside the holder's scope
    if "owner" in args and not isinstance(args["owner"], bool):
        return "owner must be a boolean"
    if args.get("owner") is True and owner_denial(d.fs, peer_pid, d.config.principal) is None:
        return None
    return f"only the holder of the {name} slot (pid {pid}) or an owner may release it"


def _hold(d, args: dict, peer_pid: int | None, rest_peer: str | None = None) -> dict:
    state = args.get("state")
    if not isinstance(state, str):
        return _err("bad_request", "state must be 'on' or 'off'")
    if state not in ("on", "off"):
        return _err("invalid_args", "state must be 'on' or 'off'")
    if "owner" in args and not isinstance(args["owner"], bool):
        return _err("bad_request", "owner must be a boolean")
    names, bad = _slot(args, allow_all=True)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": "hold", "request": args}
    if rest_peer is not None:  # REST callers are the owner (the token was checked by the listener)
        ctx.update(source="rest", peer=rest_peer)
    else:
        why = owner_denial(d.fs, peer_pid, d.config.principal)
        if why or args.get("owner") is not True:
            why = why or "owner: true is required for this operation"
            d.audit.append(
                {"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused", "reason": why}
            )
            return _err("owner_denied", why)
    reason, bad = _text(args, "reason", required=state == "on" and rest_peer is None)
    if rest_peer is not None and reason is None and state == "on":
        reason = f"REST hold from {rest_peer}"
    if bad:
        return bad
    d._expire()
    if state == "on":
        d.slots.hold_on(names, reason)
    else:
        d.slots.hold_off(names)
    # The hold flag is a human restriction: agent loosenings are refused while an owner hold exists.
    d.limits.set_flag("hold", d.slots.owner_hold_reason())
    d._after_change({**ctx, "principal": OWNER_ORIGIN, "reason": reason})
    if rest_peer is None:
        d.notify_owner_change({**ctx, "reason": reason}, f"hold {state}")
    return {"ok": True, "hold": state, "slots": d.slots.summary()}


def run_argv(argv: list[str]) -> int:
    """The daemon's one external command: `systemctl kill` of a job scope (injectable as `Daemon.runner`)."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=10).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def _scope_target(d, s, h) -> tuple[dict | None, str | None]:
    """The client (`loadctl run`) is gone but its scope runs: the target is that scope, nothing else."""
    pids = [int(x) for x in (d.slots.scope_reader(h.cgroup) or "").split() if x.isdigit()]
    if not pids:
        return None, f"scope {h.scope} has no processes"
    if h.client_lock or d.slots.enforce:
        ident = _lock_identity(d.fs, s.lock)
        if ident is None or not any(
            p in _kernel_lockers(d.fs, ident) or _holder_has_lock_open(d.fs, p, ident) for p in pids
        ):
            return None, f"no process of scope {h.scope} holds the lock"
    return {"pid": h.job_pid, "start": h.job_start, "source": "loadgov", "holder": h.line(),
            "comm": "scope", "unit": h.scope}, None  # fmt: skip


def _cancel_target(d, name: str) -> tuple[dict | None, str | None]:
    """The process to TERM for `name`: a loadgov job holder or a legacy holder, still the same process."""
    s = d.slots.slots[name]
    h, lg = s.holder, s.legacy
    if h is not None and h.kind == "hold":
        return None, "the slot is held by an owner hold (use hold off)"
    if h is not None and h.scope and h.cgroup and not d.slots._alive(h.pid):
        return _scope_target(d, s, h)
    if h is not None:
        pid, start, source, line = h.pid, h.start, "loadgov", h.line()
    elif lg is not None and lg.get("pid"):
        pid, start, source, line = lg["pid"], None, "legacy", lg["line"]
    else:
        return None, "the slot has no holder process to cancel"
    if pid <= 1 or pid in (os.getpid(), d.slots.self_pid):
        return None, f"refusing to signal pid {pid}"
    stat = d.slots.stat_reader(pid)
    parsed = parse_stat(stat)
    if parsed is None or parsed[0] in ("Z", "X") or (start is not None and parsed[1] != start):
        return None, f"holder pid {pid} is no longer the process that holds the slot"
    if source == "loadgov" and start is None:
        return None, f"holder pid {pid} has no recorded start time; its identity cannot be verified"
    if source == "legacy" or d.slots.enforce or (h is not None and h.client_lock):
        # the holder's pid must still have the lock file open
        ident = _lock_identity(d.fs, s.lock)
        if ident is None or not (
            pid in _kernel_lockers(d.fs, ident) or _holder_has_lock_open(d.fs, pid, ident)
        ):
            return None, f"holder pid {pid} no longer holds the lock"
    comm = stat[stat.find("(") + 1 : stat.rfind(")")]
    if comm == "claude":
        return None, "refusing to signal the agent process (claude)"
    return {"pid": pid, "start": parsed[1], "source": source, "holder": line, "comm": comm}, None


def rest_hold(d, args: dict, rest_peer: str) -> dict:
    """`POST /v1/hold` as the owner: the socket `hold` op without the peer-credential check."""
    return _hold(d, args, None, rest_peer=rest_peer)


def cancel_holder(d, args: dict, peer_pid: int | None, rest_peer: str | None = None) -> dict:
    """Owner only: SIGTERM the current holder of `slot` after `confirm: true`. With `[slots] enforce`
    off nothing is signalled and the audit line says what would have been."""
    unknown = sorted(set(args) - {"slot", "confirm", "reason", "owner"})
    if unknown:
        return _err("bad_request", f"unknown argument(s): {', '.join(unknown)}")
    if "owner" in args and not isinstance(args["owner"], bool):
        return _err("bad_request", "owner must be a boolean")
    names, bad = _slot(args)
    if bad:
        return bad
    reason, bad = _text(args, "reason", required=False)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": "cancel_holder", "request": args}
    if rest_peer is not None:
        ctx.update(source="rest", peer=rest_peer)
        reason = reason or f"REST cancel-holder from {rest_peer}"
    else:
        why = owner_denial(d.fs, peer_pid, d.config.principal)
        if why or args.get("owner") is not True:
            why = why or "owner: true is required for this operation"
            d.audit.append(
                {"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused", "reason": why}
            )
            return _err("owner_denied", why)
        reason = reason or "cancel-holder by owner"
    if args.get("confirm") is not True:
        return _err("bad_request", "confirm: true is required")
    ctx.update(principal=OWNER_ORIGIN, reason=reason, project=None)
    if d.slots.tick():  # a holder that exited since the last tick is gone, not a target
        d._after_change({})
    target, why = _cancel_target(d, names[0])
    if target is None:
        d.audit.append({"ts": iso(d.clock()), **ctx, "result": "refused", "reason": f"{reason}; {why}"})
        return _err("invalid_args", why)
    reply = {"ok": True, "slot": names[0], "signalled": False, "signal": "SIGTERM", **target}
    result = "would"
    if d.slots.enforce and target.get("unit"):
        # only that unit: loadctl run is gone, so the signal goes to the scope it started
        rc = d.runner(["systemctl", "kill", "--signal=SIGTERM", target["unit"]])
        if rc != 0:
            return _err("invalid_args", f"systemctl kill {target['unit']} failed (exit {rc})")
        reply["signalled"], result = True, "signalled"
    elif d.slots.enforce:
        try:
            fd = os.pidfd_open(target["pid"])  # the fd pins the process: no pid reuse after the check
        except OSError as e:
            return _err("invalid_args", f"holder pid {target['pid']} is gone ({e.strerror})")
        try:
            st = parse_stat(d.slots.stat_reader(target["pid"]))
            if st is None or st[1] != target["start"]:
                return _err("invalid_args", f"holder pid {target['pid']} changed before the signal")
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            reply["signalled"], result = True, "signalled"
        except OSError as e:
            return _err("invalid_args", f"cannot signal pid {target['pid']} ({e.strerror})")
        finally:
            os.close(fd)
    d.audit.append(
        {"ts": iso(d.clock()), **ctx, "result": result, "before": target["holder"],
         "after": f"SIGTERM {target['unit']}" if target.get("unit") else f"SIGTERM pid {target['pid']}"}
    )  # fmt: skip
    if rest_peer is None:
        d.notify_owner_change(ctx, f"cancel_holder {names[0]}")
    return reply
