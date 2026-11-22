"""Socket ops of the image train: image_submit, image_list, image_cancel, image_now, image_next, image_done.

The daemon only arbitrates: it records jobs, answers which one may start, and notes the result. The client
(`loadctl image runner`) runs the job like `loadctl run --kind image`; nothing is executed here (AGENTS.md
rule 2). Rules and planning: `engines/image.py`; the interface: docs/protocol.md "The image train".
"""

from __future__ import annotations

import re

from loadgov.daemon.principal import caller_info, owner_denial
from loadgov.daemon.slot_ops import _BAD_TEXT, _err, _text
from loadgov.engines.image import CHANNELS, MAX_QUEUE
from loadgov.engines.limits import iso
from loadgov.engines.slots import pid_alive
from loadgov.keys import key_error

ARGV_MAX, ARG_LEN, ENV_LEN = 64, 4096, 4096
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _id(args: dict) -> tuple[int | None, dict | None]:
    v = args.get("id")
    if v is None:
        return None, _err("invalid_args", "id is required")
    if isinstance(v, bool) or not isinstance(v, int):
        return None, _err("bad_request", "id must be an integer")
    return v, None


def _submission(d, args: dict) -> tuple[dict | None, dict | None]:
    """The validated job fields, or an error reply."""
    project, bad = _text(args, "project")
    if bad:
        return None, bad
    label, bad = _text(args, "label")
    if bad:
        return None, bad
    channel = args.get("channel")
    if not isinstance(channel, str):
        return None, _err("bad_request", "channel must be a string")
    if channel not in CHANNELS:
        return None, _err("invalid_args", f"channel must be one of: {', '.join(CHANNELS)}")
    key = args.get("key")
    if key is not None and (why := key_error(key)):
        return None, _err("bad_request" if not isinstance(key, str) else "invalid_args", why)
    argv = args.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None, _err("bad_request", "argv must be a list of strings")
    if not argv or len(argv) > ARGV_MAX or not argv[0] or any(len(a) > ARG_LEN or "\0" in a for a in argv):
        return None, _err("invalid_args", f"argv must be 1-{ARGV_MAX} strings, the first not empty")
    cwd = args.get("cwd")
    if not isinstance(cwd, str):
        return None, _err("bad_request", "cwd must be a string")
    if not cwd.startswith("/") or len(cwd) > 400 or _BAD_TEXT.search(cwd):
        return None, _err("invalid_args", "cwd must be an absolute path")
    env = args.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        return None, _err("bad_request", "env must be an object of strings")
    allow = set(d.config.image.env_allow)  # the daemon filters again: only [image] env_allow is kept
    kept = {
        k: v for k, v in env.items() if k in allow and _NAME.match(k) and len(v) <= ENV_LEN and "\0" not in v
    }
    return {"project": project, "label": label, "channel": channel, "key": key, "argv": argv, "cwd": cwd,
            "env": kept}, None  # fmt: skip


def _request(j) -> dict:
    """What the audit log keeps of a job: never the environment values, only the program name."""
    return {"id": j.id, "project": j.project, "label": j.label, "channel": j.channel, "key": j.key,
            "argv0": j.argv[0], "argc": len(j.argv), "cwd": j.cwd, "env_names": sorted(j.env)}  # fmt: skip


def _audit(
    d, ctx: dict, op: str, principal: str, result: str, reason: str, j=None, project=None, **result_info
) -> None:
    """One audit line; `result_info` (exit code, duration) goes into its `request`."""
    ctx = {k: v for k, v in ctx.items() if k != "request"}
    d.audit.append({"ts": iso(d.clock()), **ctx, "op": op, "principal": principal, "result": result,
                    "reason": reason, "project": j.project if j else project,
                    **({"request": {**_request(j), **result_info}} if j else {})})  # fmt: skip


def _hold(d) -> bool:
    return bool(d.limits.state.hold) or bool(d.limits.effective()["hold_heavy_slot"])


def reap(d) -> None:
    """A running job whose runner process is gone is dropped (called from the daemon's fast tick)."""
    j = d.image.reap(lambda pid: pid_alive(pid, d.slots.stat_reader))
    if j:
        _audit(d, {"source": "image"}, "image_done", "system", "failed",
               f"the runner (pid {j.runner_pid}) is gone; the job was not run again", j)  # fmt: skip
        d._after_change({})


def _runner_denial(d, peer_pid: int | None) -> str | None:
    """The runner is a systemd service or the owner's shell: never a process inside a governed cgroup."""
    return owner_denial(d.fs, peer_pid, d.config.principal)


def rest(d, op: str, args: dict, peer: str) -> dict:
    """`POST /v1/image/now` and `/v1/image/cancel`: the REST caller is the owner (the token was checked)."""
    return handle(d, op, args, None, rest_peer=peer)


def handle(d, op: str, args: dict, peer_pid: int | None, rest_peer: str | None = None) -> dict:
    """`d` is the Daemon (image, config, fs, limits, audit, clock, jobs, _after_change)."""
    now, cfg = d.clock(), d.config.image
    ctx = {**caller_info(d.fs, peer_pid), "op": op, "request": args}
    if rest_peer is not None:
        ctx.update(source="rest", peer=rest_peer)
    reap(d)
    if op == "image_list":
        return {"ok": True, "image": d.image_status(limit=MAX_QUEUE)}
    if "owner" in args and not isinstance(args["owner"], bool):
        return _err("bad_request", "owner must be a boolean")
    if op == "image_submit":
        return _submit(d, args, peer_pid, ctx, now, cfg)
    if op in ("image_cancel", "image_now"):
        job_id, bad = _id(args)
        if bad:
            return bad
        j = d.image.get(job_id)
        if j is None:
            if rest_peer is not None:
                return _err("not_found", f"no image job {job_id}")
            return _err("invalid_args", f"no image job {job_id}")
        return (_cancel if op == "image_cancel" else _now)(d, args, peer_pid, ctx, j, rest_peer)
    if op == "image_next":
        why = _runner_denial(d, peer_pid)
        if why:
            _audit(d, ctx, op, "agent", "refused", why)
            return _err("owner_denied", why)
        hold = _hold(d)
        j, start_by = d.image.claim(cfg, now, hold, peer_pid)
        if j is None:
            return {"ok": True, "job": None, "in_window": d.image_status(limit=0)["in_window"]}
        _audit(d, ctx, "image_start", "system", "accepted",
               f"{j.channel} {j.label} released to the runner (pid {peer_pid})"
               + (" by owner now" if j.now_ok else ""), j)  # fmt: skip
        d._after_change({"source": "image"})
        return {"ok": True, "job": {"id": j.id, "project": j.project, "label": j.label, "channel": j.channel,
                                    "key": j.key, "argv": j.argv, "cwd": j.cwd, "env": j.env,
                                    "start_by": start_by}}  # fmt: skip
    if op == "image_done":
        return _done(d, args, peer_pid, ctx)
    return _err("unknown_op", f"unknown op {op!r}")


def _submit(d, args: dict, peer_pid: int | None, ctx: dict, now: float, cfg) -> dict:
    fields, bad = _submission(d, args)
    if bad:
        return bad
    principal, reason = "agent", None
    if args.get("owner") is True:  # an owner claim waives the beta interval (and is audited)
        why = owner_denial(d.fs, peer_pid, d.config.principal)
        if why:
            _audit(d, ctx, "image_submit", "agent", "refused", why)
            return _err("owner_denied", why)
        reason, bad = _text(args, "reason")
        if bad:
            return bad
        principal = "owner"
    fields["submitter"] = {**{k: ctx[k] for k in ("pid", "cgroup", "cmdline")}, "principal": principal}
    job, refusal = d.image.submit(cfg, now, fields, waive_interval=principal == "owner")
    if refusal:
        error, message, extra = refusal
        _audit(d, ctx, "image_submit", principal, "refused", message, project=fields["project"])
        return {**_err(error, message), **extra}
    _audit(d, ctx, "image_submit", principal, "accepted",
           f"{job.channel} {job.label} queued" + (f" (interval waived: {reason})" if reason else ""),
           job)  # fmt: skip
    d._after_change({"source": "image"})
    row = next((r for r in d.image_status(limit=MAX_QUEUE)["queue"] if r["id"] == job.id), {})
    return {
        "ok": True,
        "id": job.id,
        **{k: row.get(k) for k in ("position", "planned_start", "planned_finish", "state")},
    }


def _running(job_id: int, what: str, rest_peer: str | None) -> dict:
    reply = _err("invalid_args", f"job {job_id} is running; only a queued job can be {what}")
    return {**reply, "refused": True} if rest_peer is not None else reply


def _cancel(d, args: dict, peer_pid: int | None, ctx: dict, j, rest_peer: str | None = None) -> dict:
    """The submitter's project (named in the request: audit-grade evidence, not a boundary) or the owner."""
    owner = args.get("owner") is True or rest_peer is not None
    if rest_peer is not None:
        why = None
    elif owner:
        why = owner_denial(d.fs, peer_pid, d.config.principal)
    else:
        why = (
            None
            if args.get("project") == j.project
            else f"only a client of project {j.project} or an owner may cancel"
        )
    if why:
        _audit(d, ctx, "image_cancel", "agent", "refused", why, j)
        return _err("owner_denied", why)
    if d.image.cancel(j.id) is None:
        return _running(j.id, "cancelled", rest_peer)
    _audit(
        d,
        ctx,
        "image_cancel",
        "owner" if owner else "agent",
        "cancelled",
        f"{j.channel} {j.label} cancelled",
        j,
    )
    d._after_change({"source": "image"})
    return {"ok": True, "id": j.id, "cancelled": True}


def _now(d, args: dict, peer_pid: int | None, ctx: dict, j, rest_peer: str | None = None) -> dict:
    """Owner only: the job may start outside the window and the beta interval. Agents never grant it."""
    why = None if rest_peer is not None else owner_denial(d.fs, peer_pid, d.config.principal)
    if why or (args.get("owner") is not True and rest_peer is None):
        why = why or "owner: true is required for this operation"
        _audit(d, ctx, "image_now", "agent", "refused", why, j)
        return _err("owner_denied", why)
    reason, bad = _text(args, "reason")
    if bad:
        return bad
    if d.image.release_now(j.id, reason) is None:
        return _running(j.id, "released", rest_peer)
    _audit(
        d,
        ctx,
        "image_now",
        "owner",
        "accepted",
        f"{j.channel} {j.label} released outside the window: {reason}",
        j,
    )
    d._after_change({"source": "image"})
    if rest_peer is None:  # REST changes (Home Assistant itself) emit no owner-change event
        d.notify_owner_change({**ctx, "reason": reason}, f"image now {j.id}")
    return {"ok": True, "id": j.id, "now": True}


def _done(d, args: dict, peer_pid: int | None, ctx: dict) -> dict:
    job_id, bad = _id(args)
    if bad:
        return bad
    j = d.image.get(job_id)
    if j is None or j.state != "running":
        return _err("invalid_args", f"image job {job_id} is not running")
    if peer_pid is None or peer_pid != j.runner_pid:
        why = f"only the runner (pid {j.runner_pid}) that started job {job_id} may report it"
        _audit(d, ctx, "image_done", "agent", "refused", why, j)
        return _err("owner_denied", why)
    code, dur, started = args.get("exit_code"), args.get("duration_s"), args.get("started", True)
    if isinstance(code, bool) or not isinstance(code, int):
        return _err("bad_request", "exit_code must be an integer")
    if isinstance(dur, bool) or not isinstance(dur, int | float) or dur < 0:
        return _err("bad_request", "duration_s must be a number >= 0")
    if not isinstance(started, bool):
        return _err("bad_request", "started must be a boolean")
    d.image.finish(j, started)
    if started:
        _audit(d, ctx, "image_done", "system", "ok" if code == 0 else "failed",
               f"{j.channel} {j.label} exited {code} after {dur:.0f} s", j,
               exit_code=code, duration_s=round(dur, 1))  # fmt: skip
    else:
        _audit(d, ctx, "image_done", "system", "deferred",
               f"{j.channel} {j.label} did not start before the window closed; queued again", j)  # fmt: skip
    d._after_change({"source": "image"})
    return {"ok": True, "id": j.id, "requeued": not started}
