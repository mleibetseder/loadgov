"""Read-only workflow-run JSON shared by the web UI (`/api/runs...`) and REST (`/v1/runs...`).

One implementation, so both listeners return the same shapes, bounds and error mapping: 400 for a bad number
or run id, 404 for an unknown run, job or step, 422 `damaged_record` for a record that cannot be read.
Everything here is synchronous (file reads): callers run `serve` in a thread.
"""

from __future__ import annotations

import logging
import os
import re
import stat

from loadgov.workflow.records import RunRecordError, RunStore

log = logging.getLogger("loadgovd.runs")

RUNS_DEFAULT, RUNS_MAX = 50, 200
LOG_DEFAULT, LOG_MAX = 64 * 1024, 256 * 1024  # bytes of one log page
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
MAX_KEY_CHARS = 200  # a log file name is "<key>-<n>.log" and must stay inside the 255-byte name limit


class Reply(Exception):
    """An early answer from a handler: status, JSON body."""

    def __init__(self, code: int, error: str, message: str):
        super().__init__(message)
        self.code, self.error, self.message = code, error, message


def _jid(job: dict) -> str:
    """The job id (matrix legs share it); a record that stores a null id falls back to its key."""
    return job["id"] if isinstance(job.get("id"), str) else job["key"]


def levels_of(jobs: list[dict]) -> dict[str, int]:
    """Level 1 = no needs; a job is one level below its deepest need (job `id`, so matrix legs share it).
    An unknown need is ignored; a cycle stops at the first repeat instead of looping."""
    needs = {_jid(j): [n for n in j.get("needs") or [] if isinstance(n, str)] for j in jobs}
    memo: dict[str, int] = {}

    def depth(i: str, seen: tuple) -> int:
        if i in memo:
            return memo[i]
        deps = [n for n in needs.get(i, []) if n in needs and n not in seen]
        memo[i] = 1 + max((depth(n, (*seen, i)) for n in deps), default=0)
        return memo[i]

    return {i: depth(i, ()) for i in needs}


def decode_clean(data: bytes, final: bool) -> tuple[str, int]:
    """Text and the bytes it used. Unless `final`, a multi-byte character cut off at the end is left for the
    next page (only a partial character: empty text and 0 bytes used); other bad bytes become U+FFFD."""
    keep = 0
    if not final:
        for k in range(1, min(3, len(data)) + 1):
            tail = data[-k:]
            need = 4 if tail[0] >= 0xF0 else 3 if tail[0] >= 0xE0 else 2 if tail[0] >= 0xC0 else 0
            if tail[0] >= 0xF8 or not need:
                continue
            if need > k and all(0x80 <= c < 0xC0 for c in tail[1:]):
                keep = k
                break
    used = len(data) - keep
    return data[:used].decode("utf-8", errors="replace"), used


def int_arg(q: dict, name: str, default: int, lo: int, hi: int) -> int:
    v = q.get(name, [str(default)])[0]
    if not (v.isascii() and v.isdigit()) or len(v) > 15 or not lo <= int(v) <= hi:
        raise Reply(400, "bad_request", f"{name}: expected an integer {lo}..{hi}")
    return int(v)


def _check_id(run_id: str) -> None:
    if not RUN_ID.match(run_id):
        raise Reply(400, "bad_request", "bad run id")


def list_runs(store: RunStore, limit: int, who: str) -> dict:
    damaged = []  # one damaged run never hides the others; the store tells us which it skipped
    rows = store.list_runs(limit, on_skip=damaged.append)
    for m in damaged:
        log.warning("%s: skipped a run record: %s", who, m)
    return {"ok": True, "runs": rows, "damaged": len(damaged)}


def load_run(store: RunStore, run_id: str) -> dict:
    _check_id(run_id)
    if run_id not in store.ids():
        raise Reply(404, "not_found", f"no run {run_id}")
    run = store.load_run(run_id)  # any damage raises; `serve` turns it into 422
    lv = levels_of(run["jobs"])
    for j in run["jobs"]:
        j["level"] = lv[_jid(j)]
    run["levels"] = [
        [j["key"] for j in run["jobs"] if j["level"] == n] for n in range(1, max(lv.values(), default=0) + 1)
    ]
    return run


def safe_key(key: str) -> bool:
    """Whatever the model can produce (any id, any matrix suffix), but nothing that names another place:
    no separator, NUL or control character, no leading dot, no `..`."""
    return (
        0 < len(key) <= MAX_KEY_CHARS
        and not key.startswith(".")
        and ".." not in key
        and key.isprintable()
        and not any(c in key for c in "/\\")
    )


def step_log(store: RunStore, run_id: str, key: str, n: str, offset: int, limit: int) -> dict:
    run = load_run(store, run_id)
    job = None
    if safe_key(key):  # the key must also be exactly one of the run's job keys
        job = next((j for j in run["jobs"] if j["key"] == key), None)
    if job is None:
        raise Reply(404, "not_found", f"no job {key}")
    if not (n.isascii() and n.isdigit()) or not 1 <= int(n) <= len(job["steps"]):
        raise Reply(404, "not_found", f"no step {n}")
    step = job["steps"][int(n) - 1]
    path = store.log_path(run_id, store.log_rel(key, int(n)))  # built here, never from the record
    done = step.get("status") in ("success", "failure", "cancelled", "skipped")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"ok": True, "text": "", "offset": offset, "next_offset": offset, "size": 0, "eof": done,
                "status": step.get("status"), "truncated": False}  # fmt: skip
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Reply(404, "not_found", "no log")
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(min(offset, size))
        data = f.read(limit)
    at_end = offset + len(data) >= size
    text, used = decode_clean(data, final=at_end and done)
    nxt = min(offset, size) + used
    return {
        "ok": True, "text": text, "offset": min(offset, size), "next_offset": nxt, "size": size,
        "eof": done and nxt >= size, "status": step.get("status"),
        "truncated": bool(step.get("log_truncated")),
    }  # fmt: skip


def serve(store: RunStore, parts: list[str], q: dict, who: str) -> tuple[int, dict]:
    """(status, JSON body) for `parts` = the path segments from `runs` on, `q` = parsed query.
    `who` names the listener in log lines. Never raises for a bad request or a damaged record."""
    try:
        if parts == ["runs"]:
            return 200, list_runs(store, int_arg(q, "limit", RUNS_DEFAULT, 1, RUNS_MAX), who)
        if len(parts) == 2 and parts[0] == "runs":
            return 200, {"ok": True, "run": load_run(store, parts[1])}
        if (
            len(parts) == 7
            and parts[0] == "runs"
            and parts[2] == "jobs"
            and parts[4] == "steps"
            and parts[6] == "log"
        ):
            off = int_arg(q, "offset", 0, 0, 1 << 40)
            lim = int_arg(q, "limit", LOG_DEFAULT, 4, LOG_MAX)
            return 200, step_log(store, parts[1], parts[3], parts[5], off, lim)
        raise Reply(404, "not_found", "no such endpoint")
    except Reply as r:
        return r.code, {"ok": False, "error": r.error, "message": r.message}
    except (RunRecordError, ValueError, OSError, RecursionError):
        # damaged or half-written records: a clean error. Any other exception (a bug) is the caller's 500
        # handling, which logs it and never sends a traceback.
        log.warning("%s: unreadable record behind %s", who, "/".join(parts[:2]), exc_info=True)
        return 422, {"ok": False, "error": "damaged_record", "message": "the run record could not be read"}
