"""Run records: runs -> jobs -> steps, stored by the client (never by the daemon).

Layout under the runs directory (`default_runs_dir()`):

    <run id>/run.json              the run: workflow, event, project, host id, timestamps
    <run id>/jobs/<job key>.json   one job instance with its steps (written by the runner and the job script)
    <run id>/logs/<job key>-<n>.log  one log per step, capped at STEP_LOG_MAX bytes

`load_run()` joins them and derives the run status from the jobs. The JSON shape is documented in
docs/workflows.md and versioned by `schema`.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import secrets
import shutil

SCHEMA = 1
KEEP_RUNS = 200
STALE_ACTIVE_DAYS = 30  # a run that never finished stops being protected after this
ACTIVE = ("queued", "waiting for slot", "running")
STEP_LOG_MAX = 5 * 1024 * 1024
TRUNCATED = "\n[loadgov: log truncated at {n} bytes]\n"
STATUSES = ("queued", "waiting for slot", "running", "success", "failure", "cancelled", "skipped")
FINAL = ("success", "failure", "cancelled", "skipped")
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")


def now_iso(t: float | None = None) -> str:
    d = (
        datetime.datetime.fromtimestamp(t, datetime.UTC)
        if t is not None
        else datetime.datetime.now(datetime.UTC)
    )
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def default_runs_dir(env: dict | None = None, euid: int | None = None) -> str:
    """$LOADGOV_RUNS_DIR; else /var/lib/loadgov/runs for root; else <state home>/loadgov/runs."""
    env = os.environ if env is None else env
    if env.get("LOADGOV_RUNS_DIR"):
        return env["LOADGOV_RUNS_DIR"]
    if (os.geteuid() if euid is None else euid) == 0:
        return "/var/lib/loadgov/runs"
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "loadgov", "runs")


def write_json(path: str, data) -> None:
    """Atomic: readers see the old or the new file, never half of one."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class RunRecordError(ValueError):
    """A damaged run record: the message is one line and names the file (and the field, where it applies)."""


def _one_line(e: Exception) -> str:
    return " ".join(str(e).split())[:200]


def read_record(path: str):
    """Like `read_json`, but every way a record can be damaged (unreadable, not UTF-8, truncated, nested too
    deep) becomes a RunRecordError naming the file."""
    try:
        return read_json(path)
    except OSError as e:
        raise RunRecordError(f"{path}: unreadable ({e.strerror or type(e).__name__})") from None
    except RecursionError:
        raise RunRecordError(f"{path}: nested too deeply") from None
    except UnicodeDecodeError:
        raise RunRecordError(f"{path}: not valid UTF-8") from None
    except ValueError as e:
        raise RunRecordError(f"{path}: not valid JSON ({_one_line(e)})") from None


def _number(v) -> bool:
    # A finite number of sane size: JSON NaN/Infinity, 1e400 or a 400-digit integer would crash the readers
    # (`runs show` formats durations with float arithmetic).
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) < 2**63
    return isinstance(v, float) and math.isfinite(v)


# Fields the loader and the readers (`loadctl runs`, `runs show`, parity) use: name, type check, mode.
# "req" = present and not null; "null" = key present, value may be null; "opt" = may be absent or null;
# "set" = may be absent, but not null when present.
_STR = (lambda v: isinstance(v, str), "a string")
_INT = (lambda v: isinstance(v, int) and not isinstance(v, bool), "an integer")
_NUM = (_number, "a number")
_RUN_FIELDS = (
    ("id", _STR, "req"),
    ("event", _STR, "req"),
    ("project", _STR, "req"),
    ("host_id", _STR, "req"),
    ("created_at", _STR, "req"),
    ("finished_at", _STR, "null"),
    ("key", _STR, "opt"),
    ("sha", _STR, "opt"),
    ("ref", _STR, "opt"),
)
_WORKFLOW_FIELDS = (("name", _STR, "req"), ("file", _STR, "req"), ("stem", _STR, "req"))
_JOB_FIELDS = (
    ("key", _STR, "req"),
    ("id", _STR, "null"),
    ("lane", _STR, "req"),
    ("status", _STR, "req"),
    ("reason", _STR, "opt"),
    ("order", _NUM, "set"),
    ("exit_code", _INT, "null"),
    ("duration_s", _NUM, "null"),
)
_STEP_FIELDS = (
    ("index", _INT, "req"),
    ("id", _STR, "opt"),
    ("name", _STR, "req"),
    ("status", _STR, "req"),
    ("exit_code", _INT, "null"),
    ("duration_s", _NUM, "null"),
    ("log", _STR, "req"),
)


def _object(v, path: str, at: str, fields) -> dict:
    if not isinstance(v, dict):
        raise RunRecordError(f"{path}: {at or 'the record'} must be an object")
    for name, (ok, what), mode in fields:
        where = f"{at}.{name}" if at else name
        if name not in v:
            if mode not in ("opt", "set"):
                raise RunRecordError(f"{path}: {where} is missing")
        elif v[name] is None:
            if mode in ("req", "set"):
                raise RunRecordError(f"{path}: {where} must not be null")
        elif not ok(v[name]):
            raise RunRecordError(f"{path}: {where} must be {what}")
    return v


def check_run_file(run, path: str) -> dict:
    run = _object(run, path, "", _RUN_FIELDS)
    _object(run.get("workflow"), path, "workflow", _WORKFLOW_FIELDS)
    home = os.path.basename(os.path.dirname(path))
    if run["id"] != home:  # the list links by id: a row whose id is not its directory would answer 404
        raise RunRecordError(f"{path}: id {run['id'][:40]!r} does not match its directory {home!r}")
    return run


def check_job_file(job, path: str) -> dict:
    job = _object(job, path, "", _JOB_FIELDS)
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise RunRecordError(f"{path}: steps must be a list")
    for k, s in enumerate(steps):
        _object(s, path, f"steps[{k}]", _STEP_FIELDS)
    return job


def derive_status(jobs: list[dict]) -> str:
    """The status of a run from its jobs (a failed job with continue_on_error does not fail the run)."""
    st = [j["status"] for j in jobs]
    if not st:
        return "queued"
    for s in ("running", "waiting for slot", "queued"):
        if s in st:
            return s
    if any(j["status"] == "failure" and not j.get("continue_on_error") for j in jobs):
        return "failure"
    if "cancelled" in st:
        return "cancelled"
    if all(s == "skipped" for s in st):
        return "skipped"
    return "success"


class RunStore:
    def __init__(self, root: str, keep: int = KEEP_RUNS):
        self.root, self.keep = root, keep

    # ---- paths ------------------------------------------------------------------------------------
    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.root, run_id)

    def job_path(self, run_id: str, key: str) -> str:
        return os.path.join(self.run_dir(run_id), "jobs", f"{key}.json")

    def log_rel(self, key: str, index: int) -> str:
        return f"logs/{key}-{index}.log"

    def log_path(self, run_id: str, rel: str) -> str:
        return os.path.join(self.run_dir(run_id), rel)

    # ---- writing ----------------------------------------------------------------------------------
    def create(self, meta: dict, now: float | None = None) -> str:
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        t = (
            datetime.datetime.fromtimestamp(now, datetime.UTC)
            if now is not None
            else datetime.datetime.now(datetime.UTC)
        )
        run_id = f"{t.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        d = self.run_dir(run_id)
        for sub in ("", "jobs", "logs"):
            os.makedirs(os.path.join(d, sub), mode=0o700, exist_ok=True)
        write_json(os.path.join(d, "run.json"), {"schema": SCHEMA, "id": run_id, **meta, "finished_at": None})
        self.prune(keep_id=run_id)
        return run_id

    def write_job(self, run_id: str, job: dict) -> None:
        write_json(self.job_path(run_id, job["key"]), job)

    def finish(self, run_id: str, when: str) -> None:
        p = os.path.join(self.run_dir(run_id), "run.json")
        r = read_json(p)
        r["finished_at"] = when
        write_json(p, r)

    def prune(self, keep_id: str | None = None) -> list[str]:
        """Remove the oldest finished runs beyond the retention limit. A run with a job that is not final
        (a queued image job, running, waiting) is kept and not counted, until it is STALE_ACTIVE_DAYS old.
        Only directories named like run ids are touched."""
        ids = [i for i in self.ids() if i == keep_id or not self.active(i)]
        drop = [i for i in ids[: max(0, len(ids) - self.keep)] if i != keep_id]
        for i in drop:
            shutil.rmtree(self.run_dir(i), ignore_errors=True)
        return drop

    def active(self, run_id: str) -> bool:
        try:
            born = datetime.datetime.strptime(run_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.UTC)
            if (datetime.datetime.now(datetime.UTC) - born).days >= STALE_ACTIVE_DAYS:
                return False
            return self.load_run(run_id)["status"] in ACTIVE
        except RunRecordError:  # damaged: it may still be in flight, so keep it until it is stale
            return os.path.isfile(os.path.join(self.run_dir(run_id), "run.json"))
        except (OSError, ValueError, LookupError):  # also gone: another process pruned it
            return False

    # ---- reading ----------------------------------------------------------------------------------
    def ids(self) -> list[str]:
        """Run ids, oldest first (the id starts with its UTC creation time)."""
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return sorted(
            n for n in names if _RUN_ID.match(n) and os.path.isfile(os.path.join(self.root, n, "run.json"))
        )

    def resolve(self, ref: str) -> str:
        ids = self.ids()
        if ref in ids:
            return ref
        if ref == "latest" and ids:
            return ids[-1]
        hits = [i for i in ids if i.startswith(ref)] if ref else []
        if len(hits) == 1:
            return hits[0]
        raise LookupError(
            f"no run {ref!r}" if not hits else f"run {ref!r} is ambiguous ({len(hits)} matches)"
        )

    def load_run(self, ref: str) -> dict:
        """The run joined with its jobs. A damaged run.json or job file raises RunRecordError."""
        run_id = self.resolve(ref)
        rp = os.path.join(self.run_dir(run_id), "run.json")
        run = check_run_file(read_record(rp), rp)
        jobs = []
        jd = os.path.join(self.run_dir(run_id), "jobs")
        try:
            names = sorted(os.listdir(jd)) if os.path.isdir(jd) else []
        except OSError as e:
            raise RunRecordError(f"{jd}: unreadable ({e.strerror or type(e).__name__})") from None
        for n in names:
            if n.endswith(".json"):
                jp = os.path.join(jd, n)
                try:
                    jobs.append(check_job_file(read_record(jp), jp))
                except RunRecordError:
                    if os.path.exists(jp):
                        raise
                    # gone since the listing: another process pruned the run
        jobs.sort(key=lambda j: j.get("order", 0))
        run["jobs"] = jobs
        run["status"] = derive_status(jobs)
        return run

    def list_runs(self, limit: int | None = None, on_skip=None) -> list[dict]:
        """Newest first; each row is the run without its jobs plus a job count. A damaged run is skipped
        (one bad record must not hide the others); `on_skip(message)` is told why."""
        rows = []
        for i in reversed(self.ids()):
            try:
                r = self.load_run(i)
            except LookupError:
                if i in self.ids():  # still there: not the prune race but a real bug, so let it surface
                    raise
                continue  # pruned between ids() and resolve(): vanished, not damaged, so no on_skip
            except (OSError, ValueError) as e:
                if on_skip:
                    on_skip(str(e))
                continue
            jobs = r.pop("jobs")
            r["job_count"] = len(jobs)
            rows.append(r)
            if limit and len(rows) >= limit:
                break
        return rows


class LogWriter:
    """A step log with a size cap: past it bytes are dropped and a marker ends the file."""

    def __init__(self, path: str, cap: int | None = None):
        cap = STEP_LOG_MAX if cap is None else cap
        self.path, self.cap, self.size, self.truncated = path, cap, 0, False
        self.f = open(path, "wb")  # noqa: SIM115 - closed by close()

    def write(self, data: bytes) -> None:
        if self.truncated or not data:
            return
        room = self.cap - self.size
        if len(data) > room:
            self.f.write(data[: max(room, 0)])
            self.f.write(TRUNCATED.format(n=self.cap).encode())
            self.size = self.cap
            self.truncated = True
        else:
            self.f.write(data)
            self.size += len(data)
        self.f.flush()

    def close(self) -> None:
        self.f.close()
