"""Heavy and compile slot arbiter, interoperable with the legacy flock protocol.

Two slots, each a lock file plus a holder file under `HostFS.lock_root`. The daemon arbitrates among
its own clients with a FIFO queue. A legacy holder (a bash process with the lock on an fd) is never
broken: the slot then counts as held (`source: legacy`) and acquisition waits.

With `enforce` off the daemon never opens the real lock files for locking and never writes holder
files; it only logs `WOULD flock ...` events. With `enforce` on, the lock of a client holder lives with
the client, like the legacy protocol (the gate shell holds fd 9): the daemon only probes that the real
lock is free before granting, and `loadctl slot acquire --fd N` takes `flock` on the inherited
descriptor and writes the legacy holder line. The daemon keeps no flock for client holders, so a daemon
crash never frees a lock whose gate is still running. Only daemon-owned holds (owner hold, drain)
are flocked by the daemon itself and end with it.

A loadgov holder is bound to a holder ref: a pid (plus start time) until `loadctl run` registers its job,
then the job scope (`bind_scope`): the slot stays held while the scope's `cgroup.procs` is non-empty, even
after `loadctl run` itself died, and is released on explicit release or on the first tick after the ref is
gone. A dropped client connection never releases it (ADR-0001).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field

from loadgov.host.procfs import HostFS
from loadgov.sample import (
    _holder_has_lock_open,
    _kernel_lockers,
    _lock_identity,
    _since_epoch,
    parse_holder,
)

log = logging.getLogger("loadgovd")

DEFAULT_COMPILE_LOCK = "host-compile-slot.lock"
SLOT_FILES = {
    "heavy": ("host-heavy-slot.lock", "host-heavy-slot.holder"),
    "compile": (DEFAULT_COMPILE_LOCK, "host-compile-slot.holder"),
}


def set_compile_lock(name: str | None = None) -> None:
    """The compile slot's lock file: `name`, else $LOADGOV_COMPILE_LOCK, else the generic default. The
    holder file is the same name with `.holder` for `.lock`."""
    name = name or os.environ.get("LOADGOV_COMPILE_LOCK") or DEFAULT_COMPILE_LOCK
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.lock", name):
        raise ValueError(f"LOADGOV_COMPILE_LOCK: {name!r} is not a plain *.lock file name")
    SLOT_FILES["compile"] = (name, name[: -len(".lock")] + ".holder")


STUCK_S = 45 * 60
QUEUE_STALE_S = 60  # a waiter that has not polled for this long is gone
HOLD_PROJECT, HOLD_LABEL = "home-assistant", "hold"
OWNER_ORIGIN, PROFILE_ORIGIN = "owner", "profile"


def read_stat(pid: int) -> str | None:
    """/proc/<pid>/stat from the real /proc (holder pids are real host pids), None when gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read()
    except OSError:
        return None


def parse_stat(text: str | None) -> tuple[str, int] | None:
    """(state, start time in ticks = field 22) of a stat line."""
    try:
        rest = (text or "").rpartition(")")[2].split()
        return rest[0], int(rest[19])
    except (IndexError, ValueError):
        return None


def pid_alive(pid: int, reader=read_stat) -> bool:
    """Alive and not a zombie."""
    st = parse_stat(reader(pid))
    return st is not None and st[0] not in ("Z", "X")


@dataclass(frozen=True)
class HolderRef:
    """What a holder is bound to: a pid plus its start time (a reused pid is a different process), or,
    with `cgroup`, a job scope that lives while its `cgroup.procs` is non-empty. `procs` reads that file
    (None = the cgroup is gone or unknown); then the pid and start time decide. A scope whose
    `cgroup.procs` cannot be read counts as alive (fail safe: the slot stays held) and is read again on
    the next tick."""

    pid: int
    start: int | None = None
    cgroup: str | None = None

    def alive(self, reader=read_stat, procs=None) -> bool:
        if self.cgroup and procs is not None:
            try:
                text = procs(self.cgroup)
            except OSError:
                return True
            if text is not None:
                return bool(text.strip())
        st = parse_stat(reader(self.pid))
        if st is None or st[0] in ("Z", "X"):
            return False
        return self.start is None or st[1] == self.start


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Holder:
    project: str
    label: str
    pid: int
    since: float
    kind: str = "job"  # "job" or "hold"
    start: int | None = None  # process start time (stat field 22) when granted: guards against pid reuse
    scope: str | None = None  # job scope unit once `loadctl run` registered its job (bind_scope)
    cgroup: str | None = None
    job_pid: int | None = None
    job_start: int | None = None
    client_lock: bool = False  # the client takes the real flock itself (loadctl run, --fd)
    orphan: bool = False  # the client (`pid`) is gone but the scope lives: the event is raised once

    @property
    def ref(self) -> HolderRef:
        if self.scope and self.cgroup and self.job_pid:
            return HolderRef(self.job_pid, self.job_start, self.cgroup)
        return HolderRef(self.pid, self.start)

    def line(self) -> str:
        return f"{self.project} {self.label} pid={self.pid} since={_stamp(self.since)}"


@dataclass
class Waiter:
    project: str
    label: str
    pid: int
    seen: float
    deadline: float | None = None
    client_lock: bool = False


@dataclass
class Slot:
    name: str
    lock: str
    holder_file: str
    holder: Holder | None = None
    fd: int | None = None  # the daemon's own flock, for daemon-owned holds only
    queue: list[Waiter] = field(default_factory=list)
    hold: dict | None = None  # {reason, since, origin}
    legacy: dict | None = None  # {line, pid, since} when someone loadgov did not grant holds the lock


class SlotManager:
    def __init__(self, fs: HostFS, clock=time.time, enforce: bool = False, self_pid: int | None = None,
                 stat_reader=read_stat, scope_reader=None):  # fmt: skip
        self.fs, self.clock, self.enforce, self.stat_reader = fs, clock, enforce, stat_reader
        self.scope_reader = scope_reader or fs.scope_procs
        self.self_pid = os.getpid() if self_pid is None else self_pid
        self.slots = {n: Slot(n, *files) for n, files in SLOT_FILES.items()}
        self.events: list[dict] = []

    # ---- events ----------------------------------------------------------------------------------

    def _ev(self, op, result, reason, s, principal="system", project=None, pid=None, before=None, after=None,
            **request) -> None:  # fmt: skip
        self.events.append(
            {"op": op, "principal": principal, "result": result, "reason": reason, "project": project,
             "pid": pid, "before": before, "after": after, "request": {"slot": s.name, **request}}
        )  # fmt: skip

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    # ---- the real lock ---------------------------------------------------------------------------

    def _detect_legacy(self, s: Slot) -> dict | None:
        """Read-only: a live kernel locker, or the holder line's pid alive with the lock file open."""
        ident = _lock_identity(self.fs, s.lock)
        if ident is None:
            return None
        lockers = [p for p in _kernel_lockers(self.fs, ident) if p != self.self_pid]
        line = (self.fs.read_lock_file(s.holder_file) or "").strip()
        parsed = parse_holder(line) if line else None
        named = bool(
            parsed
            and parsed["pid"] != self.self_pid
            and (parsed["pid"] in lockers or _holder_has_lock_open(self.fs, parsed["pid"], ident))
        )
        if not lockers and not named:
            return None
        return {
            "line": line if named else None,
            "pid": parsed["pid"] if named else None,
            "since": _since_epoch(parsed["since"]) if named else None,
        }

    def _open_lock(self, s: Slot) -> int:
        return os.open(self.fs.lock_path(s.lock), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)

    def _probe(self, s: Slot) -> bool:
        """True when nobody holds the real lock right now (taken and dropped at once)."""
        fd = self._open_lock(s)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)  # closing the descriptor drops the flock
        return True

    def _lock(self, s: Slot) -> bool:
        """Daemon-owned hold: take the real lock and keep it. False when somebody else holds it."""
        fd = self._open_lock(s)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except OSError:
            os.close(fd)
            raise
        s.fd = fd
        return True

    def _unlock(self, s: Slot) -> None:
        if s.fd is not None:
            os.close(s.fd)
            s.fd = None

    def _take(self, s: Slot, h: Holder) -> bool:
        """Grant holder `h` (or log what would happen). False: not grantable now.

        A job holder's real lock is taken by its client; here the daemon only checks it is free. A hold
        is the daemon's own: it flocks the lock file and writes the holder line itself."""
        path = self.fs.lock_path(s.lock)
        # `enforce` governs what the daemon does itself (holds). A client that takes the real flock itself
        # replaces a legacy lock user, so it always does, in either mode.
        real = self.enforce if h.kind == "hold" else (self.enforce or h.client_lock)
        if not real:
            hpath = self.fs.lock_path(s.holder_file)
            self._ev(
                "slot_lock", "would", f"WOULD flock {path}; WOULD write {hpath}: {h.line()}",
                s, project=h.project, pid=h.pid,
            )  # fmt: skip
            log.info("WOULD flock %s for %s", path, h.line())
            return True
        try:
            if h.kind == "hold":
                if not self._lock(s):
                    s.legacy = {"line": None, "pid": None, "since": None}  # held, holder unknown
                    return False
                with open(self.fs.lock_path(s.holder_file), "w") as f:
                    f.write(h.line() + "\n")
            elif not self._probe(s):
                s.legacy = {"line": None, "pid": None, "since": None}
                return False
        except OSError as e:
            self._unlock(s)
            log.error("slot %s: cannot lock %s: %s", s.name, path, e)
            self._ev("slot_lock", "error", f"cannot lock {path}: {e}", s, project=h.project, pid=h.pid)
            return False
        return True

    # ---- state machine ---------------------------------------------------------------------------

    def _advance(self, s: Slot) -> None:
        """Free slot: a pending hold takes it first, else the head of the queue. Never preempts."""
        if s.holder is not None:
            return
        s.legacy = self._detect_legacy(s)
        if s.legacy is not None:
            return
        now = self.clock()
        if s.hold is not None:
            h = Holder(HOLD_PROJECT, HOLD_LABEL, self.self_pid, now, "hold", self._start(self.self_pid))
            if self._take(s, h):
                s.holder = h
                self._ev(
                    "hold", "granted", f"slot held: {s.hold['reason']}", s, self._who(s.hold), after=h.line()
                )
            return
        while s.queue:
            w = s.queue[0]
            h = Holder(w.project, w.label, w.pid, now, "job", self._start(w.pid), client_lock=w.client_lock)
            if not self._take(s, h):
                return
            s.queue.pop(0)
            s.holder = h
            self._ev("slot_acquire", "granted", "slot granted", s, "agent", w.project, w.pid, after=h.line())
            return

    def _start(self, pid: int) -> int | None:
        st = parse_stat(self.stat_reader(pid))
        return st[1] if st else None

    def _alive(self, pid: int) -> bool:
        return pid_alive(pid, self.stat_reader)

    @staticmethod
    def _who(hold: dict) -> str:
        return OWNER_ORIGIN if hold["origin"] == OWNER_ORIGIN else "system"

    def _release(self, s: Slot, reason: str, principal: str = "system") -> None:
        h, s.holder = s.holder, None
        self._unlock(s)  # the holder file stays: legacy scripts never delete it either
        if h:
            self._ev("slot_release", "released", reason, s, principal, h.project, h.pid, before=h.line())

    def tick(self) -> bool:
        """Release holders whose ref is gone, drop dead waiters, hand out free slots."""
        now, n = self.clock(), len(self.events)
        for s in self.slots.values():
            if s.holder and not s.holder.ref.alive(self.stat_reader, self.scope_reader):
                self._release(s, "released: holder exited")
            elif s.holder and s.holder.scope and not s.holder.orphan and not self._alive(s.holder.pid):
                s.holder.orphan = True  # the client died but its scope runs, so the slot stays held
                self._ev(
                    "slot_holder_orphaned", "orphaned",
                    f"client pid {s.holder.pid} is gone, scope {s.holder.scope} still runs: slot stays held",
                    s, project=s.holder.project, pid=s.holder.pid, before=s.holder.line(),
                )  # fmt: skip
            for w in [w for w in s.queue if not self._alive(w.pid) or self._stale(w, now)]:
                s.queue.remove(w)
                self._ev("slot_acquire", "dropped", "waiter gone or timed out", s, "agent", w.project, w.pid)
            self._advance(s)
        return len(self.events) > n

    @staticmethod
    def _stale(w: Waiter, now: float) -> bool:
        return now - w.seen > QUEUE_STALE_S or (w.deadline is not None and now > w.deadline)

    # ---- operations ------------------------------------------------------------------------------

    def _granted(self, s: Slot, reply: dict) -> dict:
        """For a client that holds the real lock: which lock to take and which holder line to write."""
        h = s.holder
        out = {**reply, "status": "granted", "holder": h.line()}
        if self.enforce or h.client_lock:
            out |= {
                "lock_path": self.fs.lock_path(s.lock),
                "holder_path": self.fs.lock_path(s.holder_file),
                "holder_line": h.line(),
            }
        return out

    def _describe(self, s: Slot) -> str:
        if s.holder:
            return s.holder.line()
        if s.legacy and s.legacy["line"]:
            return s.legacy["line"]
        return "unknown"

    def acquire(self, name: str, project: str, label: str, pid: int, wait: bool, timeout_s=None,
                client_lock: bool = False) -> dict:  # fmt: skip
        s, now = self.slots[name], self.clock()
        h = s.holder
        reply = {"ok": True, "slot": name, "who": f"{project} {label}"}
        if h and h.kind == "job" and (h.pid, h.project, h.label) == (pid, project, label):
            return self._granted(s, reply)
        w = next((w for w in s.queue if (w.pid, w.project, w.label) == (pid, project, label)), None)
        fresh = w is None
        if w is None:
            w = Waiter(project, label, pid, now, client_lock=client_lock)
            s.queue.append(w)
            if timeout_s is not None:
                w.deadline = now + timeout_s
        w.seen = now
        w.client_lock = client_lock
        if w.deadline is not None and now > w.deadline:
            s.queue.remove(w)
            return {**reply, "status": "timeout", "holder": self._describe(s)}
        self._advance(s)
        h = s.holder
        if h and h.kind == "job" and (h.pid, h.project, h.label) == (pid, project, label):
            return self._granted(s, reply)
        if not wait:
            if fresh:
                s.queue.remove(w)
            return {**reply, "status": "busy", "holder": self._describe(s), "queue": len(s.queue)}
        if fresh:
            self._ev("slot_acquire", "queued", f"queued behind {self._describe(s)}", s, "agent", project, pid)
        return {**reply, "status": "queued", "position": s.queue.index(w) + 1, "holder": self._describe(s)}

    def release(self, name: str, pid: int) -> dict:
        """Release the slot a job holds for `pid` (a hold ends with `hold_off`) and leave the queue."""
        s = self.slots[name]
        released = False
        if s.holder and s.holder.kind == "job" and s.holder.pid == pid:
            self._release(s, "released: explicit release", "agent")
            released = True
        s.queue = [w for w in s.queue if w.pid != pid]
        self._advance(s)
        return {"ok": True, "slot": name, "released": released}

    def bind_scope(self, name: str, owner_pid: int, scope: str, cgroup: str, job_pid: int) -> bool:
        """`loadctl run` (pid `owner_pid`) holds the slot: bind it to the job scope from now on."""
        h = self.slots[name].holder
        if not (h and h.kind == "job" and h.pid == owner_pid):
            return False
        h.scope, h.cgroup, h.job_pid, h.job_start = scope, cgroup, job_pid, self._start(job_pid)
        return True

    def hold_on(self, names: list[str], reason: str, origin: str = OWNER_ORIGIN) -> None:
        for n in names:
            s = self.slots[n]
            if s.hold and s.hold["origin"] == OWNER_ORIGIN and origin != OWNER_ORIGIN:
                continue
            s.hold = {"reason": reason, "since": self.clock(), "origin": origin}
            self._ev("hold", "accepted", reason, s, self._who(s.hold))
            self._advance(s)

    def hold_off(self, names: list[str], origin: str = OWNER_ORIGIN) -> list[str]:
        """Clear the holds of this origin; a hold of the other origin stays."""
        cleared = []
        for n in names:
            s = self.slots[n]
            if s.hold is None or s.hold["origin"] != origin:
                continue
            hold, s.hold = s.hold, None
            if s.holder and s.holder.kind == "hold":
                self._release(s, "released: hold off", self._who(hold))
            self._ev("hold", "cleared", hold["reason"], s, self._who(hold))
            cleared.append(n)
            self._advance(s)
        return cleared

    def set_profile_hold(self, desired: bool) -> None:
        """The drain profile's `hold_heavy_slot`: hold the heavy slot while that profile is in force."""
        if desired and self.slots["heavy"].hold is None:
            self.hold_on(["heavy"], "profile with hold_heavy_slot (drain)", PROFILE_ORIGIN)
        elif not desired:
            self.hold_off(["heavy"], PROFILE_ORIGIN)

    def owner_hold_reason(self) -> str | None:
        reasons = [
            s.hold["reason"] for s in self.slots.values() if s.hold and s.hold["origin"] == OWNER_ORIGIN
        ]
        return reasons[0] if reasons else None

    # ---- reading and persistence -----------------------------------------------------------------

    def summary(self) -> dict:
        now, out = self.clock(), {}
        for n, s in self.slots.items():
            h, lg = s.holder, s.legacy
            if h:
                state = "held-by-hold" if h.kind == "hold" else "held"
                holder, source, since = h.line(), "loadgov", h.since
            elif lg:
                state, holder, source, since = "held", lg["line"], "legacy", lg["since"]
            else:
                state, holder, source, since = "free", None, None, None
            out[n] = {
                "state": state,
                "holder": holder,
                "source": source,
                "queue": len(s.queue),
                "held_since": _stamp(since) if since is not None else None,
                "stuck": since is not None and now - since > STUCK_S,
                "hold": s.hold["reason"] if s.hold else None,
                "hold_origin": s.hold["origin"] if s.hold else None,
            }
        return out

    def queues(self) -> dict:
        return {
            n: [{"project": w.project, "label": w.label, "pid": w.pid} for w in s.queue]
            for n, s in self.slots.items()
        }

    def to_state(self) -> dict:
        return {
            n: {
                "holder": asdict(s.holder) if s.holder and s.holder.kind == "job" else None,
                "hold": s.hold,
            }
            for n, s in self.slots.items()
        }

    def restore(self, raw: dict) -> None:
        """Re-adopt saved holders whose ref is alive (same pid and start time); restore holds."""
        for n, saved in (raw or {}).items():
            s = self.slots.get(n)
            if s is None or not isinstance(saved, dict):
                continue
            try:
                if saved.get("hold"):
                    hd = saved["hold"]
                    s.hold = {
                        "reason": str(hd["reason"]),
                        "since": float(hd["since"]),
                        "origin": str(hd["origin"]),
                    }
                if not saved.get("holder"):
                    continue
                h = Holder(**saved["holder"])
            except (KeyError, TypeError, ValueError):
                log.warning("ignoring unreadable saved slot %s", n)
                continue
            if not h.ref.alive(self.stat_reader, self.scope_reader):
                self._ev(
                    "slot_release", "released", "released: holder exited (daemon was down)", s,
                    project=h.project, pid=h.pid, before=h.line(),
                )  # fmt: skip
                continue
            s.holder = h  # the client's lock is untouched; only the bookkeeping is re-attached
            self._ev(
                "slot_acquire", "adopted", "holder re-adopted after restart", s,
                project=h.project, pid=h.pid, after=h.line(),
            )  # fmt: skip
        for s in self.slots.values():
            if s.hold:
                self._advance(s)  # a saved hold takes its free slot now
