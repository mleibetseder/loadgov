"""Parity of one workflow run: the local run record against a hosted run's jobs (ILM-26).

`compare()` takes a local run record (the JSON of `loadctl runs show --json`, docs/workflows.md section 7)
and the jobs of a hosted run in the shape of the GitHub REST call
`GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs` (Forgejo Actions answers with the same shape), and
returns a report with two separate lists:

- `divergences`: real differences (a status, an exit code, the order of steps or dependent jobs, a missing job
  or step, a declared deterministic output). Any of them makes the result `diverged`.
- `host_only`: differences that are expected between a hosted runner and this host (runner labels, timing,
  the hosted runner's own set-up and post steps, the `GITHUB_*` environment). They never fail the comparison.

Nothing here touches the network or the host: the hosted side is a file. Every input is untrusted JSON, so
the files are size-bounded and every field is type-checked before use; a bad input is a `ParityError`.
"""

from __future__ import annotations

import datetime
import json
import os

from loadgov.workflow.records import FINAL, RunRecordError, RunStore

SCHEMA = 1
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_JOBS = 1024  # the runner's own cap on job instances per workflow
MAX_STEPS = 1024  # per job
MAX_DETERMINISTIC = 4096
MAX_TEXT = 4096

# What a hosted runner adds around the steps of the workflow: never a step of the file.
_HOSTED_STEPS = ("set up job", "complete job", "initialize containers", "stop containers")
_HOSTED_PREFIXES = ("post run ", "pre run ")  # an action's pre/post step, not any step named "Post ..."
_CONCLUSIONS = {"timed_out": "failure", "startup_failure": "failure", "neutral": "success"}

HOST_ONLY_ENV = (
    "environment: hosted runs set GITHUB_ACTIONS, GITHUB_RUN_ID, GITHUB_RUN_NUMBER, GITHUB_ACTOR, "
    "GITHUB_REPOSITORY, GITHUB_SERVER_URL, RUNNER_NAME and the like; local runs set none of them "
    "(they set LOADGOV_RUN_ID and LOADGOV_JOB_KEY instead)"
)


class ParityError(Exception):
    """An input that cannot be compared: unreadable, too large, malformed."""


# ---- loading (untrusted input) ----------------------------------------------------------------------


def read_json_file(path: str, what: str):
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        raise ParityError(f"{what}: cannot read {path}: {e.strerror or e}") from None
    if len(raw) > MAX_FILE_BYTES:
        raise ParityError(f"{what}: {path} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise ParityError(f"{what}: {path} is not valid JSON: {e}") from None


def _dict(v, where: str) -> dict:
    if not isinstance(v, dict):
        raise ParityError(f"{where} must be an object")
    return v


def _list(v, where: str, limit: int) -> list:
    if not isinstance(v, list):
        raise ParityError(f"{where} must be a list")
    if len(v) > limit:
        raise ParityError(f"{where} has {len(v)} entries (at most {limit})")
    return v


def _str(d: dict, key: str, where: str, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            raise ParityError(f"{where}.{key} is missing")
        return None
    if not isinstance(v, str):
        raise ParityError(f"{where}.{key} must be a string")
    if len(v) > MAX_TEXT:
        raise ParityError(f"{where}.{key} is longer than {MAX_TEXT} characters")
    return v


def _int(d: dict, key: str, where: str) -> int | None:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ParityError(f"{where}.{key} must be an integer")
    return v


def _when(s: str | None) -> datetime.datetime | None:
    if not s:
        return None
    try:
        t = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.UTC)


def load_hosted(path: str) -> list[dict]:
    """The jobs of a hosted run (GitHub REST shape), checked and reduced to what the comparison uses."""
    doc = read_json_file(path, "hosted jobs")
    jobs = _list(_dict(doc, "hosted jobs").get("jobs"), "hosted jobs.jobs", MAX_JOBS)
    out = []
    for i, j in enumerate(jobs):
        at = f"jobs[{i}]"
        j = _dict(j, at)
        steps = []
        for k, s in enumerate(_list(j.get("steps", []), f"{at}.steps", MAX_STEPS)):
            sat = f"{at}.steps[{k}]"
            s = _dict(s, sat)
            steps.append(
                {
                    "name": _str(s, "name", sat),
                    "number": _int(s, "number", sat) or k + 1,
                    "status": _str(s, "status", sat),
                    "conclusion": _str(s, "conclusion", sat, required=False),
                    "started_at": _str(s, "started_at", sat, required=False),
                    "completed_at": _str(s, "completed_at", sat, required=False),
                    "exit_code": _int(s, "exit_code", sat),  # not in the API: an optional recorded extra
                }
            )
        steps.sort(key=lambda s: s["number"])
        labels = j.get("labels", [])
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise ParityError(f"{at}.labels must be a list of strings")
        out.append(
            {
                "name": _str(j, "name", at),
                "status": _str(j, "status", at),
                "conclusion": _str(j, "conclusion", at, required=False),
                "started_at": _str(j, "started_at", at, required=False),
                "completed_at": _str(j, "completed_at", at, required=False),
                "labels": labels[:64],
                "runner_name": _str(j, "runner_name", at, required=False),
                "steps": steps,
            }
        )
    if not out:
        raise ParityError("hosted jobs: the file has no jobs")
    return out


def load_outputs(path: str) -> dict:
    """The outputs file: `deterministic` names the outputs to compare, `hosted` holds the hosted values.

    {"schema": 1,
     "deterministic": [{"job": "Check", "step": "Compute", "output": "sum"}],
     "hosted": {"<job name>": {"<step name or id>": {"<output>": "<value>"}}}}
    """
    doc = _dict(read_json_file(path, "outputs"), "outputs")
    det = []
    for i, e in enumerate(_list(doc.get("deterministic", []), "outputs.deterministic", MAX_DETERMINISTIC)):
        at = f"outputs.deterministic[{i}]"
        e = _dict(e, at)
        det.append({k: _str(e, k, at) for k in ("job", "step", "output")})
    hosted = _dict(doc.get("hosted", {}), "outputs.hosted")
    vals: dict[str, dict[str, dict[str, str]]] = {}
    for job, steps in hosted.items():
        for step, outs in _dict(steps, f"outputs.hosted[{job!r}]").items():
            for name, v in _dict(outs, f"outputs.hosted[{job!r}][{step!r}]").items():
                if not isinstance(v, str) or len(v) > MAX_TEXT:
                    raise ParityError(f"outputs.hosted[{job!r}][{step!r}][{name!r}] must be a short string")
                vals.setdefault(job, {}).setdefault(step, {})[name] = v
    return {"deterministic": det, "hosted": vals}


def load_local(ref: str, runs_dir: str | None) -> dict:
    """A local run: a run record file (`loadctl runs show --json`), else a run id, prefix or `latest`."""
    if os.path.isfile(ref):
        run = read_json_file(ref, "local run")
    else:
        try:
            run = RunStore(runs_dir or "").load_run(ref)
        except (LookupError, RunRecordError) as e:
            raise ParityError(f"local run: {e}") from None
        except (OSError, ValueError, RecursionError) as e:
            raise ParityError(f"local run {ref!r}: unreadable: {e}") from None
    return check_local(run)


def check_local(run) -> dict:
    run = _dict(run, "local run")
    jobs = _list(run.get("jobs"), "local run.jobs", MAX_JOBS)
    for i, j in enumerate(jobs):
        at = f"local run.jobs[{i}]"
        j = _dict(j, at)
        _str(j, "key", at)
        _str(j, "status", at)
        _str(j, "name", at, required=False)
        _str(j, "id", at, required=False)
        for key in ("needs", "notes"):
            v = j.get(key)
            if v is not None and not all(isinstance(x, str) for x in _list(v, f"{at}.{key}", MAX_JOBS)):
                raise ParityError(f"{at}.{key} must be a list of strings")
        if j.get("matrix") is not None:
            _dict(j["matrix"], f"{at}.matrix")
        for k, s in enumerate(_list(j.get("steps"), f"{at}.steps", MAX_STEPS)):
            s = _dict(s, f"{at}.steps[{k}]")
            _str(s, "name", f"{at}.steps[{k}]")
            _str(s, "status", f"{at}.steps[{k}]")
            _int(s, "exit_code", f"{at}.steps[{k}]")
            _int(s, "index", f"{at}.steps[{k}]")
            _str(s, "id", f"{at}.steps[{k}]", required=False)
            if not isinstance(s.get("outputs", {}), dict):
                raise ParityError(f"{at}.steps[{k}].outputs must be an object")
    notes = run.get("notes")
    if notes is not None and not all(isinstance(x, str) for x in _list(notes, "local run.notes", MAX_JOBS)):
        raise ParityError("local run.notes must be a list of strings")
    return run


# ---- comparison -------------------------------------------------------------------------------------


def _conclusion(c: str | None) -> str | None:
    return _CONCLUSIONS.get(c, c) if c else c


def _local_effective(rec: dict) -> str:
    """The conclusion a hosted runner reports: a failure the file tolerates (continue-on-error) is success."""
    if rec["status"] == "failure" and rec.get("continue_on_error"):
        return "success"
    return rec["status"]


def _is_hosted_step(name: str) -> bool:
    n = name.strip().lower()
    return n in _HOSTED_STEPS or n.startswith(_HOSTED_PREFIXES)


def _same_step(hosted: str, local: str) -> bool:
    return hosted == local or hosted == f"Run {local}"


def _duration(a: str | None, b: str | None) -> float | None:
    x, y = _when(a), _when(b)
    return None if x is None or y is None else round((y - x).total_seconds(), 3)


class _Report:
    def __init__(self):
        self.divergences: list[dict] = []
        self.host_only: list[dict] = []

    def diverge(self, kind: str, where: str, message: str, local=None, hosted=None) -> None:
        self.divergences.append(
            {"kind": kind, "where": where, "message": message, "local": local, "hosted": hosted}
        )

    def note(self, kind: str, where: str, message: str, local=None, hosted=None) -> None:
        self.host_only.append(
            {"kind": kind, "where": where, "message": message, "local": local, "hosted": hosted}
        )


def _find_job(jobs: list[dict], name: str) -> dict | None:
    """A job by its name or key, exactly first, then ignoring case."""
    for fold in (False, True):
        for j in jobs:
            for c in (j.get("name"), j.get("key")):
                if isinstance(c, str) and (c.casefold() == name.casefold() if fold else c == name):
                    return j
    return None


def _compare_steps(rep: _Report, job: str, local: dict, hosted: dict, counts: dict) -> None:
    lsteps = sorted(local["steps"], key=lambda s: s.get("index") or 0)
    hsteps = hosted["steps"]
    used: set[int] = set()
    pairs: list[tuple[dict, int]] = []  # (local step, position in the hosted list)
    for ls in lsteps:
        pos = next(
            (p for p, hs in enumerate(hsteps) if p not in used and _same_step(hs["name"], ls["name"])), None
        )
        if pos is None:
            rep.diverge(
                "step_missing_hosted",
                f"{job} / {ls['name']}",
                "the step ran locally but is not in the hosted run",
                local=ls["status"],
            )
            continue
        used.add(pos)
        pairs.append((ls, pos))
    for p, hs in enumerate(hsteps):
        if p in used:
            continue
        if _is_hosted_step(hs["name"]):
            rep.note(
                "hosted_step",
                f"{job} / {hs['name']}",
                "step added by the hosted runner, not part of the workflow file",
                hosted=hs["conclusion"],
            )
        else:
            rep.diverge(
                "step_missing_local",
                f"{job} / {hs['name']}",
                "the step is in the hosted run but did not run locally",
                hosted=hs["conclusion"],
            )
    positions = [p for _, p in pairs]
    if positions != sorted(positions):
        rep.diverge(
            "step_order",
            job,
            "the steps ran in a different order",
            local=[ls["name"] for ls in lsteps],
            hosted=[hs["name"] for hs in hsteps if not _is_hosted_step(hs["name"])],
        )
    for ls, p in pairs:
        hs = hsteps[p]
        where = f"{job} / {ls['name']}"
        counts["steps"] += 1
        if hs["status"] != "completed":
            rep.diverge(
                "state", where, f"the hosted step is {hs['status']!r}, not completed", hosted=hs["status"]
            )
        else:
            lc, hc = _local_effective(ls), _conclusion(hs["conclusion"])
            if lc != hc:
                rep.diverge("step_status", where, "the step ended differently", local=lc, hosted=hc)
        if (
            hs["exit_code"] is not None
            and ls.get("exit_code") is not None
            and hs["exit_code"] != ls["exit_code"]
        ):
            rep.diverge(
                "exit_code", where, "the exit codes differ", local=ls["exit_code"], hosted=hs["exit_code"]
            )


def _compare_outputs(rep: _Report, jobs: list[dict], outputs: dict, counts: dict) -> None:
    for e in outputs["deterministic"]:
        where = f"{e['job']} / {e['step']} / {e['output']}"
        job = _find_job(jobs, e["job"])
        step = None
        if job is not None:
            step = next(
                (s for s in job["steps"] if e["step"] in (s.get("name"), s.get("id"), str(s.get("index")))),
                None,
            )
        if step is None:
            rep.diverge(
                "output_unresolved", where, "the declared output names a job or step the local run lacks"
            )
            continue
        counts["outputs"] += 1
        got = step.get("outputs", {}).get(e["output"])
        want = outputs["hosted"].get(e["job"], {}).get(e["step"], {}).get(e["output"])
        if got is None or want is None:
            rep.diverge(
                "output_missing",
                where,
                "the deterministic output is missing on "
                + ("the local side" if got is None else "the hosted side"),
                local=got,
                hosted=want,
            )
        elif str(got) != want:
            rep.diverge(
                "output_value", where, "the deterministic output differs", local=str(got), hosted=want
            )


def compare(local: dict, hosted: list[dict], outputs: dict | None = None) -> dict:
    """The parity report of a checked local run record and checked hosted jobs."""
    outputs = outputs or {"deterministic": [], "hosted": {}}
    rep = _Report()
    counts = {"jobs": 0, "steps": 0, "outputs": 0}
    ljobs = local["jobs"]
    matched: dict[str, dict] = {}  # local job key -> hosted job
    used: set[int] = set()
    for lj in ljobs:
        name = lj.get("name") or lj["key"]
        hj = _find_job([h for i, h in enumerate(hosted) if i not in used], name)
        if hj is None:
            rep.diverge(
                "job_missing_hosted",
                name,
                "the job ran locally but is not in the hosted run",
                local=lj["status"],
            )
            continue
        used.add(next(i for i, h in enumerate(hosted) if h is hj))
        matched[lj["key"]] = hj
    for i, hj in enumerate(hosted):
        if i not in used:
            rep.diverge(
                "job_missing_local",
                hj["name"],
                "the job is in the hosted run but did not run locally",
                hosted=hj["conclusion"],
            )
    for lj in ljobs:
        hj = matched.get(lj["key"])
        if hj is None:
            continue
        name = lj.get("name") or lj["key"]
        counts["jobs"] += 1
        if lj["status"] not in FINAL:
            rep.diverge("state", name, f"the local job is {lj['status']!r}, not final", local=lj["status"])
            continue
        if hj["status"] != "completed":
            rep.diverge(
                "state", name, f"the hosted job is {hj['status']!r}, not completed", hosted=hj["status"]
            )
            continue
        lc, hc = _local_effective(lj), _conclusion(hj["conclusion"])
        if lc != hc:
            rep.diverge("job_status", name, "the job ended differently", local=lc, hosted=hc)
        _compare_steps(rep, name, lj, hj, counts)
        if hj["labels"] or lj.get("runs_on"):
            rep.note(
                "runner_labels",
                name,
                "runner labels differ by design",
                local=lj.get("runs_on"),
                hosted=hj["labels"],
            )
        if hj["runner_name"]:
            rep.note("runner", name, "the hosted runner that ran the job", hosted=hj["runner_name"])
        d_local, d_hosted = lj.get("duration_s"), _duration(hj["started_at"], hj["completed_at"])
        if d_local is not None and d_hosted is not None:
            rep.note(
                "timing",
                name,
                "job duration (seconds); local jobs may also wait for a slot",
                local=d_local,
                hosted=d_hosted,
            )
    _compare_job_order(rep, ljobs, matched)
    _compare_outputs(rep, ljobs, outputs, counts)
    for n in local.get("notes") or []:
        if isinstance(n, str):
            rep.note("local_note", "run", n[:MAX_TEXT])
    rep.note("environment", "run", HOST_ONLY_ENV)
    rep.note(
        "job_start_order",
        "run",
        "jobs without `needs` start in parallel on both sides; only `needs` order is compared",
    )
    return {
        "schema": SCHEMA,
        "result": "diverged" if rep.divergences else "identical",
        "local_run": local.get("id"),
        "compared": counts,
        "divergences": rep.divergences,
        "host_only": rep.host_only,
    }


def _compare_job_order(rep: _Report, ljobs: list[dict], matched: dict[str, dict]) -> None:
    """A job that `needs` another must start after the needed job ended, on the hosted side too."""
    by_id = {j.get("id", j["key"]): j for j in ljobs if not j.get("matrix")}
    for lj in ljobs:
        h = matched.get(lj["key"])
        if h is None or lj["status"] == "skipped":
            continue
        for need in lj.get("needs") or []:
            n = by_id.get(need)
            hn = matched.get(n["key"]) if n else None
            start, end = _when(h["started_at"]), _when(hn["completed_at"] or hn["started_at"]) if hn else None
            if start is not None and end is not None and start < end:
                rep.diverge(
                    "job_order",
                    lj.get("name") or lj["key"],
                    f"the hosted job started before its needed job {n.get('name') or need!r} ended",
                    local="after",
                    hosted="before",
                )


def format_report(rep: dict) -> str:
    c = rep["compared"]
    lines = [
        f"parity: {rep['result']} ({c['jobs']} job(s), {c['steps']} step(s), "
        f"{c['outputs']} output(s) compared; "
        f"{len(rep['divergences'])} divergence(s), {len(rep['host_only'])} host-only difference(s))"
    ]
    if rep["divergences"]:
        lines.append("divergences (real):")
        for d in rep["divergences"]:
            vals = ""
            if d["local"] is not None or d["hosted"] is not None:
                vals = f" [local {d['local']!r}, hosted {d['hosted']!r}]"
            lines.append(f"  {d['kind']}: {d['where']}: {d['message']}{vals}")
    if rep["host_only"]:
        lines.append("host-only (expected, not counted):")
        for d in rep["host_only"]:
            vals = ""
            if d["local"] is not None or d["hosted"] is not None:
                vals = f" [local {d['local']!r}, hosted {d['hosted']!r}]"
            lines.append(f"  {d['kind']}: {d['where']}: {d['message']}{vals}")
    return "\n".join(lines)
