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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from loadgov.engines.eta import finish_estimate, queue_starts
from loadgov.host.procfs import HostFS
from loadgov.keys import key_url
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


STUCK_S = 45 * 60  # held longer than this ...
STUCK_IDLE_S = 10 * 60  # ... and without CPU progress for this long is stuck
PROGRESS_SAMPLE_S = 60  # CPU time of the holder is read this often
PROGRESS_MIN_CORES = 0.01  # less than 1 % of one core between two samples is no progress
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


def parse_ppid(text: str | None) -> int | None:
    """Parent pid (field 4) of a stat line."""
    try:
        return int((text or "").rpartition(")")[2].split()[1])
    except (IndexError, ValueError):
        return None


MAX_DEPTH = 64  # ancestor levels walked for a nested request


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
    job_kind: str | None = None  # kind of the job (impl, gate, ...) when the client said so
    key: str | None = None  # issue key the client passed; never derived
    job_since: float | None = None  # registration time of the bound job (jobs[].since); None until bound

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
    since: float = 0.0  # first request time (`seen` is the last poll)
    kind: str | None = None
    key: str | None = None


@dataclass
class Progress:
    """CPU progress of the current holder: a few numbers, no history. `key` names what is watched, so a new
    holder starts a new observation; `progress_t` starts at the first reading."""

    key: tuple
    at: float  # last attempt to read
    cpu_t: float | None = None  # time of the last good reading
    cpu: float | None = None  # CPU seconds then
    progress_t: float | None = None  # last time at least PROGRESS_MIN_CORES were used between two readings
    known: bool = False  # the last attempt could read the data


@dataclass
class Nested:
    """A re-entrant grant: bookkeeping only, no flock and no holder line. Not saved across a restart."""

    project: str
    label: str
    pid: int
    start: int | None
    since: float


@dataclass
class Slot:
    name: str
    lock: str
    holder_file: str
    holder: Holder | None = None
    fd: int | None = None  # the daemon's own flock, for daemon-owned holds only
    queue: list[Waiter] = field(default_factory=list)
    nested: list[Nested] = field(default_factory=list)  # grants to descendants of the current holder
    progress: Progress | None = None
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
        # watched-holder key -> Docker CPU seconds attributed to its jobs (UsageMeter); set by the daemon
        self.docker_cpu: Callable[[tuple], float] | None = None
        # (project, kind) -> median duration of finished jobs in seconds, None when unknown; set by the daemon
        self.expected_s: Callable[[str, str], float | None] = lambda project, kind: None
        self.url_template = ""  # [keys] url

    # ---- events ----------------------------------------------------------------------------------

    def _ev(self, op, result, reason, s, principal="system", project=None, pid=None, before=None, after=None,
            **request) -> None:  # fmt: skip
        req = {k: v for k, v in request.items() if v is not None}  # an absent key stays out of the audit line
        self.events.append(
            {"op": op, "principal": principal, "result": result, "reason": reason, "project": project,
             "pid": pid, "before": before, "after": after, "request": {"slot": s.name, **req}}
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
            h = Holder(
                w.project, w.label, w.pid, now, "job", self._start(w.pid), client_lock=w.client_lock,
                job_kind=w.kind, key=w.key,
            )  # fmt: skip
            if not self._take(s, h):
                return
            s.queue.pop(0)
            s.holder = h
            self._ev(
                "slot_acquire", "granted", "slot granted", s, "agent", w.project, w.pid, after=h.line(),
                key=w.key,
            )  # fmt: skip
            return

    def _start(self, pid: int) -> int | None:
        st = parse_stat(self.stat_reader(pid))
        return st[1] if st else None

    def _alive(self, pid: int) -> bool:
        return pid_alive(pid, self.stat_reader)

    # ---- CPU progress (stuck) --------------------------------------------------------------------

    def _tree_cpu(self, root: int) -> float | None:
        """CPU seconds (utime + stime, plus reaped children) of `root` and its descendants from /proc."""
        procs: dict[int, tuple[int, int]] = {}
        try:
            pids = list(self.fs.pids())
        except OSError:
            return None
        for pid in pids:
            try:
                rest = self.fs.proc(f"{pid}/stat").rpartition(")")[2].split()
                procs[pid] = (int(rest[1]), sum(int(x) for x in rest[11:15]))
            except (OSError, ValueError, IndexError):
                continue  # gone between the listing and the read
        if root not in procs:
            return None
        kids: dict[int, list[int]] = {}
        for pid, (ppid, _) in procs.items():
            kids.setdefault(ppid, []).append(pid)
        total, todo, seen = 0, [root], set()
        while todo:
            pid = todo.pop()
            if pid not in seen:
                seen.add(pid)
                total += procs[pid][1]
                todo.extend(kids.get(pid, ()))
        return total / self.fs.clk_tck

    def _scope_cpu(self, cgroup: str) -> float | None:
        text = self.fs.cgroup(cgroup, "cpu.stat")
        for line in (text or "").splitlines():
            if line.startswith("usage_usec "):
                try:
                    return int(line.split()[1]) / 1e6
                except (IndexError, ValueError):
                    return None
        return None

    @staticmethod
    def _watch_key(s: Slot) -> tuple | None:
        """What is watched for CPU progress: the holder's job scope when it has one, else the holder pid's
        process tree (also a legacy holder named by its holder line). None: free, or an owner hold."""
        h = s.holder
        if h is not None and h.kind == "job":
            if h.scope and h.cgroup:
                return ("scope", h.cgroup, h.scope)
            return ("pid", h.pid, h.start)
        if h is None and s.legacy and s.legacy.get("pid"):
            return ("legacy", s.legacy["pid"], s.legacy.get("since"))
        return None

    def _read_cpu(self, key: tuple) -> float | None:
        base = self._scope_cpu(key[1]) if key[0] == "scope" else self._tree_cpu(key[1])
        if base is None:
            return None
        return base + (self.docker_cpu(key) if self.docker_cpu else 0.0)  # Docker CPU the jobs caused

    def _sample_progress(self, s: Slot, now: float) -> None:
        key = self._watch_key(s)
        if key is None:
            s.progress = None
            return
        p = s.progress
        if p is not None and p.key != key:
            p = None  # another holder: a new observation
        if p is not None and now - p.at < PROGRESS_SAMPLE_S:
            return
        cpu = self._read_cpu(key)
        if p is None:
            p = s.progress = Progress(key, now)
        p.at, p.known = now, cpu is not None
        if cpu is None:
            return  # unreadable: unknown, never stuck; the last good reading stays the baseline
        if p.cpu is None:
            p.progress_t = now  # the observation starts here
        elif cpu < p.cpu or (cpu - p.cpu) / max(now - p.cpu_t, 1e-9) >= PROGRESS_MIN_CORES:
            p.progress_t = now  # a falling total means processes came and went: that is activity
        p.cpu, p.cpu_t = cpu, now

    def _nested_alive(self, n: Nested) -> bool:
        st = parse_stat(self.stat_reader(n.pid))
        return st is not None and st[0] not in ("Z", "X") and (n.start is None or st[1] == n.start)

    def _in_scope(self, pid: int, cgroup: str) -> bool:
        try:
            for line in self.fs.proc(f"{pid}/cgroup").splitlines():
                if line.startswith("0::"):
                    path = line[3:]
                    return path == cgroup or path.startswith(cgroup.rstrip("/") + "/")
        except OSError:
            pass
        return False

    def _ancestor_holder(self, h: Holder, pids: list[int | None]) -> str | None:
        """How a request from `pids` (the claimed pid and the socket peer) descends from the holder `h`,
        else None. Each ancestor counts as the holder only with the same start time (a reused pid is a
        different process); a scope holder also counts every process inside its cgroup."""
        known = [(h.pid, h.start, "holder pid")]
        if h.job_pid:
            known.append((h.job_pid, h.job_start, "job pid"))
        for cur in dict.fromkeys(p for p in pids if p):
            for _ in range(MAX_DEPTH):
                if cur <= 1:
                    break
                text = self.stat_reader(cur)
                st = parse_stat(text)
                if st is None:
                    break
                for pid, start, what in known:
                    if cur == pid and start is not None and st[1] == start:
                        return f"{what} {pid}"
                if h.scope and h.cgroup and self._in_scope(cur, h.cgroup):
                    return f"pid {cur} in scope {h.scope}"
                nxt = parse_ppid(text)
                if nxt is None:
                    break
                cur = nxt
        return None

    @staticmethod
    def _who(hold: dict) -> str:
        return OWNER_ORIGIN if hold["origin"] == OWNER_ORIGIN else "system"

    def _release(self, s: Slot, reason: str, principal: str = "system") -> None:
        h, s.holder = s.holder, None
        s.nested.clear()  # bookkeeping of the ended holder only: the real slot is gone with it
        self._unlock(s)  # the holder file stays: legacy scripts never delete it either
        if h:
            self._ev("slot_release", "released", reason, s, principal, h.project, h.pid, before=h.line())

    def set_enforce(self, enforce: bool) -> None:
        """Change `enforce` live. A daemon-owned hold is brought to the state a restart would give: the
        old one is dropped (its real flock closed when there was one) and the pending hold is taken again
        in the new mode, or stays pending when a legacy holder has the lock. Job holders are untouched."""
        if enforce == self.enforce:
            return
        self.enforce = enforce
        for s in self.slots.values():
            if s.holder is not None and s.holder.kind == "hold":
                self._release(s, f"released: enforce {'on' if enforce else 'off'} (reload)")
            if s.hold:
                self._advance(s)

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
            s.nested = [n for n in s.nested if self._nested_alive(n)]
            for w in [w for w in s.queue if not self._alive(w.pid) or self._stale(w, now)]:
                s.queue.remove(w)
                self._ev(
                    "slot_acquire", "dropped", "waiter gone or timed out", s, "agent", w.project, w.pid,
                    key=w.key,
                )  # fmt: skip
            self._advance(s)
            self._sample_progress(s, now)
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
                client_lock: bool = False, peer_pid: int | None = None, kind: str | None = None,
                key: str | None = None) -> dict:  # fmt: skip
        s, now = self.slots[name], self.clock()
        h = s.holder
        reply = {"ok": True, "slot": name, "who": f"{project} {label}"}
        if h and h.kind == "job" and (h.pid, h.project, h.label) == (pid, project, label):
            return self._granted(s, reply)
        if h and h.kind == "job":  # a request from below the holder never waits for it (re-entrant)
            nested = {"status": "granted", "nested": True, "holder": h.line()}
            if any((n.pid, n.project, n.label) == (pid, project, label) for n in s.nested):
                return {**reply, **nested}
            via = self._ancestor_holder(h, [pid, peer_pid])
            if via:
                s.queue = [w for w in s.queue if (w.pid, w.project, w.label) != (pid, project, label)]
                s.nested.append(Nested(project, label, pid, self._start(pid), now))
                self._ev(
                    "slot_acquire", "nested", f"nested under holder {h.line()} (ancestor: {via})", s,
                    "agent", project, pid, after=h.line(), key=key,
                )  # fmt: skip
                return {**reply, **nested}
        w = next((w for w in s.queue if (w.pid, w.project, w.label) == (pid, project, label)), None)
        fresh = w is None
        if w is None:
            w = Waiter(project, label, pid, now, client_lock=client_lock, since=now)
            s.queue.append(w)
            if timeout_s is not None:
                w.deadline = now + timeout_s
        w.seen = now
        w.client_lock = client_lock
        w.kind, w.key = kind or w.kind, key or w.key
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
            self._ev(
                "slot_acquire", "queued", f"queued behind {self._describe(s)}", s, "agent", project, pid,
                key=key,
            )  # fmt: skip
        return {**reply, "status": "queued", "position": s.queue.index(w) + 1, "holder": self._describe(s)}

    def release(self, name: str, pid: int) -> dict:
        """Release the slot a job holds for `pid` (a hold ends with `hold_off`) and leave the queue."""
        s = self.slots[name]
        released = False
        mine = [i for i, n in enumerate(s.nested) if n.pid == pid]
        if mine:  # nested grants first, the most recent one (LIFO): the real slot stays bound
            del s.nested[mine[-1]]
            return {"ok": True, "slot": name, "released": True, "nested": True}
        if s.holder and s.holder.kind == "job" and s.holder.pid == pid:
            self._release(s, "released: explicit release", "agent")
            released = True
        s.queue = [w for w in s.queue if w.pid != pid]
        self._advance(s)
        return {"ok": True, "slot": name, "released": released}

    def bind_scope(self, name: str, owner_pid: int, scope: str, cgroup: str, job_pid: int,
                   kind: str | None = None, key: str | None = None,
                   since: float | None = None) -> bool:  # fmt: skip
        """`loadctl run` (pid `owner_pid`) holds the slot: bind it to the job scope from now on."""
        h = self.slots[name].holder
        if not (h and h.kind == "job" and h.pid == owner_pid):
            return False
        h.scope, h.cgroup, h.job_pid, h.job_start = scope, cgroup, job_pid, self._start(job_pid)
        h.job_kind, h.key = kind or h.job_kind, key or h.key
        h.job_since = since
        return True

    def is_nested(self, name: str, pid: int) -> bool:
        return any(n.pid == pid for n in self.slots[name].nested)

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
            held_s = int(now - since) if since is not None else None
            p = s.progress
            idle_s = int(now - p.progress_t) if p and p.known and p.progress_t is not None else None
            out[n] = {
                "key": h.key if h and h.kind == "job" else None,
                "url": key_url(self.url_template, h.key) if h and h.kind == "job" else None,
                "waiters": self._waiters(s, now),
                "state": state,
                "holder": holder,
                "source": source,
                "queue": len(s.queue),
                "nested": len(s.nested),
                "held_since": _stamp(since) if since is not None else None,
                "held_s": held_s,
                "idle_s": idle_s,
                "stuck": held_s is not None
                and held_s > STUCK_S
                and idle_s is not None
                and idle_s >= STUCK_IDLE_S,
                "hold": s.hold["reason"] if s.hold else None,
                "hold_origin": s.hold["origin"] if s.hold else None,
            }
        return out

    def _free_at(self, s: Slot, now: float) -> float | None:
        """When the slot is expected free: now when it is free, the holder job's expected finish while it
        is not overdue, else unknown (overdue, no statistics, any hold or a legacy holder). The start is the
        bound job's registration time (the same instant as its `jobs[].eta`),
        the grant time before it is bound."""
        if s.hold:
            return None  # pending or taken: the hold gets the slot before any waiter
        h = s.holder
        if h is None:
            return None if s.legacy else now
        if h.kind != "job" or h.job_kind is None:
            return None
        eta, overdue = finish_estimate(h.job_since or h.since, self.expected_s(h.project, h.job_kind), now)
        return None if overdue else eta

    def _waiters(self, s: Slot, now: float) -> list[dict]:
        expected = [self.expected_s(w.project, w.kind) if w.kind else None for w in s.queue]
        starts = queue_starts(self._free_at(s, now), expected)
        return [
            {
                "position": i,
                "project": w.project,
                "label": w.label,
                "kind": w.kind,
                "key": w.key,
                "url": key_url(self.url_template, w.key),
                "pid": w.pid,
                "queued_since": _stamp(w.since),
                "wait_s": max(0, int(now - w.since)),
                "expected_s": e,
                "eta": None if t is None else _stamp(t),
            }
            for i, (w, e, t) in enumerate(zip(s.queue, expected, starts, strict=True), 1)
        ]

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
