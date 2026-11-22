"""Socket ops of `loadctl run`: job_start and job_end (see docs/protocol.md)."""

from __future__ import annotations

import re

from loadgov.config import KINDS
from loadgov.daemon.principal import caller_info
from loadgov.daemon.slot_ops import _err, _text
from loadgov.engines.jobs import Job
from loadgov.engines.limits import iso
from loadgov.engines.slots import SLOT_FILES, pid_alive
from loadgov.keys import key_error

OVERRUNS = ("runtime", "memory")  # what `loadctl run --kind light` reports with job_end
_UNIT = re.compile(r"^loadgov-[A-Za-z0-9:_.-]+\.scope$")
_CGROUP = re.compile(r"^/[A-Za-z0-9:_.@/-]+$")


def _int(args: dict, key: str, required: bool = True) -> tuple[int | None, dict | None]:
    v = args.get(key)
    if v is None:
        return (None, _err("invalid_args", f"{key} is required") if required else None)
    if isinstance(v, bool) or not isinstance(v, int):
        return None, _err("bad_request", f"{key} must be an integer")
    if v < 2:
        return None, _err("invalid_args", f"{key} must be > 1")
    return v, None


def _unit(args: dict) -> tuple[str | None, dict | None]:
    unit, bad = _text(args, "unit")
    if bad:
        return None, bad
    if len(unit) > 200 or not _UNIT.match(unit):
        return None, _err("invalid_args", "unit must be a loadgov-*.scope unit name")
    return unit, None


def _unregistered(d, unit: str, args: dict, peer_pid: int | None) -> Job | str | None:
    """The overrun report for a light unit the daemon neither runs nor remembers (its `job_start` failed, a
    daemon restart or 64 later reports dropped it): `project` and `label` (and `kind` light) ride on
    `job_end`. The unit name ends in the pid of the `loadctl run` that made it (run.py `unit_name`); only
    that peer may report it, else the reason string is returned (refused). One audit per unit; nothing else
    is booked."""
    project, label = args.get("project"), args.get("label")
    if args.get("kind") != "light" or not unit.startswith("loadgov-light-") or unit in d.jobs.recent:
        return None
    if not isinstance(project, str) or not isinstance(label, str):
        return None
    if _text(args, "project")[1] or _text(args, "label")[1]:
        return None
    tail = unit.removesuffix(".scope").rsplit("-", 1)[-1]
    if peer_pid is None or not tail.isdigit() or int(tail) != peer_pid:
        return f"overrun report for {unit} refused: the peer is not the loadctl run that started it"
    j = Job("light", project.strip(), label.strip(), unit, "", peer_pid, None, peer_pid, None, d.clock())
    return d.jobs.unregistered_overrun(j)


def handle(d, op: str, args: dict, peer_pid: int | None) -> dict:
    """`d` is the Daemon (jobs, slots, audit, clock, _after_change)."""
    unit, bad = _unit(args)
    if bad:
        return bad
    ctx = {**caller_info(d.fs, peer_pid), "op": op, "request": args}
    if op == "job_end":
        overrun = args.get("overrun")
        if overrun is not None and overrun not in OVERRUNS:
            return _err("invalid_args", f"overrun must be one of: {', '.join(OVERRUNS)}")
        running = d.jobs.jobs.get(unit)
        reason = "job_end"
        if overrun:
            c = d.config.light
            why = "light_overrun: " + (
                f"runtime {c.max_runtime_s} s exceeded" if overrun == "runtime"
                else f"memory limit {c.memory_max_mb} MB hit"
            )  # fmt: skip
            hit = None
            if running:
                if running.kind == "light":  # a light job hit its runtime or memory limit
                    hit, reason = running, why
            elif (hit := d.jobs.late_overrun(unit)) is None:  # the tick may have ended it first
                hit = _unregistered(d, unit, args, peer_pid)  # or it never registered, or was forgotten
            if isinstance(hit, str):
                d.audit.append(
                    {"ts": iso(d.clock()), **ctx, "principal": "agent", "result": "refused", "reason": hit}
                )
                return _err("owner_denied", hit)
            if hit:
                d.jobs.overrun(hit, why)
                d.watch.note_overrun(hit.project, hit.label)
        j = d.jobs.end(unit, reason)
        released = False
        if j and j.slot and not (d.jobs.scope_reader(j.cgroup) or "").strip():
            # a scope that still has processes keeps the slot; the tick frees it when the scope empties
            released = d.slots.release(j.slot, j.owner_pid)["released"]
        d._after_change(ctx)
        return {"ok": True, "unit": unit, "ended": j is not None, "released": released}
    kind = args.get("kind")
    if kind not in KINDS:
        return _err("invalid_args", f"kind must be one of: {', '.join(KINDS)}")
    project, bad = _text(args, "project")
    if bad:
        return bad
    label, bad = _text(args, "label")
    if bad:
        return bad
    cgroup = args.get("cgroup")
    if not isinstance(cgroup, str):
        return _err("bad_request", "cgroup must be a string")
    if ".." in cgroup or len(cgroup) > 400 or not _CGROUP.match(cgroup) or not cgroup.endswith(f"/{unit}"):
        return _err("invalid_args", f"cgroup must be an absolute path ending in /{unit}")
    key = args.get("key")
    if key is not None and (why := key_error(key)):
        return _err("bad_request" if not isinstance(key, str) else "invalid_args", why)
    pid, bad = _int(args, "pid")
    if bad:
        return bad
    owner, bad = _int(args, "owner_pid", required=False)
    if bad:
        return bad
    owner = owner or peer_pid
    slot = args.get("slot")
    if slot is not None and slot not in SLOT_FILES:
        return _err("invalid_args", f"slot must be one of: {', '.join(SLOT_FILES)}")
    if owner is None:
        return _err("invalid_args", "owner_pid is required (the loadctl run process)")
    if not pid_alive(pid, d.slots.stat_reader):
        return _err("invalid_args", f"job pid {pid} is not running")
    if unit in d.jobs.jobs:
        return _err("invalid_args", f"job {unit} is already registered")
    registered = d.clock()
    bound = False
    if slot:
        bound = d.slots.bind_scope(slot, owner, unit, cgroup, pid, kind, key, registered)
        if not bound and not d.slots.is_nested(slot, owner):
            return _err("invalid_args", f"pid {owner} does not hold the {slot} slot")
        if not bound:
            slot = None  # a nested grant: the job runs in its own scope but binds no slot
    j = Job(kind, project, label, unit, cgroup, pid, d.jobs.start_of(pid), owner, slot, registered, key=key)
    d.jobs.add(j)
    d.backstop.on_job(unit, cgroup)  # memory.oom.group = 1 on the new scope (backstop enabled only)
    d._after_change({**ctx, "project": project})
    return {"ok": True, "unit": unit, "slot": slot, "bound": bound}
