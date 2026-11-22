"""The image train: beta and release container builds queue during the day and run in a night window.

Pure bookkeeping (injectable clock, no I/O, nothing is started here): the daemon only arbitrates, a client
(`loadctl image runner`) asks for the next runnable job and runs it like `loadctl run --kind image`.

Rules (from `[image]`): one job at a time in submission order; a job starts only inside the window unless
the owner released it with `now`; no new start after the window end (a job that runs over the end runs
to its end); a project's beta starts at most every `beta_interval_h` hours, measured from the start of
its last beta job; at most `max_per_night` starts per window. Window times are wall-clock times of
`timezone`, so DST changes the length of a window, not its clock times.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from zoneinfo import ZoneInfo

from loadgov.config import ImageConfig
from loadgov.engines.limits import iso
from loadgov.host import proctree
from loadgov.keys import key_url

CHANNELS = ("beta", "release")
MAX_QUEUE = 50  # queued plus running jobs; a submission beyond it is refused
VIEW_MAX = 20  # entries of the status `image.queue`
LOOKAHEAD_DAYS = 14  # window instances considered when planning
KEEP_STARTS_S = 3 * 86400


# ---- window math ----------------------------------------------------------------------------------


def _minutes(hm: str) -> tuple[int, int]:
    h, m = hm.split(":")
    return int(h), int(m)


def instances(
    cfg: ImageConfig, now: float, back: int = 1, ahead: int = LOOKAHEAD_DAYS
) -> Iterator[tuple[float, float]]:
    """(start, end) epoch seconds of each window instance from `back` days before to `ahead` days after the
    local date of `now`, in order. A window whose end is not after its start crosses midnight."""
    tz = ZoneInfo(cfg.timezone)
    a, b = cfg.window.split("-")
    (sh, sm), (eh, em) = _minutes(a), _minutes(b)
    over = (eh, em) <= (sh, sm)
    today = dt.datetime.fromtimestamp(now, tz).date()
    for i in range(-back, ahead + 1):
        day = today + dt.timedelta(days=i)
        s = dt.datetime.combine(day, dt.time(sh, sm), tz).timestamp()
        e = dt.datetime.combine(day + dt.timedelta(days=1 if over else 0), dt.time(eh, em), tz).timestamp()
        if e > s:
            yield s, e


def window_state(cfg: ImageConfig, now: float) -> tuple[bool, float | None, float | None, float | None]:
    """(in_window, current start, current end, next start strictly after now)."""
    cur = nxt = None
    for s, e in instances(cfg, now):
        if s <= now < e:
            cur = (s, e)
        elif s > now and nxt is None:
            nxt = s
    return cur is not None, cur[0] if cur else None, cur[1] if cur else None, nxt


def first_open_at_or_after(cfg: ImageConfig, t: float) -> float | None:
    """`t` when the window is open then, else the next window start."""
    for s, e in instances(cfg, t):
        if e > t:
            return max(s, t)
    return None


# ---- the queue ------------------------------------------------------------------------------------


@dataclass
class ImageJob:
    id: int
    project: str
    label: str
    channel: str  # beta | release
    key: str | None
    argv: list[str]
    cwd: str
    env: dict[str, str]  # only the variables of [image] env_allow
    submitted: float
    submitter: dict  # pid, cgroup, cmdline, principal: audit evidence
    state: str = "queued"  # queued | running
    now_ok: bool = False  # the owner released it outside the window
    now_reason: str | None = None
    runner_pid: int | None = None
    started: float | None = None
    prev_beta: float | None = None  # last_beta[project] before this job started (restored when it never ran)


Expected = Callable[[str], float | None]  # project -> median duration (s) of finished image jobs


class ImageTrain:
    def __init__(self, clock: Callable[[], float]):
        self.clock = clock
        self.jobs: list[ImageJob] = []
        self.next_id = 1
        self.last_beta: dict[str, float] = {}  # project -> start of its last beta job
        self.starts: list[float] = []  # starts inside a window (for max_per_night)

    # ---- lookups ---------------------------------------------------------------------------------

    def running(self) -> ImageJob | None:
        return next((j for j in self.jobs if j.state == "running"), None)

    def queued(self) -> list[ImageJob]:
        return [j for j in self.jobs if j.state == "queued"]

    def get(self, job_id: int) -> ImageJob | None:
        return next((j for j in self.jobs if j.id == job_id), None)

    def beta_block(self, cfg: ImageConfig, project: str, now: float) -> float | None:
        """When the project may start its next beta, None when it may now."""
        last = self.last_beta.get(project)
        until = None if last is None else last + cfg.beta_interval_h * 3600
        return until if until is not None and now < until else None

    def _night_count(self, s: float, e: float) -> int:
        return sum(1 for t in self.starts if s <= t < e)

    # ---- changes ---------------------------------------------------------------------------------

    def submit(self, cfg: ImageConfig, now: float, data: dict, waive_interval: bool = False):
        """(job, None) or (None, (error, message, extra)). `data`: project, label, channel, key, argv, cwd,
        env, submitter."""
        if len(self.jobs) >= MAX_QUEUE:
            return None, ("refused", f"the image queue is full ({MAX_QUEUE} jobs)", {})
        if data["channel"] == "beta" and not waive_interval:
            project = data["project"]
            until = self.beta_block(cfg, project, now)
            if until is not None:
                nxt = first_open_at_or_after(cfg, until)
                return None, ("refused", f"{project} started a beta at {iso(self.last_beta[project])}; "
                              f"the interval is {cfg.beta_interval_h} h, next allowed {iso(nxt)}",
                              {"next_allowed": iso(nxt)})  # fmt: skip
            queued = next((j for j in self.jobs if j.project == project and j.channel == "beta"), None)
            if queued:
                return None, ("refused", f"a beta of {project} is already queued (id {queued.id}); "
                              "at most one per interval", {"queued_id": queued.id})  # fmt: skip
        job = ImageJob(id=self.next_id, submitted=now, **data)
        self.next_id += 1
        self.jobs.append(job)
        return job, None

    def cancel(self, job_id: int) -> ImageJob | None:
        j = self.get(job_id)
        if j and j.state == "queued":
            self.jobs.remove(j)
            return j
        return None

    def release_now(self, job_id: int, reason: str) -> ImageJob | None:
        j = self.get(job_id)
        if j and j.state == "queued":
            j.now_ok, j.now_reason = True, reason
            return j
        return None

    def runnable(self, j: ImageJob, cfg: ImageConfig, now: float, hold: bool) -> bool:
        if hold or self.running() or j.state != "queued":
            return False
        if j.now_ok:
            return True
        in_win, s, e, _ = window_state(cfg, now)
        if not in_win or self._night_count(s, e) >= cfg.max_per_night:
            return False
        return not (j.channel == "beta" and self.beta_block(cfg, j.project, now) is not None)

    def claim(self, cfg: ImageConfig, now: float, hold: bool, runner_pid: int | None):
        """(job, start_by): the first runnable job in submission order, marked running; (None, None) if none.
        `start_by` is the window end for a job outside owner `now` (the runner must not start it later)."""
        self.starts = [t for t in self.starts if now - t < KEEP_STARTS_S]
        for j in self.queued():
            if not self.runnable(j, cfg, now, hold):
                continue
            j.state, j.started, j.runner_pid = "running", now, runner_pid
            j.prev_beta = self.last_beta.get(j.project)
            if j.channel == "beta":
                self.last_beta[j.project] = now
            start_by = None
            if not j.now_ok:
                self.starts.append(now)
                start_by = window_state(cfg, now)[2]
            return j, start_by
        return None, None

    def finish(self, j: ImageJob, started: bool) -> None:
        """The runner reports back. A job that never started (the window closed while it waited for the slot)
        goes back to the queue and its start no longer counts."""
        if started:
            self.jobs.remove(j)
            return
        if j.started in self.starts:
            self.starts.remove(j.started)
        if j.channel == "beta":
            if j.prev_beta is None:
                self.last_beta.pop(j.project, None)
            else:
                self.last_beta[j.project] = j.prev_beta
        j.state, j.started, j.runner_pid = "queued", None, None

    def reap(self, alive: Callable[[int], bool]) -> ImageJob | None:
        """Drop a running job whose runner process is gone (it is not run again: it may be half done)."""
        j = self.running()
        if j and j.runner_pid is not None and not alive(j.runner_pid):
            self.jobs.remove(j)
            return j
        return None

    # ---- planning --------------------------------------------------------------------------------

    def plan(self, cfg: ImageConfig, now: float, expected: Expected, hold: bool) -> dict[int, dict]:
        """job id -> {planned_start, planned_finish, state}, chained from the window open (or now inside it)
        with the median duration of the project's finished image jobs. An unknown duration makes every
        start behind it unknown; a hold makes all of them unknown."""
        out: dict[int, dict] = {}
        run, free, known = self.running(), now, True
        if run:
            e = expected(run.project)
            fin = None if e is None else max(run.started + e, now)
            out[run.id] = {"planned_start": run.started, "planned_finish": fin, "state": "running"}
            free, known = fin, fin is not None
        in_win, cur_s, _, nxt = window_state(cfg, now)
        first = cur_s if in_win else nxt
        wins = list(instances(cfg, now))
        used = {s: self._night_count(s, e) for s, e in wins}
        last_beta = dict(self.last_beta)
        for j in self.queued():
            start = None
            fit_s = first
            if known and not hold:
                if j.now_ok:
                    start = free
                else:
                    start, fit_s = self._fit(cfg, wins, used, last_beta, j, free)
                    if start is not None and j.channel == "beta":
                        last_beta[j.project] = start
            e = expected(j.project)
            fin = None if start is None or e is None else start + e
            if j.now_ok and start is not None:
                state = "now"
            elif start is not None and fit_s != first:
                state = "deferred"
            else:
                state = "queued"
            out[j.id] = {"planned_start": start, "planned_finish": fin, "state": state}
            free, known = fin, known and not hold and fin is not None
        return out

    @staticmethod
    def _fit(cfg, wins, used, last_beta, j, free):
        """(start, window start) of the earliest window instance that can take `j` at or after `free`."""
        for s, e in wins:
            if e <= free or used.get(s, 0) >= cfg.max_per_night:
                continue
            begin = max(s, free)
            if j.channel == "beta" and j.project in last_beta:
                begin = max(begin, last_beta[j.project] + cfg.beta_interval_h * 3600)
            if begin < e:
                used[s] = used.get(s, 0) + 1
                return begin, s
        return None, None

    def view(self, cfg: ImageConfig, now: float, expected: Expected, hold: bool, url_template: str = "",
             limit: int = VIEW_MAX) -> list[dict]:  # fmt: skip
        plan = self.plan(cfg, now, expected, hold)
        rows = []
        for pos, j in enumerate(sorted(self.jobs, key=lambda j: (j.state != "running", j.id)), 1):
            p = plan[j.id]
            rows.append({
                "id": j.id, "position": pos, "project": j.project, "label": j.label, "key": j.key,
                "url": key_url(url_template, j.key), "channel": j.channel, "submitted": iso(j.submitted),
                "planned_start": iso(p["planned_start"]), "planned_finish": iso(p["planned_finish"]),
                "state": p["state"],
            })  # fmt: skip
        return rows[:limit]

    def status(self, cfg: ImageConfig, now: float, expected: Expected, hold: bool, url_template: str = "",
               limit: int = VIEW_MAX) -> dict:  # fmt: skip
        in_win, _, _, nxt = window_state(cfg, now)
        return {
            "window": cfg.window, "timezone": cfg.timezone, "in_window": in_win,
            "next_window_start": iso(nxt),
            "max_runtime_min": cfg.max_runtime_min, "beta_interval_h": cfg.beta_interval_h,
            "max_per_night": cfg.max_per_night, "env_allow": list(cfg.env_allow),
            "queued": len(self.queued()), "running": 1 if self.running() else 0,
            "queue": self.view(cfg, now, expected, hold, url_template, limit),
        }  # fmt: skip

    # ---- persistence -----------------------------------------------------------------------------

    def to_state(self) -> dict:
        return {"next_id": self.next_id, "jobs": [asdict(j) for j in self.jobs],
                "last_beta": self.last_beta, "starts": self.starts}  # fmt: skip

    def restore(self, raw) -> None:
        if not isinstance(raw, dict):
            return
        for item in raw.get("jobs") or []:
            try:
                self.jobs.append(ImageJob(**item))
            except (TypeError, ValueError):
                continue  # an unreadable entry is dropped, the rest is kept
        try:
            self.next_id = max(int(raw.get("next_id", 1)), 1 + max((j.id for j in self.jobs), default=0))
            self.last_beta = {str(k): float(v) for k, v in (raw.get("last_beta") or {}).items()}
            self.starts = [float(t) for t in raw.get("starts") or []]
        except (TypeError, ValueError, AttributeError):
            pass


# ---- out-of-window detection (observe only) ------------------------------------------------------

BUILD_COMMS = ("docker", "docker-compose", "docker-buildx", "buildctl")
BUILD_WORDS = ("build", "bake")


def find_builds(fs, proc_ticks: dict, image_cgroups: list[str]) -> list[dict]:
    """Container image builds running now that do not belong to a running image job.

    A heuristic, on purpose small: a client process (`docker build`, `docker buildx build|bake`,
    `docker compose build`, `buildctl build`) whose cgroup is not inside the scope of a running image job.
    The BuildKit daemon itself is not looked at: an idle one is no build activity. Only the process name
    and the sub-command are reported, never the arguments (build arguments can carry secrets)."""
    out = []
    for pid, (_start, _ticks, comm, _rss) in proc_ticks.items():
        if comm not in BUILD_COMMS:
            continue
        args = proctree.cmdline(fs, pid)
        if not args:
            continue
        words = [a for a in args[1:6] if not a.startswith("-")]
        if not words or words[0] in ("run", "exec") or not any(w in BUILD_WORDS for w in words[:3]):
            continue
        cg = proctree.cgroup_of(fs, pid) or ""
        if any(cg == c or cg.startswith(c.rstrip("/") + "/") for c in image_cgroups):
            continue
        what = " ".join(
            [os.path.basename(args[0]), *[w for w in words[:3] if w in BUILD_WORDS or w == "buildx"]]
        )
        out.append({"pid": pid, "what": what, "cgroup": cg})
    return sorted(out, key=lambda b: b["pid"])
