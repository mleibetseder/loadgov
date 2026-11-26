"""Memory backstop on `build.slice/build-work.slice`: a hard `memory.max` / `memory.swap.max` that
only fires when the memory guard was too slow, plus `memory.oom.group = 1` on every job scope so an OOM
kill takes that one scope down as a group and nothing else.

`[backstop] enabled = false` (default) is the enforce flag (AGENTS.md rule 2): nothing is written to any
cgroup file and systemctl is never run, except to undo what an earlier enabled run set. With it on, the
daemon sets exactly the configured values (never `max`, never below 4 GB for `memory.max`, checked by the
config) through systemd: `systemctl set-property --runtime <slice unit> MemoryMax=<n>M MemorySwapMax=<n>M`,
then verifies that the cgroup files hold them. A value written only to the cgroup file is overwritten by
systemd whenever it re-realizes the slice (for example when a scope with resource properties starts below
it); a runtime property is what systemd itself writes back then. systemctl runs at the first apply after a
start and whenever the files differ from the wanted values, never on every tick. A slice that is inactive
(no cgroup directory, e.g. right after boot) is started first. If systemctl fails, the files are written
directly and the status note says so.

A `memory.max` at or below the slice's current usage is never written (the kernel would OOM-kill running
builds at once). Instead the limit is a cap of `max(target, usage + cap_margin_mb)`, lowered to
`max(target, min(cap, usage + margin))` when that is at least a quarter margin lower (or the target), so
a step-down is not a systemctl call on every tick; one `backstop_cap` alert (warning) and one audit line
mark the start. The cap is kept in the marker, so a restart never lowers it below usage nor raises it while
it is in force; a saved cap that is no longer in the file while usage is at or above it starts a new
episode (plan_cap). The slice is never left unlimited while enabled.

Per-project caps (`[backstop] project_memory_max_mb`, `[projects.<name>] memory_max_mb`; 0 = none): under
`enabled`, `systemctl set-property --runtime build-work-<project>.slice MemoryMax=<cap>M MemoryHigh=<high>M`
(high = `project_memory_high_pct` of the cap), so a runaway is throttled, then confined to its own slice,
instead of filling build-work.slice and making the kernel kill other projects' jobs. The unit is run.py's
`slice_name`; systemd nests dashed names, so a cap on `build-work-a.slice` also covers `build-work-a-b.slice`.
A slice is capped at the daemon start, after a reload, when it is first seen (it holds a job scope) and when
its files differ; an inactive slice keeps the stored property until it starts. The same never-below-usage
capping applies. Capped slices are kept in the marker (`projects`); a cap that goes to 0 (also a removed
project while the default cap is 0; with a default > 0 it keeps the default) or the flag off is lifted
with `MemoryMax=infinity MemoryHigh=infinity`, retried after RETRY_S on failure. An unreadable marker
rolls back build-work.slice only (the project slices it named are unknown).
A rise of a project slice's own `memory.events` `oom_kill` (less its child slices') emits one
`backstop_oom` alert naming the project.

Bound: a tick makes at most one systemctl call for build-work.slice (a start, a set-property or its
rollback) and at most PROJECT_CALLS_PER_TICK for project slices, each with the runner's 10 s timeout.

Read-only, whatever `enabled` says: the `shmem` / `file` share of memory.stat per slice (status), the
leaked-memory alert (`backstop_leak`: more than `leak_alert_mb` of shmem in a slice whose subtree has
had no process for `leak_alert_min` minutes; tmpfs pages stay charged after a SIGKILL skipped a job's
cleanup) and the tmpfs leftover scan (leftovers.py). Nothing is ever deleted.

Detection: every tick the `oom_kill` / `oom_group_kill` counters of `memory.events` of the slice (they
include all descendant scopes) are compared with the last reading; an increase emits one `BACKSTOP` alert
(critical) naming the scope and its `memory.peak` (the slice's when the killed scope is already gone), plus an
audit line.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable

from loadgov.cli.run import slice_cgroup, slice_name
from loadgov.config import BackstopConfig
from loadgov.daemon import slot_ops
from loadgov.daemon.leftovers import JOB_END_DIRS, JOB_END_ENTRIES, LeftoverScan, describe, tmpfs_mounts
from loadgov.daemon.state import atomic_write_text
from loadgov.engines.governor import PRIMARY
from loadgov.engines.limits import iso
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS

log = logging.getLogger("loadgovd")
MIB = 1024 * 1024
LIMIT_FILES = ("memory.max", "memory.swap.max")
HOST_CGROUP_ROOT = "/sys/fs/cgroup"
RETRY_S = 60.0  # a failed `systemctl start` of an inactive slice, or a failed rollback: again after this
PROJECT_CALLS_PER_TICK = 1  # systemctl calls for project slices per tick (build-work.slice has its own one)


def slice_unit(slice_path: str) -> str:
    """The systemd unit of a slice cgroup path: `build.slice/build-work.slice` -> `build-work.slice`."""
    unit = slice_path.strip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.slice", unit):
        raise ValueError(f"not a slice cgroup path: {slice_path!r}")
    return unit


def trusted_runner(
    fs: HostFS, runner: Callable[[list[str]], int] | None
) -> Callable[[list[str]], int] | None:
    """The systemctl runner, fail closed: the daemon's real command runner is only used on the host's own
    cgroup tree. With an injected cgroup root (every unit test) it is refused, so no test reaches the host's
    systemd; an injected fake runner is used as given."""
    if runner is slot_ops.run_argv and os.path.realpath(fs.cgroup_root) != HOST_CGROUP_ROOT:
        return None
    return runner


def plan_cap(
    target_mb: int, capped_mb: int | None, have_max: str | None, usage: int | None, margin_mb: int
) -> tuple[int, int | None, bool]:
    """The memory.max to hold: (MB, the cap kept or None at the target, True when a capping episode starts).

    A value at or below the usage is never written: when the limit to hold (the target, or a cap held from
    the marker) is not what the file holds and usage is at or above it, a new cap of usage + margin starts.
    A cap steps down towards the target as usage falls, by at least a quarter margin or to the target."""
    used_mb = -(-usage // MIB) if usage is not None else 0  # rounded up: the cap stays above the usage
    roof = used_mb + margin_mb
    held = capped_mb if capped_mb is not None else target_mb
    if usage is not None and usage >= held * MIB and have_max != str(held * MIB):
        new = max(target_mb, roof)
        return new, (new if new > target_mb else None), True
    if capped_mb is None:
        return target_mb, None, False
    new = max(target_mb, min(capped_mb, roof))
    if new != target_mb and capped_mb - new < max(1, margin_mb // 4):
        new = capped_mb  # small steps are not worth a systemctl call: wait for a quarter margin
    return new, (new if new > target_mb else None), False


def _new_proj(capped: int | None = None) -> dict:
    return {"capped": capped, "sent": None, "retry_at": None, "undo_at": None, "applied": False}


class BackstopRuntime:
    def __init__(
        self,
        fs: HostFS,
        cfg: BackstopConfig,
        clock: Callable[[], float],
        state_dir: str,
        emit: Callable[[str, dict], object],  # Daemon.emit_event
        audit: Callable[[dict], None],  # AuditLog.append
        jobs: Callable[[], dict],  # unit -> Job (has .cgroup)
        slice_path: str = PRIMARY,  # test-only override (integration test on loadgov-test.slice)
        runner: Callable[[list[str]], int] | None = None,  # systemctl argv -> exit code; None = files only
        mount_root: str | None = None,  # test-only: where tmpfs mount points are opened (leftovers.py)
    ):
        self.fs, self.cfg, self.clock, self.emit, self.audit, self._jobs = fs, cfg, clock, emit, audit, jobs
        self.slice = slice_path
        self.unit = slice_unit(slice_path)
        self.runner = trusted_runner(fs, runner)
        self.marker = os.path.join(state_dir, "backstop.json")
        self.applied = False
        self.note: str | None = None
        self.via: str | None = None  # "systemd" | "file": how the limits were last applied
        self.oom_kills = 0
        self.max_events = 0
        self.swap_fail_events = 0
        self.last_event: str | None = None
        self._asserted = False  # set-property ran since the start
        self._fallback: str | None = None  # the note of the last direct-write fallback
        self._start_at: float | None = None  # last `systemctl start` of an inactive slice
        self._rollback_at: float | None = None  # last failed rollback
        saved = self._load_marker()
        if saved is None:  # as ILM-43 did whenever the file existed: undo build-work.slice; projects unknown
            saved = {"slice": self.slice}
            self.note = f"marker unreadable: {self.marker}; build-work.slice is rolled back, projects left"
            log.warning("backstop: %s", self.note)
            self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system",
                        "source": "backstop", "result": "failed", "reason": self.note,
                        "cgroup": self.slice})  # fmt: skip
        # memory.max held above the target while usage is above it; kept over a restart
        self.capped_mb: int | None = self._saved_cap(saved.get("capped_mb"))
        self._bw_marked = bool(saved.get("slice"))  # build-work.slice limits are to be undone
        marked = saved.get("projects")
        # project slice unit -> its cap (None = at its target): runtime properties to undo
        self._marked: dict[str, int | None] = {
            u: self._saved_cap(c)
            for u, c in (marked.items() if isinstance(marked, dict) else ())
            if isinstance(u, str) and re.fullmatch(r"[A-Za-z0-9_.\-]+\.slice", u)
        }
        self._proj: dict[str, dict] = {}  # unit -> {capped, sent, retry_at, undo_at, applied}
        self._held_scope: set[str] = set()  # project slice units seen holding a job scope
        self._proj_oom: dict[str, int] = {}  # slice path -> last oom_kill
        self._leak_since: dict[str, float] = {}  # slice path -> first tick of the leak condition
        self._leak_said: set[str] = set()
        self.leak_alerts = 0
        self._tree: list[tuple[str, bool]] = []  # (cgroup path, has processes) at and below the slice
        self.scan = LeftoverScan(fs, clock, cfg.leftover_report_mb, mount_root)
        self.scan.running = lambda: [
            {"unit": u, "project": j.project, "key": getattr(j, "key", None), "start": j.since}
            for u, j in self._jobs().items()
        ]
        self._waiting: list[tuple[str, str, str | None, float]] = []  # job ends whose scan still runs
        self._base: tuple[int, int] | None = None  # (oom_kill, oom_group_kill) at the last reading
        self._max_base: int | None = None
        self._swap_fail_base: int | None = None
        self._seen: dict[str, tuple[int, int]] = {}  # unit -> (last memory.peak MB, own oom_kill)
        self._grouped: set[str] = set()
        self._said: str | None = None

    # ---- driving ---------------------------------------------------------------------------------

    def configure(self, cfg: BackstopConfig) -> None:
        """A reload: the project caps are asserted again (one call per slice, bounded per tick)."""
        self.cfg = cfg
        self.scan.report_mb = cfg.leftover_report_mb
        for st in self._proj.values():
            st["sent"], st["retry_at"] = None, None

    def tick(self) -> None:
        self._read_tree()
        if self.cfg.enabled:
            self._apply()
            self._apply_projects()
            self._group_all()
            self._detect()
            self._detect_projects()
        elif os.path.exists(self.marker):
            self._rollback()
        self._check_leaks()
        if self.scan.step():
            self._resolve_waiting()

    def on_job(self, unit: str, job_cgroup: str) -> None:
        """A job was just registered: its scope exists, so `memory.oom.group` can be set at once."""
        if self.cfg.enabled:
            self._group(unit, job_cgroup)

    def job_ended(self, j, event: dict) -> None:
        """JobTable hook: a job ended. Scan the tmpfs mounts for its leftovers (read-only); a scan that
        finishes within one step puts them into the job_end audit line, else a `job_leftovers` line
        follows."""
        if not self.scan.enabled:
            return
        end = self.clock()
        key = getattr(j, "key", None)
        self.scan.note_end(j.unit, j.project, key, j.since, end)
        self.scan.trigger()
        done = self.scan.step(JOB_END_DIRS, JOB_END_ENTRIES)  # small: the job end must not wait long
        if done and self.scan.result_started_at is not None and self.scan.result_started_at >= end:
            event["leftovers"] = self._public(self.scan.of(j.unit))
            self._resolve_waiting()
            return
        self._waiting.append((j.unit, j.project, key, end))

    def status(self) -> dict:
        shmem, file = self._shares(self.slice)
        return {
            "enabled": self.cfg.enabled,
            "applied": self.applied,
            "memory_max_mb": self.cfg.memory_max_mb,
            "memory_swap_max_mb": self.cfg.memory_swap_max_mb,
            "capped_mb": self.capped_mb,
            "via": self.via,
            "oom_kills": self.oom_kills,
            "max_events": self.max_events,
            "swap_fail_events": self.swap_fail_events,
            "last_event": self.last_event,
            "note": self.note,
            "shmem_mb": shmem,
            "file_mb": file,
            "leak_alert_mb": self.cfg.leak_alert_mb,
            "leak_alerts": self.leak_alerts,
            "projects": self._project_status(),
            "leftovers": self._public(self.scan.leftovers),
        }

    # ---- apply and roll back ---------------------------------------------------------------------

    def _say(self, result: str, reason: str) -> None:
        """Audit and log a state change once (not on every tick)."""
        if reason == self._said:
            return
        self._said = reason
        log.info("backstop: %s", reason)
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": result, "reason": reason, "cgroup": self.slice})  # fmt: skip

    def _read(self, name: str) -> str | None:
        text = self.fs.cgroup(self.slice, name)
        return None if text is None else text.strip()

    def _have(self) -> dict[str, str | None]:
        return {name: self._read(name) for name in LIMIT_FILES}

    def _systemctl(self, *args: str) -> int:
        try:
            rc = self.runner(["systemctl", *args])
        except Exception:  # an injected runner must never stop the tick
            log.exception("backstop: systemctl %s failed", args[0])
            rc = 127
        if rc != 0:
            log.warning("backstop: systemctl %s: exit %s", " ".join(args), rc)
        return rc

    def _load_marker(self) -> dict | None:
        """The marker of an earlier run: {} when there is none, None when it exists but cannot be read or
        parsed (or is not a marker object), so the caller still undoes build-work.slice."""
        try:
            with open(self.marker) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and "slice" in data else None

    @staticmethod
    def _saved_cap(cap) -> int | None:
        return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else None

    def _persist(self) -> str | None:
        """Write the marker (what is to be undone), or remove it when nothing is. The error, or None."""
        try:
            if self._bw_marked or self._marked:
                os.makedirs(os.path.dirname(self.marker), exist_ok=True)
                atomic_write_text(self.marker, json.dumps({
                    "slice": self.slice if self._bw_marked else None, "capped_mb": self.capped_mb,
                    "projects": dict(sorted(self._marked.items())),
                }))  # fmt: skip
            elif os.path.exists(self.marker):
                os.unlink(self.marker)
        except OSError as e:
            return e.strerror or str(e)
        return None

    def _start_slice(self) -> bool:
        """An inactive slice (no cgroup directory, e.g. after boot until its first job): start it, so the
        backstop holds from the boot on and not only from the first job. True when systemctl ran."""
        now = self.clock()
        if self._start_at is not None and now - self._start_at < RETRY_S:
            return False
        self._start_at = now
        rc = self._systemctl("start", self.unit)
        self._say("started" if rc == 0 else "failed",
                  f"systemctl start {self.unit}: " + ("ok" if rc == 0 else f"exit {rc}"))  # fmt: skip
        return True

    def _max_mb(self, have: dict, usage: int | None) -> int:
        """The memory.max to hold now: the target, or a cap above the usage that steps down to it."""
        target = self.cfg.memory_max_mb
        new, self.capped_mb, started = plan_cap(
            target, self.capped_mb, have["memory.max"], usage, self.cfg.cap_margin_mb
        )
        if started:
            msg = (f"backstop capped at {new} MB, above the {target} MB target: usage {-(-usage // MIB)} MB "
                   f"in {self.slice}; lowered as usage falls")  # fmt: skip
            log.warning("%s", msg)
            self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system",
                        "source": "backstop", "result": "capped", "reason": msg,
                        "cgroup": self.slice})  # fmt: skip
            self.emit("backstop_cap", {"message": msg})
        return new

    def _apply(self) -> None:
        have = self._have()
        if None in have.values() and self.runner is not None and self._start_slice():
            have = self._have()
            if None not in have.values():  # one systemctl call per tick: the limits follow on the next one
                self.applied, self.note = False, f"{self.unit} started; the limits follow on the next tick"
                return
        if None in have.values():
            self.applied, self.note = False, f"{self.slice} has no memory limit files"
            self._say("failed", self.note)
            return
        usage = cgroup.read_int(self.fs, self.slice, "memory.current")
        max_mb, swap_mb = self._max_mb(have, usage), self.cfg.memory_swap_max_mb
        want = {"memory.max": str(max_mb * MIB), "memory.swap.max": str(swap_mb * MIB)}
        if have == want and (self._asserted or self.runner is None):
            self._settle(max_mb)
            return
        self.applied = False
        was, self._bw_marked = self._bw_marked, True
        err = self._persist()  # the marker first (like a memory guard pause): a rollback must find the slice
        if err:
            self._bw_marked = was
            self.note = f"refused: cannot persist {self.marker}: {err}"
            self._say("refused", self.note)
            return
        if self.runner is not None:
            self._asserted = True  # once after the start, then only when the files differ
            rc = self._systemctl("set-property", "--runtime", self.unit, f"MemoryMax={max_mb}M",
                                 f"MemorySwapMax={swap_mb}M")  # fmt: skip
            if rc == 0 and self._have() == want:
                self.via, self._fallback = "systemd", None
                self._settle(max_mb)
                return
            why = (
                f"systemctl set-property exit {rc}" if rc else "the files differ after systemctl set-property"
            )
        else:
            why = "no systemctl runner"
        self._fallback = f"{why}: wrote the cgroup files directly (systemd may reset them)"
        for name in LIMIT_FILES:
            if self._read(name) == want[name]:
                continue
            try:
                cgroup.write_memory_limit(self.fs, self.slice, name, int(want[name]))
            except OSError as e:
                self.note = f"cannot write {name}: {e.strerror}"
                self._say("failed", self.note)
                return
        if self._have() != want:
            self.note = f"{self.slice} does not hold the limits after the write"
            self._say("failed", self.note)
            return
        self.via = "file"
        self._settle(max_mb)

    def _settle(self, max_mb: int) -> None:
        """The files hold what is wanted now: the target (applied) or a cap above it (not yet applied)."""
        if self.capped_mb is not None:
            self.applied = False
            self.note = (f"capped at {max_mb} MB above the {self.cfg.memory_max_mb} MB target while usage is "
                         "above it; lowered as usage falls")  # fmt: skip
            return
        self.applied, self.note = True, self._fallback
        self._say("applied", f"memory.max {self.cfg.memory_max_mb} MB, "
                             f"memory.swap.max {self.cfg.memory_swap_max_mb} MB via {self.via}")  # fmt: skip

    def _rollback(self) -> None:
        """The flag is off but an earlier run wrote limits: undo build-work.slice's, then the project
        slices' (bounded per tick), then drop the marker. Undoing is always allowed."""
        self.applied, self.capped_mb, self._asserted = False, None, False
        if self._bw_marked and not self._rollback_slice():
            return
        calls = 0
        for unit in sorted(self._marked):
            if calls >= PROJECT_CALLS_PER_TICK:
                break
            calls += self._unproject(unit)
        if self._marked:
            return
        if err := self._persist():
            self.note = f"rollback done but {self.marker} stays: {err}"

    def _rollback_slice(self) -> bool:
        """Lift build-work.slice's runtime properties and put `max` back (the documented rollback). Until
        every step succeeded it stays in the marker and is retried every RETRY_S: a runtime property left
        behind would be re-applied by systemd at the next re-realization of the slice. True when done."""
        now = self.clock()
        if self._rollback_at is not None and now - self._rollback_at < RETRY_S:
            return False
        failed = []
        if self.runner is not None:
            rc = self._systemctl("set-property", "--runtime", self.unit, "MemoryMax=infinity",
                                 "MemorySwapMax=infinity")  # fmt: skip
            if rc != 0:
                failed.append(f"systemctl set-property {self.unit} MemoryMax=infinity: exit {rc}")
        for name in LIMIT_FILES:  # `max` also when systemctl failed: the files are free at once
            try:
                cgroup.clear_memory_limit(self.fs, self.slice, name)
            except FileNotFoundError:
                continue  # the slice is gone: nothing left to undo
            except OSError as e:
                failed.append(f"{name}: {e.strerror}")
        if failed:
            self._rollback_at = now  # the marker stays: retried after RETRY_S
            self.note = "rollback incomplete: " + "; ".join(failed)
            self._say("failed", self.note)
            return False
        self._rollback_at, self._bw_marked = None, False
        if err := self._persist():
            self.note = f"rollback done but {self.marker} stays: {err}"
            return True
        self.note = None
        self._say("rollback", f"memory.max and memory.swap.max of {self.slice} set back to max")
        return True

    # ---- the slice tree (read-only) --------------------------------------------------------------

    def _read_tree(self) -> None:
        """Every cgroup at and below the slice with whether it has processes of its own (`cgroup.procs`;
        an unreadable one counts as having some). Also notes project slices that hold a job scope."""
        tree: list[tuple[str, bool]] = []
        top = self.slice.strip("/")
        for dirpath, dirnames, _ in os.walk(os.path.join(self.fs.cgroup_root, top)):
            rel = os.path.relpath(dirpath, self.fs.cgroup_root)
            procs = self.fs.cgroup(rel, "cgroup.procs")
            tree.append((rel, procs is None or bool(procs.strip())))
            if rel != top and any(d.endswith(".scope") for d in dirnames):
                self._held_scope.add(rel.rsplit("/", 1)[-1])
        self._tree = tree

    def _slices(self) -> list[str]:
        """Project slice cgroup paths below build-work.slice (nested ones included)."""
        top = self.slice.strip("/")
        return [p for p, _ in self._tree if p != top and p.endswith(".slice")]

    def _shares(self, path: str) -> tuple[int | None, int | None]:
        """(shmem MB, file MB) from memory.stat; (None, None) when unreadable."""
        st = cgroup.read_events(self.fs, path, "memory.stat")
        if st is None:
            return None, None
        return st.get("shmem", 0) // MIB, st.get("file", 0) // MIB

    def _file(self, path: str, name: str) -> str | None:
        text = self.fs.cgroup(path, name)
        return None if text is None else text.strip()

    # ---- per-project caps ------------------------------------------------------------------------

    def _project_name(self, unit: str) -> str:
        for name, _ in self.cfg.projects:
            if slice_name(name) == unit:
                return name
        return unit.removeprefix(self.unit.removesuffix(".slice") + "-").removesuffix(".slice")

    def _under(self, unit: str) -> bool:
        return slice_cgroup(unit).startswith(self.slice.strip("/") + "/")

    def _targets(self) -> dict[str, int]:
        """unit -> cap MB (0 = none) for every configured, scope-holding or earlier capped project slice."""
        out = {}
        for name, override in self.cfg.projects:
            unit = slice_name(name)
            if self._under(unit):
                out[unit] = self.cfg.project_memory_max_mb if override is None else override
        for unit in sorted(self._held_scope | set(self._marked)):
            if unit not in out and self._under(unit):
                out[unit] = self.cfg.project_memory_max_mb
        return out

    def _apply_projects(self) -> None:
        now, calls = self.clock(), 0
        targets = self._targets()
        for unit, cap in sorted(targets.items()):
            if cap <= 0:
                if unit in self._marked and calls < PROJECT_CALLS_PER_TICK:
                    calls += self._unproject(unit)
                continue
            path = slice_cgroup(unit)
            st = self._proj.setdefault(unit, _new_proj(self._marked.get(unit)))
            usage = cgroup.read_int(self.fs, path, "memory.current")
            max_mb, st["capped"], started = plan_cap(
                cap, st["capped"], self._file(path, "memory.max"), usage, self.cfg.cap_margin_mb
            )
            if started:
                self._project_capped(unit, cap, max_mb, usage)
            want = (max_mb, cap * self.cfg.project_memory_high_pct // 100)
            st["applied"] = st["sent"] == want and self._holds(path, want)
            if st["applied"] or calls >= PROJECT_CALLS_PER_TICK:
                continue
            if st["sent"] == want and st["retry_at"] is not None and now - st["retry_at"] < RETRY_S:
                continue  # the same values did not hold a moment ago
            if st["sent"] is None and st["retry_at"] is not None and now - st["retry_at"] < RETRY_S:
                continue  # the last call failed
            if self.runner is None:
                st["retry_at"] = now  # project caps are only set through systemd
                continue
            calls += 1
            self._cap_project(unit, path, st, want)

    def _holds(self, path: str, want: tuple[int, int]) -> bool:
        """The files hold the values, or the slice is inactive (systemd stores the property until it
        starts)."""
        have = (self._file(path, "memory.max"), self._file(path, "memory.high"))
        return have == (None, None) or have == (str(want[0] * MIB), str(want[1] * MIB))

    def _project_capped(self, unit: str, cap: int, max_mb: int, usage: int) -> None:
        name = self._project_name(unit)
        msg = (f"project {name} capped at {max_mb} MB, above its {cap} MB cap: usage {-(-usage // MIB)} MB "
               f"in {unit}; lowered as usage falls")  # fmt: skip
        log.warning("%s", msg)
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": "capped", "reason": msg, "project": name,
                    "cgroup": slice_cgroup(unit)})  # fmt: skip
        self.emit("backstop_cap", {"key": f"backstop_cap:{name}", "message": msg})

    def _cap_project(self, unit: str, path: str, st: dict, want: tuple[int, int]) -> None:
        self._marked[unit] = st["capped"]
        if err := self._persist():  # the marker first: a rollback must find every slice it capped
            st["retry_at"] = self.clock()
            self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system",
                        "source": "backstop", "result": "refused", "cgroup": path,
                        "reason": f"cannot persist {self.marker}: {err}"})  # fmt: skip
            return
        rc = self._systemctl(
            "set-property", "--runtime", unit, f"MemoryMax={want[0]}M", f"MemoryHigh={want[1]}M"
        )
        st["sent"] = want if rc == 0 else None
        ok = rc == 0 and self._holds(path, want)
        st["retry_at"], st["applied"] = (None if ok else self.clock()), ok
        why = "ok" if ok else (f"exit {rc}" if rc else "the files differ afterwards")
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": "applied" if ok else "failed", "project": self._project_name(unit),
                    "reason": f"{unit} MemoryMax={want[0]}M MemoryHigh={want[1]}M: {why}",
                    "cgroup": path})  # fmt: skip

    def _unproject(self, unit: str) -> int:
        """Lift a project slice's cap (MemoryMax=infinity MemoryHigh=infinity, `max` in the files). Retried
        after RETRY_S on failure; the slice stays in the marker until it succeeded. 1 when systemctl ran."""
        now, path = self.clock(), slice_cgroup(unit)
        st = self._proj.setdefault(unit, _new_proj())
        if st["undo_at"] is not None and now - st["undo_at"] < RETRY_S:
            return 0
        failed, ran = [], 0
        if self.runner is not None:
            ran = 1
            rc = self._systemctl(
                "set-property", "--runtime", unit, "MemoryMax=infinity", "MemoryHigh=infinity"
            )
            if rc != 0:
                failed.append(f"systemctl set-property {unit} MemoryMax=infinity: exit {rc}")
        for name in ("memory.max", "memory.high"):  # `max` also when systemctl failed: free at once
            try:
                cgroup.clear_memory_limit(self.fs, path, name)
            except FileNotFoundError:
                continue  # inactive or gone
            except OSError as e:
                failed.append(f"{name}: {e.strerror}")
        if failed:
            st["undo_at"] = now
            self.note = "rollback incomplete: " + "; ".join(failed)
            self.audit({"ts": iso(now), "op": "backstop", "principal": "system", "source": "backstop",
                        "result": "failed", "reason": self.note, "cgroup": path})  # fmt: skip
            return ran
        del self._proj[unit]
        del self._marked[unit]
        self._persist()  # a failure leaves the entry: lifting it again is harmless
        if self.note and self.note.startswith("rollback incomplete") and not self._bw_marked:
            self.note = None
        self.audit({"ts": iso(now), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": "rollback", "project": self._project_name(unit),
                    "reason": f"{unit} MemoryMax and MemoryHigh set back to infinity",
                    "cgroup": path})  # fmt: skip
        return ran

    def _project_status(self) -> dict:
        """Every project slice: its cap (0 = none), MemoryHigh, a cap held above it, usage and the shmem /
        file share (the reads also with the flag off)."""
        out = {}
        targets = self._targets() if self.cfg.enabled else {}
        units = {p.rsplit("/", 1)[-1]: p for p in self._slices()}
        for unit in sorted(set(units) | {u for u, c in targets.items() if c > 0}):
            path = units.get(unit, slice_cgroup(unit))
            cap = targets.get(unit, 0)
            st = self._proj.get(unit, {})
            shmem, file = self._shares(path)
            usage = cgroup.read_int(self.fs, path, "memory.current")
            out[self._project_name(unit)] = {
                "unit": unit, "cap_mb": cap, "high_mb": cap * self.cfg.project_memory_high_pct // 100,
                "capped_mb": st.get("capped") if cap else None,
                "usage_mb": None if usage is None else usage // MIB, "shmem_mb": shmem, "file_mb": file,
                "applied": bool(cap and st.get("applied")),
            }  # fmt: skip
        return out

    def _detect_projects(self) -> None:
        """A rise of a project slice's own `oom_kill` (its count less its child slices'): one alert each."""
        cur = {}
        for path in self._slices():
            ev = cgroup.read_events(self.fs, path)
            if ev is not None:
                cur[path] = ev.get("oom_kill", 0)
        before, self._proj_oom = self._proj_oom, cur
        delta = {p: n - before[p] for p, n in cur.items() if p in before and n >= before[p]}
        for path, d in sorted(delta.items()):
            own = d - sum(v for c, v in delta.items() if c.rsplit("/", 1)[0] == path)
            if own <= 0:
                continue
            unit = path.rsplit("/", 1)[-1]
            name = self._project_name(unit)
            peak = (cgroup.read_int(self.fs, path, "memory.peak") or 0) // MIB
            msg = f"OOM kill in project {name} ({unit}): {own} process(es), memory.peak {peak} MB"
            self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system",
                        "source": "backstop", "result": "event", "project": name, "reason": msg,
                        "cgroup": path})  # fmt: skip
            self.emit("backstop_oom", {"key": f"backstop_oom:{name}", "message": msg, "scope": unit,
                                       "memory_peak_mb": peak})  # fmt: skip

    # ---- leaked memory and leftovers (read-only) -------------------------------------------------

    def _check_leaks(self) -> None:
        """shmem above leak_alert_mb with no process in the subtree for leak_alert_min minutes: one
        `backstop_leak` alert per episode, for the deepest such slice. Only shmem counts: page cache (`file`)
        left after a build is reclaimable, leaked tmpfs pages are not. Reads only; never deletes."""
        now, top = self.clock(), self.slice.strip("/")
        busy = [p for p, has in self._tree if has]
        cands = {}
        for path in ([top] if self._tree else []) + self._slices():
            if any(b == path or b.startswith(path + "/") for b in busy):
                continue
            shmem, file = self._shares(path)
            if (shmem or 0) > self.cfg.leak_alert_mb:
                cands[path] = (shmem, file or 0)
        for path in set(self._leak_since) - set(cands):
            del self._leak_since[path]
            self._leak_said.discard(path)
        for path in cands:
            if path not in self._leak_since:
                self._leak_since[path] = now
                self.scan.trigger()  # by the time the alert is due, the scan has named the leftovers
        for path, (shmem, file) in sorted(cands.items()):
            if path in self._leak_said or now - self._leak_since[path] < self.cfg.leak_alert_min * 60:
                continue
            if any(c.startswith(path + "/") for c in cands):
                continue  # a deeper slice holds it: that one is named
            self._leak_said.add(path)
            self.leak_alerts += 1
            unit = path.rsplit("/", 1)[-1]
            msg = (f"{unit} holds {shmem} MB of shmem (tmpfs; file cache {file} MB) with no process for "
                   f"{int(now - self._leak_since[path]) // 60} min: tmpfs files of an ended job stay charged "
                   "to its cgroup; nothing was deleted")  # fmt: skip
            if tmpfs := self._tmpfs_usage():
                msg += f"; tmpfs {tmpfs}"
            if found := describe(self.scan.leftovers):
                msg += f"; leftovers {found}"
            self.scan.trigger()
            log.warning("backstop: %s", msg)
            self.audit({"ts": iso(now), "op": "backstop", "principal": "system", "source": "backstop",
                        "result": "leak", "reason": msg, "cgroup": path,
                        "leftovers": self._public(self.scan.leftovers)})  # fmt: skip
            self.emit("backstop_leak", {"key": f"backstop_leak:{unit}", "message": msg, "scope": unit})

    def _tmpfs_usage(self) -> str:
        """`/tmp 16900 MB used` for tmpfs mounts with at least leak_alert_mb used (statvfs only)."""
        if not self.scan.enabled:
            return ""
        used = []
        for mp in tmpfs_mounts(self.fs):
            try:
                v = os.statvfs(self.scan.real(mp))
            except OSError:
                continue
            mb = (v.f_blocks - v.f_bfree) * v.f_frsize // MIB
            if mb >= self.cfg.leak_alert_mb:
                used.append((mb, mp))
        return ", ".join(f"{mp} {mb} MB used" for mb, mp in sorted(used, reverse=True)[:3])

    @staticmethod
    def _public(found: list[dict]) -> list[dict]:
        return [{"path": f["path"], "mb": f["mb"], "candidates": f["candidates"]} for f in found]

    def _resolve_waiting(self) -> None:
        """Job ends whose scan finished now: one `job_leftovers` audit line each."""
        started, keep = self.scan.result_started_at, []
        for unit, project, key, end in self._waiting:
            if started is None or started < end:
                keep.append((unit, project, key, end))
                continue
            self.audit({"ts": iso(self.clock()), "op": "job_leftovers", "principal": "system",
                        "source": "backstop", "result": "scanned", "project": project,
                        "request": {"unit": unit, "key": key},
                        "leftovers": self._public(self.scan.of(unit))})  # fmt: skip
        self._waiting = keep

    # ---- memory.oom.group ------------------------------------------------------------------------

    def _group(self, unit: str, job_cgroup: str) -> None:
        if unit in self._grouped:
            return
        self._grouped.add(unit)
        try:
            cgroup.write_oom_group(self.fs, job_cgroup)
        except FileNotFoundError:
            pass  # the scope is already gone
        except OSError as e:
            log.warning("backstop: cannot set memory.oom.group on %s: %s", unit, e.strerror)

    def _group_all(self) -> None:
        jobs = self._jobs()
        self._grouped &= set(jobs)
        for unit, j in jobs.items():
            self._group(unit, j.cgroup)

    # ---- detection -------------------------------------------------------------------------------

    @staticmethod
    def _delta(cur: int | None, base: int | None) -> tuple[int, int | None]:
        if cur is None:
            return 0, base
        if base is None or cur < base:
            return 0, cur
        return cur - base, cur

    def _detect(self) -> None:
        live: dict[str, tuple[int, int]] = {}
        for unit, j in self._jobs().items():
            peak = cgroup.read_int(self.fs, j.cgroup, "memory.peak")
            own = (cgroup.read_events(self.fs, j.cgroup) or {}).get("oom_kill", 0)
            live[unit] = ((peak or 0) // MIB, own)
        before, self._seen = self._seen, live
        sev = cgroup.read_events(self.fs, self.slice, "memory.swap.events")
        d_swap, self._swap_fail_base = self._delta(
            None if sev is None else sev.get("fail", 0), self._swap_fail_base
        )
        self.swap_fail_events += d_swap
        ev = cgroup.read_events(self.fs, self.slice)
        if ev is None:
            return
        d_max, self._max_base = self._delta(ev.get("max", 0), self._max_base)
        self.max_events += d_max
        now = (ev.get("oom_kill", 0), ev.get("oom_group_kill", 0))
        prev, self._base = self._base, now
        if prev is None or now[0] < prev[0] or now[1] < prev[1]:
            return  # first reading, or the counters restarted
        kills = now[0] - prev[0]
        if kills <= 0 and now[1] == prev[1]:
            return
        self.oom_kills += max(kills, 1)
        grew = [u for u, (_, own) in live.items() if u in before and own > before[u][1]]
        names = grew or [u for u in before if u not in live]  # a killed scope is usually gone by now
        scope = ", ".join(names[:3]) if names else "unknown (no scope found)"
        slice_peak = (cgroup.read_int(self.fs, self.slice, "memory.peak") or 0) // MIB
        # a scope that is still there reports its own memory.peak; a killed one is gone, and its high-water
        # mark lives on in the slice's memory.peak (the scope's last reading is up to one tick old)
        peaks = [live[u][0] if u in live else slice_peak for u in names]
        peak = max(peaks) if peaks else slice_peak
        msg = f"OOM kill in {self.slice}: {max(kills, 1)} process(es), scope {scope}, memory.peak {peak} MB"
        self.last_event = f"{iso(self.clock())} {scope} peak {peak} MB"
        self.audit({"ts": iso(self.clock()), "op": "backstop", "principal": "system", "source": "backstop",
                    "result": "event", "reason": msg, "cgroup": self.slice})  # fmt: skip
        self.emit("backstop", {"message": msg, "scope": scope, "memory_peak_mb": peak})
