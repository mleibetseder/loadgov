"""Big directories left on tmpfs mounts, named by the jobs that likely wrote them. Read-only, best effort.

Pages a job writes to a tmpfs (`/tmp` on many hosts) stay charged to the job's cgroup as `shmem` after the
job ends, and a job killed by SIGKILL never runs its own cleanup. This scanner only reports: it lists the top
level of every tmpfs mount, sizes each directory there with `lstat` (never reads a file, never follows a
symlink, never crosses into another filesystem: an entry that is a mount point in the scan's one reading of
`/proc/self/mounts` is skipped before any stat, so a stuck FUSE or NFS mount cannot block the tick, and an
entry on another `st_dev` is skipped too), at most
`MAX_ENTRIES` entries and `MAX_DEPTH` levels per directory. A daemon tick opens at most `DIRS_PER_STEP`
directories and looks at most at `ENTRIES_PER_STEP` entries; the synchronous step at a job end at most
`JOB_END_DIRS` / `JOB_END_ENTRIES`; any step lists at most `LIST_PER_MOUNT` top-level entries of a mount.
Every bound stops at an entry and the next step resumes there (the open listing is kept). A directory
of at least `report_mb` is attributed to every job whose
[start, end] window holds its ctime or mtime (several = all listed as candidates). Nothing is ever deleted.

Mount points come from `<proc_root>/self/mounts`. Paths are opened below `mount_root` (empty on the real
host); with an injected `/proc` or cgroup root and no `mount_root` the scanner is off, so no test walks a
host directory.
"""

from __future__ import annotations

import os
import stat
from collections import deque
from collections.abc import Callable

from loadgov.host.procfs import HostFS

MIB = 1024 * 1024
HOST_CGROUP = "/sys/fs/cgroup"
MAX_ENTRIES = 20000  # per top-level directory; a larger one is reported with what was counted (`partial`)
MAX_DEPTH = 4  # the top-level directory is depth 1
DIRS_PER_STEP = 64  # directories opened per daemon tick
ENTRIES_PER_STEP = 20000  # entries looked at per daemon tick
JOB_END_DIRS = 8  # the synchronous step at a job end is much smaller; the rest goes on in the ticks
JOB_END_ENTRIES = 2000
LIST_PER_MOUNT = 256  # top-level entries of one mount listed per step; the listing resumes on the next
RECENT_JOBS = 64  # ended jobs kept as candidates
RECENT_S = 24 * 3600.0


def _unescape(field: str) -> str:
    """`/proc/self/mounts` escapes space, tab, newline and backslash as octal (`\\040`)."""
    out, i = [], 0
    while i < len(field):
        code = field[i + 1 : i + 4]
        if field[i] == "\\" and len(code) == 3 and code.isdigit():
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def read_mounts(fs: HostFS) -> list[tuple[str, str]]:
    """(mount point, fstype) of every mount, from `/proc/self/mounts` ([] when unreadable)."""
    try:
        text = fs.proc("self/mounts")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("/"):
            out.append((_unescape(parts[1]), parts[2]))
    return out


def tmpfs_mounts(fs: HostFS, mounts: list[tuple[str, str]] | None = None) -> list[str]:
    """Mount points of every tmpfs (in mount order, each once)."""
    out = []
    for mp, fstype in read_mounts(fs) if mounts is None else mounts:
        if fstype == "tmpfs" and mp not in out:
            out.append(mp)
    return out


class LeftoverScan:
    def __init__(self, fs: HostFS, clock: Callable[[], float], report_mb: int, mount_root: str | None = None):
        self.fs, self.clock, self.report_mb = fs, clock, report_mb
        if mount_root is None:  # fail closed: any injected root (every unit test) turns the walk off
            real = (
                os.path.realpath(fs.proc_root) == "/proc" and os.path.realpath(fs.cgroup_root) == HOST_CGROUP
            )
            mount_root = "" if real else None
        self.mount_root = mount_root
        self.recent: deque[dict] = deque()  # ended jobs: {unit, project, key, start, end}
        self.running: Callable[[], list[dict]] = list  # running jobs as {unit, project, key, start}
        self.leftovers: list[dict] = []  # result of the last finished scan
        self.finished_at: float | None = None
        self.started_at: float | None = None  # of the running (or last) scan
        self.result_started_at: float | None = None  # start of the scan that produced `leftovers`
        self._queue: deque[tuple[str, str, int]] = deque()  # (reported path, real path, st_dev) to size
        self._cur: dict | None = None  # the directory being sized (with its open listing, if any)
        self._mounts: deque[str] = deque()  # tmpfs mount points still to list
        self._listing: tuple | None = None  # (mount point, st_dev, open scandir) of the mount being listed
        self._mtab: set[str] = set()  # every mount point of the scan's /proc/self/mounts reading
        self._found: list[dict] = []
        self._active = False
        self._again = False  # a trigger came while scanning: one more scan after this one

    @property
    def enabled(self) -> bool:
        return self.mount_root is not None

    def real(self, path: str) -> str:
        """Where a mount point is opened (below `mount_root` in tests)."""
        return self.mount_root + path if self.mount_root else path

    def note_end(self, unit: str, project: str, key: str | None, start: float, end: float) -> None:
        self.recent.append({"unit": unit, "project": project, "key": key, "start": start, "end": end})
        while self.recent and (len(self.recent) > RECENT_JOBS or end - self.recent[0]["end"] > RECENT_S):
            self.recent.popleft()

    def trigger(self) -> None:
        """Start a scan, or queue one more after the running one (it may have listed a mount too early)."""
        if not self.enabled:
            return
        if self._active:
            self._again = True
            return
        self._close()
        self._active, self._found, self._cur = True, [], None
        self.started_at = self.clock()
        self._queue.clear()
        mounts = read_mounts(self.fs)  # once per scan
        # every mount point: never stat'ed or opened from a walk (a stuck FUSE or NFS mount would block)
        self._mtab = {mp for mp, _ in mounts}
        self._mounts = deque(tmpfs_mounts(self.fs, mounts))  # listed in `step`, LIST_PER_MOUNT at a time

    def _is_mount(self, real_path: str) -> bool:
        host = real_path[len(self.mount_root) :] if self.mount_root else real_path
        return host in self._mtab

    def _close(self) -> None:
        for it in (self._listing[2] if self._listing else None, (self._cur or {}).get("it")):
            if it is not None:
                it.close()
        self._listing = None
        if self._cur:
            self._cur["it"] = None

    def step(self, dirs: int | None = None, entries: int | None = None) -> bool:
        """At most `dirs` directories opened (DIRS_PER_STEP) and `entries` entries looked at
        (ENTRIES_PER_STEP), plus at most LIST_PER_MOUNT top-level entries per mount; the rest is resumed on
        the next call. True when a scan finished in this call."""
        if not self._active:
            return False
        budget = {"dirs": DIRS_PER_STEP if dirs is None else dirs,
                  "entries": ENTRIES_PER_STEP if entries is None else entries}  # fmt: skip
        while True:
            if self._cur is None and not self._queue:
                if self._mounts or self._listing:
                    if not self._list(budget):
                        return False  # a mount's listing goes on in the next call
                    continue
                self._finish()
                return True
            if self._cur is None:
                path, real, dev = self._queue.popleft()
                try:
                    st = os.lstat(real)
                except OSError:
                    continue
                self._cur = {"path": path, "dev": dev, "bytes": st.st_blocks * 512, "entries": 0,
                             "partial": False, "times": (st.st_mtime, st.st_ctime),
                             "stack": [(real, 1)], "it": None, "depth": 1}  # fmt: skip
            if not self._walk(self._cur, budget):
                return False
            self._done_dir(self._cur)
            self._cur = None

    def _list(self, budget: dict) -> bool:
        """List the top level of the mounts, LIST_PER_MOUNT entries per mount and call. True when a mount
        finished in this call (the caller goes on with its directories), False when the bound was hit."""
        if self._listing is None:
            if budget["dirs"] <= 0:
                return False  # opening a mount's listing counts as a directory
            budget["dirs"] -= 1
            mp = self._mounts.popleft()
            real = self.real(mp)  # a tmpfs root: memory-backed, never blocks
            try:
                dev = os.lstat(real).st_dev
                self._listing = (mp, dev, os.scandir(real))
            except OSError:
                return True
        mp, dev, it = self._listing
        for _ in range(LIST_PER_MOUNT):
            if budget["entries"] <= 0:
                return False
            try:
                e = next(it, None)
            except OSError:
                e = None
            if e is None:
                it.close()
                self._listing = None
                return True
            budget["entries"] -= 1
            if self._is_mount(e.path):
                continue  # another mount (FUSE, NFS, a nested tmpfs listed on its own): never stat'ed
            try:
                if e.is_dir(follow_symlinks=False) and e.stat(follow_symlinks=False).st_dev == dev:
                    self._queue.append((os.path.join(mp, e.name), e.path, dev))
            except OSError:
                continue
        return False  # LIST_PER_MOUNT reached: resumed in the next call

    def _walk(self, cur: dict, budget: dict) -> bool:
        """Size `cur` within the budget, resumable at any entry. True when the directory is done."""
        while True:
            if cur["it"] is None:
                if not cur["stack"]:
                    return True
                if budget["dirs"] <= 0:
                    return False
                real, cur["depth"] = cur["stack"].pop()
                budget["dirs"] -= 1
                try:
                    cur["it"] = os.scandir(real)
                except OSError:
                    continue
            if budget["entries"] <= 0:
                return False
            if cur["entries"] >= MAX_ENTRIES:
                cur["it"].close()
                cur["partial"], cur["stack"], cur["it"] = True, [], None
                return True
            try:
                e = next(cur["it"], None)
            except OSError:
                e = None
            if e is None:
                cur["it"].close()
                cur["it"] = None
                continue
            cur["entries"] += 1
            budget["entries"] -= 1
            if self._is_mount(e.path):
                continue  # a mount point below: never stat'ed or opened (a stuck mount would block)
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if st.st_dev != cur["dev"]:
                continue  # another filesystem mounted below: never crossed
            cur["bytes"] += st.st_blocks * 512
            if stat.S_ISDIR(st.st_mode):
                if cur["depth"] < MAX_DEPTH:
                    cur["stack"].append((e.path, cur["depth"] + 1))
                else:
                    cur["partial"] = True

    def _done_dir(self, cur: dict) -> None:
        mb = cur["bytes"] // MIB
        if mb >= self.report_mb:
            self._found.append(
                {"path": cur["path"], "mb": mb, "partial": cur["partial"], "times": cur["times"]}
            )

    def _finish(self) -> None:
        now = self.clock()
        jobs = list(self.recent) + [{**j, "end": now} for j in self.running()]
        out = []
        for f in sorted(self._found, key=lambda f: -f["mb"])[:10]:
            cands = [
                {"unit": j["unit"], "project": j["project"], "key": j["key"]}
                for j in jobs
                if any(j["start"] <= t <= j["end"] for t in f["times"])
            ]
            out.append({"path": f["path"], "mb": f["mb"], "partial": f["partial"], "candidates": cands})
        self.leftovers, self.finished_at, self.result_started_at = out, now, self.started_at
        self._active, self._found = False, []
        if self._again:
            self._again = False
            self.trigger()

    def of(self, unit: str) -> list[dict]:
        """The leftovers of the last scan that name `unit` among their candidates."""
        return [f for f in self.leftovers if any(c["unit"] == unit for c in f["candidates"])]


def describe(leftovers: list[dict]) -> str:
    """`/tmp/x 20480 MB (unit, ...)` for an alert text; '' when there is nothing."""
    parts = []
    for f in leftovers[:3]:
        who = ", ".join(c["unit"] for c in f["candidates"][:3]) or "no matching job"
        parts.append(f"{f['path']} {'>= ' if f['partial'] else ''}{f['mb']} MB ({who})")
    return "; ".join(parts)
