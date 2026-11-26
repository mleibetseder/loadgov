"""The workflow runner: a client like `loadctl`, never the daemon.

Every job instance runs as ONE governed job:

    loadctl run --kind <lane> --project P --label <workflow>-<job>[-<matrix>] [--key K] -- \\
        python3 -m loadgov.workflow.jobexec <spec.json>

so the lanes, slots, headroom checks, light limits and statistics apply exactly as for any other job. A job
in the image lane is queued with `loadctl image submit ... --channel C -- <same script>` instead and
runs in the night window; jobs that need it are skipped. The runner orders jobs by `needs`, runs
independent jobs in parallel (the lanes decide who actually starts), and records runs -> jobs -> steps
(records.py).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

from loadgov.workflow import expr
from loadgov.workflow.model import Instance, Workflow
from loadgov.workflow.records import SCHEMA, RunStore, now_iso, read_json

MAX_PARALLEL_JOBS = 8
EVENTS = ("push", "pull_request", "workflow_dispatch")
LOADCTL_EXIT = {
    1: "the wait for headroom or the slot timed out",
    3: "the loadgov daemon is not reachable; the job did not start",
    5: "the loadgov daemon returned an error",
    75: "no room or slot busy (--no-wait)",
    76: "light lane overrun: the job exceeded the light runtime or memory limit",
    77: "the start-by time passed before the job started",
}


def git_info(workspace: str) -> tuple[str, str]:
    """(sha, ref) of the checkout; empty strings when it is not a git work tree."""

    def git(*args: str) -> str:
        try:
            r = subprocess.run(["git", "-C", workspace, *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""

    sha = git("rev-parse", "HEAD")
    ref = git("symbolic-ref", "-q", "HEAD")
    return sha, ref


def run_process(argv: list[str], env: dict) -> int:
    return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL).returncode


@dataclass
class Deps:
    launch: Callable[[list[str], dict], int] = run_process
    loadctl: list[str] = field(default_factory=lambda: [sys.executable, "-m", "loadgov.cli.loadctl"])
    script: list[str] = field(default_factory=lambda: [sys.executable, "-m", "loadgov.workflow.jobexec"])
    clock: Callable[[], float] = time.time
    host_id: str = "host"
    git: Callable[[str], tuple[str, str]] = git_info
    environ: dict = field(default_factory=lambda: dict(os.environ))
    out: TextIO = field(default_factory=lambda: sys.stdout)


@dataclass
class Options:
    project: str
    workspace: str
    runs_dir: str
    event: str = "workflow_dispatch"
    job: str | None = None
    key: str | None = None
    dry_run: bool = False
    image_channel: str | None = None
    max_parallel: int = MAX_PARALLEL_JOBS


class RunnerError(ValueError):
    """The run cannot start (bad option); nothing was recorded."""


def label_of(wf: Workflow, inst: Instance) -> str:
    return f"{wf.stem}-{inst.key}"


# ---- planning ---------------------------------------------------------------------------------------


def select(wf: Workflow, opts: Options) -> list[Instance]:
    if opts.job is None:
        return list(wf.instances)
    if opts.job not in wf.jobs:
        raise RunnerError(f"no job {opts.job!r} in {wf.path} (jobs: {', '.join(wf.jobs)})")
    return [i for i in wf.instances if i.job.id == opts.job]


def job_argv(wf: Workflow, inst: Instance, opts: Options, d: Deps, spec_path: str) -> list[str]:
    label = label_of(wf, inst)
    script = [*d.script, spec_path]
    if inst.lane == "image":
        channel = inst.channel or opts.image_channel
        if channel is None:
            raise RunnerError(
                f"job {inst.job.id!r} runs in the image lane: name the channel with the runs-on label "
                "loadgov-image-beta or loadgov-image-release, or pass --channel beta|release"
            )
        argv = [
            *d.loadctl,
            "image",
            "submit",
            "--project",
            opts.project,
            "--label",
            label,
            "--channel",
            channel,
        ]
        if opts.key:
            argv += ["--key", opts.key]
        return [*argv, "--cwd", opts.workspace, "--", *script]
    argv = [*d.loadctl, "run", "--kind", inst.lane, "--project", opts.project, "--label", label]
    if opts.key:
        argv += ["--key", opts.key]
    return [*argv, "--", *script]


def check_event(wf: Workflow, opts: Options, explicit: bool) -> None:
    if explicit and opts.event not in wf.events:
        raise RunnerError(
            f"{wf.path} does not trigger on {opts.event!r} (on: {', '.join(wf.events)}); "
            "pass another --event or add the trigger"
        )


def dry_run_text(wf: Workflow, insts: list[Instance], opts: Options, d: Deps) -> str:
    lines = [f"workflow {wf.name} ({wf.path}), event {opts.event}, project {opts.project}", ""]
    for inst in insts:
        j = inst.job
        needs = ", ".join(j.needs) if j.needs and opts.job is None else "-"
        lines.append(
            f"job {inst.key}: lane {inst.lane}, needs {needs}" + (f", if: {j.cond}" if j.cond else "")
        )
        lines.append("  $ " + " ".join(job_argv(wf, inst, opts, d, "<spec.json>")))
        for s in j.steps:
            cond = f"  [if: {s.cond}]" if s.cond else ""
            lines.append(f"  {s.index}. {s.label()}{cond}")
    for n in wf.notes:
        lines.append(f"note: {n}")
    return "\n".join(lines)


# ---- records ----------------------------------------------------------------------------------------


def initial_job(
    wf: Workflow, inst: Instance, opts: Options, d: Deps, order: int, run_id: str, store: RunStore
):
    j = inst.job
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "order": order,
        "key": inst.key,
        "id": j.id,
        "name": inst.name,
        "matrix": inst.matrix,
        "lane": inst.lane,
        "channel": inst.channel,
        "runs_on": j.runs_on,
        "label": label_of(wf, inst),
        "needs": list(j.needs),
        "status": "queued",
        "reason": None,
        "exit_code": None,
        "loadctl_exit": None,
        "started_at": None,
        "finished_at": None,
        "duration_s": None,
        "host_id": d.host_id,
        "continue_on_error": inst.continue_on_error,
        "notes": list(inst.notes),
        "steps": [
            {
                "index": s.index,
                "id": s.id,
                "name": s.label(),
                "kind": "uses" if s.uses else "run",
                "status": "queued",
                "exit_code": None,
                "started_at": None,
                "finished_at": None,
                "duration_s": None,
                "timed_out": False,
                "continue_on_error": False,
                "log": store.log_rel(inst.key, s.index),
                "log_truncated": False,
                "outputs": {},
                "note": None,
            }
            for s in j.steps
        ],
    }


def job_spec(wf: Workflow, inst: Instance, opts: Options, d: Deps, run_id: str, sha: str, ref: str) -> dict:
    j = inst.job
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "runs_dir": opts.runs_dir,
        "key": inst.key,
        "job_id": j.id,
        "workflow_name": wf.name,
        "event": opts.event,
        "ref": ref,
        "ref_name": ref.removeprefix("refs/heads/"),
        "sha": sha,
        "workspace": opts.workspace,
        "matrix": inst.matrix,
        "timeout_minutes": j.timeout_minutes,
        "env": {"workflow": wf.env, "job": j.env},
        "defaults": {"shell": j.shell, "working_directory": j.working_directory},
        "steps": [
            {
                "index": s.index,
                "id": s.id,
                "label": s.label(),
                "run": s.run,
                "uses": s.uses,
                "uses_ref": s.uses_ref,
                "with": s.with_,
                "shell": s.shell,
                "working_directory": s.working_directory,
                "env": s.env,
                "cond": s.cond,
                "continue_on_error": s.continue_on_error,
                "timeout_minutes": s.timeout_minutes,
            }
            for s in j.steps
        ],
    }


# ---- running ----------------------------------------------------------------------------------------


class _Run:
    def __init__(self, wf: Workflow, insts: list[Instance], opts: Options, d: Deps):
        self.wf, self.insts, self.opts, self.d = wf, insts, opts, d
        self.store = RunStore(opts.runs_dir)
        self.sha, self.ref = d.git(opts.workspace)
        self.run_id = ""
        self.done: dict[str, dict] = {}  # key -> {"status", "conclusion"}
        self.orders = {i.key: n for n, i in enumerate(insts)}

    def say(self, text: str) -> None:
        self.d.out.write(text + "\n")
        self.d.out.flush()

    # ---- job records ------------------------------------------------------------------------------
    def read(self, inst: Instance) -> dict:
        return read_json(self.store.job_path(self.run_id, inst.key))

    def update(self, inst: Instance, **fields) -> dict:
        job = self.read(inst)
        job.update(fields)
        self.store.write_job(self.run_id, job)
        return job

    def finish_job(self, inst: Instance, job: dict) -> None:
        coe = inst.continue_on_error
        st = job["status"]
        self.done[inst.key] = {"status": st, "conclusion": "success" if (coe and st == "failure") else st}
        extra = f" ({job['reason']})" if job.get("reason") else ""
        dur = f", {job['duration_s']:g}s" if job.get("duration_s") is not None else ""
        self.say(f"job {inst.key}: {st}{extra} [lane {inst.lane}{dur}]")

    def skip(self, inst: Instance, why: str, status: str = "skipped") -> None:
        now = now_iso(self.d.clock())
        job = self.read(inst)
        for s in job["steps"]:
            s["status"] = status
        job.update(status=status, reason=why, started_at=now, finished_at=now, duration_s=0.0)
        self.store.write_job(self.run_id, job)
        self.finish_job(inst, job)

    # ---- one job ----------------------------------------------------------------------------------
    def execute(self, inst: Instance) -> None:
        d, opts = self.d, self.opts
        spec_path = os.path.join(self.store.run_dir(self.run_id), "specs", f"{inst.key}.json")
        try:
            with open(spec_path, "w", encoding="utf-8") as f:
                json.dump(job_spec(self.wf, inst, opts, d, self.run_id, self.sha, self.ref), f, indent=2)
            argv = job_argv(self.wf, inst, opts, d, spec_path)
            self.update(inst, status="waiting for slot", started_at=None)
            env = dict(d.environ)
            env["LOADGOV_RUN_ID"] = self.run_id
            rc = d.launch(argv, env)
            err = None
        except (OSError, RunnerError) as e:
            rc, err = 127, f"cannot start: {e}"
        job = self.read(inst)
        job["loadctl_exit"] = rc
        reason = err or LOADCTL_EXIT.get(rc)
        if inst.lane == "image" and rc == 0 and job["status"] in ("waiting for slot", "queued"):
            job.update(status="queued", reason="submitted to the image train (see: loadctl image list)")
        elif job["status"] in ("waiting for slot", "queued", "running"):  # the job script never finished
            was_running = job["status"] == "running"
            for s in job["steps"]:
                if s["status"] == "running":
                    s["status"] = "failure"
                elif s["status"] == "queued":
                    s["status"] = "cancelled" if was_running else "skipped"
            killed = rc in (130, 143)
            job.update(
                status="cancelled" if killed else "failure",
                exit_code=rc,
                reason=reason
                or (
                    f"loadctl run exited {rc} before the job finished"
                    if rc
                    else "job script ended without a result"
                ),
                finished_at=now_iso(d.clock()),
            )
        elif rc != 0 and job["status"] == "success":  # the job passed but loadctl run did not: it overran
            job.update(status="failure", exit_code=rc, reason=reason or f"loadctl run exited {rc}")
        elif rc in LOADCTL_EXIT and rc != job.get("exit_code"):  # loadctl ended it (e.g. 76: light overrun)
            job.update(status="failure", reason=reason)
        self.store.write_job(self.run_id, job)
        self.finish_job(inst, job)

    # ---- scheduling -------------------------------------------------------------------------------
    def needs_of(self, inst: Instance) -> list[Instance]:
        if self.opts.job is not None:
            return []
        return [i for i in self.insts if i.job.id in inst.job.needs]

    def job_ctx(self, inst: Instance) -> dict:
        ctx = {
            "github": {
                "event_name": self.opts.event,
                "ref": self.ref,
                "ref_name": self.ref.removeprefix("refs/heads/"),
                "sha": self.sha,
                "workspace": self.opts.workspace,
            },
            "runner": {"os": "Linux"},
            "matrix": inst.matrix,
        }
        env: dict[str, str] = {}
        for raw in (self.wf.env, inst.job.env):
            for k, v in raw.items():
                env[k] = expr.interpolate(v, ctx | {"env": env})
        return ctx | {"env": env}

    def decide(self, inst: Instance) -> str | None:
        """None = run it; else the reason to skip."""
        deps = [self.done[i.key] for i in self.needs_of(inst)]
        if any(x["status"] == "queued" for x in deps):
            return "a needed job is queued in the image train and has not run"
        status = {
            "success": all(x["conclusion"] == "success" for x in deps),
            "failure": any(x["conclusion"] == "failure" for x in deps),
            "cancelled": any(x["conclusion"] == "cancelled" for x in deps),
        }
        cond = inst.job.cond
        if expr.should_run(cond, self.job_ctx(inst), status):
            return None
        if not status["success"] and (cond is None or not _has_status(cond)):
            return "a needed job did not succeed"
        return f"if: {cond} is false"

    def go(self) -> bool:
        """Run everything; True when it ran to its end, False when interrupted."""
        pending = list(self.insts)
        running: dict[cf.Future, Instance] = {}
        per_job: dict[str, int] = {}
        pool = cf.ThreadPoolExecutor(max_workers=max(1, self.opts.max_parallel))
        try:
            while pending or running:
                for inst in list(pending):
                    deps = self.needs_of(inst)
                    if any(i.key not in self.done for i in deps):
                        continue
                    why = self.decide(inst)
                    if why is not None:
                        pending.remove(inst)
                        self.skip(inst, why)
                        continue
                    cap = inst.job.max_parallel
                    if len(running) >= self.opts.max_parallel or (cap and per_job.get(inst.job.id, 0) >= cap):
                        continue
                    pending.remove(inst)
                    per_job[inst.job.id] = per_job.get(inst.job.id, 0) + 1
                    running[pool.submit(self.execute, inst)] = inst
                if not running:
                    if pending:  # nothing can start and nothing runs: cannot happen with a DAG
                        for inst in pending:
                            self.skip(inst, "never became ready", "cancelled")
                    break
                finished, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    inst = running.pop(fut)
                    per_job[inst.job.id] -= 1
                    try:
                        fut.result()
                    except Exception as e:  # a runner bug must not lose the other jobs
                        job = self.update(inst, status="failure", reason=f"runner error: {e}")
                        self.finish_job(inst, job)
            return True
        except KeyboardInterrupt:
            for inst in pending:
                self.skip(inst, "interrupted", "cancelled")
            pool.shutdown(wait=True)
            return False
        finally:
            pool.shutdown(wait=True)


def _has_status(text: str) -> bool:
    return any(n[0] == "call" for n in expr.walk(expr.parse_expr(text)))


def run_workflow(wf: Workflow, opts: Options, d: Deps | None = None, explicit_event: bool = False):
    """Run (or with dry_run print the plan of) a workflow: (exit code, run id or None, run dict or None).

    Exit codes: 0 every job succeeded or was skipped, 1 a job failed or was cancelled, 130 interrupted."""
    d = d or Deps()
    if opts.event not in EVENTS:
        raise RunnerError(f"--event must be one of {', '.join(EVENTS)}")
    check_event(wf, opts, explicit_event)
    insts = select(wf, opts)
    for inst in insts:  # fail before anything is recorded (e.g. an image job without a channel)
        job_argv(wf, inst, opts, d, "<spec.json>")
    if opts.dry_run:
        d.out.write(dry_run_text(wf, insts, opts, d) + "\n")
        return 0, None, None
    run = _Run(wf, insts, opts, d)
    meta = {
        "workflow": {"name": wf.name, "file": wf.path, "stem": wf.stem},
        "event": opts.event,
        "project": opts.project,
        "key": opts.key,
        "host_id": d.host_id,
        "sha": run.sha,
        "ref": run.ref,
        "workspace": opts.workspace,
        "created_at": now_iso(d.clock()),
        "notes": list(wf.notes),
    }
    run.run_id = run.store.create(meta, d.clock())
    os.makedirs(os.path.join(run.store.run_dir(run.run_id), "specs"), mode=0o700, exist_ok=True)
    for n, inst in enumerate(insts):
        run.store.write_job(run.run_id, initial_job(wf, inst, opts, d, n, run.run_id, run.store))
    run.say(f"run {run.run_id}: {wf.name}, {len(insts)} job(s)")
    complete = run.go()
    run.store.finish(run.run_id, now_iso(d.clock()))
    result = run.store.load_run(run.run_id)
    run.say(f"run {run.run_id}: {result['status']}")
    if not complete:
        return 130, run.run_id, result
    bad = result["status"] in ("failure", "cancelled")
    return (1 if bad else 0), run.run_id, result
