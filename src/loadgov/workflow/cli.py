"""`loadctl workflow list|validate|run|parity` and `loadctl runs [show]`: the client side of the runner."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys

from loadgov.workflow import parity, runner
from loadgov.workflow.model import Workflow, WorkflowError, discover, load_workflow
from loadgov.workflow.records import RunRecordError, RunStore, default_runs_dir


def add_workflow_parser(sub) -> None:
    wf = sub.add_parser("workflow", help="run GitHub/Forgejo-Actions-compatible workflows through the lanes")
    ws = wf.add_subparsers(dest="workflow_cmd", required=True)
    ls = ws.add_parser("list", help="the workflows under .github/workflows and .forgejo/workflows")
    ls.add_argument("--path", default=".", help="project directory (default: the current directory)")
    ls.add_argument("--json", action="store_true")
    va = ws.add_parser("validate", help="load a workflow and refuse what is outside the supported subset")
    va.add_argument("file")
    va.add_argument("--json", action="store_true")
    ru = ws.add_parser("run", help="run a workflow's jobs, each as one governed `loadctl run`")
    ru.add_argument("file")
    ru.add_argument("--job", help="run only this job (its `needs` are not run)")
    ru.add_argument("--event", choices=runner.EVENTS, help="event to run for (default: workflow_dispatch)")
    ru.add_argument(
        "--project", help="project for the lanes (default: $LOADGOV_PROJECT or the workspace name)"
    )
    ru.add_argument("--key", help="issue key of this work, e.g. PROJ-42 (default: $LOADGOV_KEY)")
    ru.add_argument("--workspace", help="the checkout the steps run in (default: the project of the file)")
    ru.add_argument("--channel", choices=("beta", "release"), help="image channel for a `loadgov-image` job")
    ru.add_argument("--max-parallel", type=int, default=runner.MAX_PARALLEL_JOBS, help="jobs started at once")
    ru.add_argument(
        "--dry-run", action="store_true", help="print the plan and the loadctl commands, run nothing"
    )
    ru.add_argument("--runs-dir", help="where run records go (default: see docs/workflows.md)")
    ru.add_argument("--json", action="store_true", help="print the run record as JSON when done")
    pa = ws.add_parser(
        "parity", help="compare a local run with a hosted run's jobs (recorded JSON, no network)"
    )
    pa.add_argument("run", help="a local run id, unique prefix or `latest`, or a run record JSON file")
    pa.add_argument("--hosted", required=True, help="jobs JSON of the hosted run (REST jobs shape)")
    pa.add_argument("--outputs", help="outputs file: the deterministic outputs and their hosted values")
    pa.add_argument("--runs-dir", help="where run records are (default: see docs/workflows.md)")
    pa.add_argument("--json", action="store_true", help="print the report as JSON")


def add_runs_parser(sub) -> None:
    rs = sub.add_parser("runs", help="workflow run records (list, or `runs show RUN`)")
    rs.add_argument("--json", action="store_true")
    rs.add_argument("--limit", type=int, default=20, help="newest runs shown (default 20)")
    rs.add_argument("--runs-dir")
    sp = rs.add_subparsers(dest="runs_cmd")
    sh = sp.add_parser("show", help="one run: jobs and steps; --log prints the step logs")
    sh.add_argument("run", help="run id, a unique prefix of it, or `latest`")
    sh.add_argument("--job", help="only this job (key or id)")
    sh.add_argument("--step", help="only this step (number, id or name)")
    sh.add_argument("--log", action="store_true", help="print the log of the selected steps")
    sh.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sh.add_argument("--runs-dir", default=argparse.SUPPRESS)


# ---- helpers ----------------------------------------------------------------------------------------


def workspace_of(file: str, given: str | None) -> str:
    if given:
        return os.path.abspath(given)
    d = os.path.dirname(os.path.abspath(file))
    if os.path.basename(d) == "workflows" and os.path.basename(os.path.dirname(d)) in (".github", ".forgejo"):
        return os.path.dirname(os.path.dirname(d))
    return os.getcwd()


def host_id(sock: str) -> str:
    """$LOADGOV_HOST_ID, else the daemon's host id, else the id derived from the hostname."""
    if os.environ.get("LOADGOV_HOST_ID"):
        return os.environ["LOADGOV_HOST_ID"]
    from loadgov.cli import loadctl
    from loadgov.cli.client import DaemonUnavailable
    from loadgov.config import derive_host_id

    try:
        hid = loadctl.request(sock, "status")["status"]["host"].get("id")
        if isinstance(hid, str) and hid:
            return hid
    except (DaemonUnavailable, KeyError, TypeError, OSError):
        pass
    return derive_host_id(socket.gethostname())


def summary(wf: Workflow) -> dict:
    return {
        "file": wf.path,
        "name": wf.name,
        "on": wf.events,
        "jobs": [
            {
                "id": j.id,
                "needs": j.needs,
                "steps": len(j.steps),
                "instances": [
                    {"key": i.key, "lane": i.lane, "channel": i.channel} for i in wf.instances if i.job is j
                ],
            }
            for j in wf.jobs.values()
        ],
        "notes": wf.notes,
    }


# ---- workflow list / validate / run -------------------------------------------------------------------


def _list(a, out) -> int:
    rows = []
    for path in discover(a.path):
        rel = os.path.relpath(path, a.path)
        try:
            s = summary(load_workflow(path))
            rows.append({**s, "file": rel, "valid": True})
        except WorkflowError as e:
            rows.append({"file": rel, "valid": False, "error": str(e)})
    if a.json:
        print(json.dumps(rows, indent=2), file=out)
        return 0
    if not rows:
        print(f"no workflows under {a.path}/.github/workflows or .forgejo/workflows", file=out)
    for r in rows:
        if not r["valid"]:
            print(f"{r['file']}  INVALID  {r['error']}", file=out)
            continue
        jobs = ", ".join(
            f"{j['id']}[{'/'.join(sorted({i['lane'] for i in j['instances']}))}]" for j in r["jobs"]
        )
        print(f"{r['file']}  {r['name']}  on: {','.join(r['on'])}  jobs: {jobs}", file=out)
    return 0


def _validate(a, out, err) -> int:
    try:
        wf = load_workflow(a.file)
    except WorkflowError as e:
        print(f"refused: {e}", file=err)
        return 1
    s = summary(wf)
    if a.json:
        print(json.dumps(s, indent=2), file=out)
        return 0
    n = len(wf.instances)
    print(
        f"OK {wf.path}: {wf.name} (on: {', '.join(wf.events)}; {len(wf.jobs)} job(s), {n} instance(s))",
        file=out,
    )
    for j in s["jobs"]:
        lanes = sorted({i["lane"] for i in j["instances"]})
        extra = f", needs {', '.join(j['needs'])}" if j["needs"] else ""
        mult = f", {len(j['instances'])} matrix instances" if len(j["instances"]) > 1 else ""
        print(f"  job {j['id']}: lane {'/'.join(lanes)}, {j['steps']} step(s){extra}{mult}", file=out)
    for note in wf.notes:
        print(f"note: {note}", file=out)
    return 0


def _run(a, out, err, deps: runner.Deps | None = None) -> int:
    try:
        wf = load_workflow(a.file)
    except WorkflowError as e:
        print(f"refused: {e}", file=err)
        return 2
    ws = workspace_of(a.file, a.workspace)
    project = a.project or os.environ.get("LOADGOV_PROJECT") or os.path.basename(ws) or "default"
    if a.max_parallel < 1:
        print("loadctl workflow run: --max-parallel must be >= 1", file=err)
        return 2
    opts = runner.Options(
        project=project,
        workspace=ws,
        runs_dir=a.runs_dir or default_runs_dir(),
        event=a.event or "workflow_dispatch",
        job=a.job,
        key=a.key or os.environ.get("LOADGOV_KEY"),
        dry_run=a.dry_run,
        image_channel=a.channel,
        max_parallel=a.max_parallel,
    )
    if deps is None:
        override = os.environ.get("LOADGOV_LOADCTL")
        argv = shlex.split(override) if override else [sys.executable, "-m", "loadgov.cli.loadctl"]
        deps = runner.Deps(
            loadctl=[*argv, "--socket", a.socket],
            host_id=host_id(a.socket),
            out=err if a.json else out,
        )
    try:
        code, _run_id, result = runner.run_workflow(wf, opts, deps, explicit_event=a.event is not None)
    except (runner.RunnerError, RunRecordError) as e:
        print(f"loadctl workflow run: {e}", file=err)
        return 2
    if a.json and result is not None:
        print(json.dumps(result, indent=2), file=out)
    return code


def _parity(a, out, err) -> int:
    try:
        local = parity.load_local(a.run, a.runs_dir or default_runs_dir())
        hosted = parity.load_hosted(a.hosted)
        outputs = parity.load_outputs(a.outputs) if a.outputs else None
    except parity.ParityError as e:
        print(f"loadctl workflow parity: {e}", file=err)
        return 2
    report = parity.compare(local, hosted, outputs)
    print(json.dumps(report, indent=2) if a.json else parity.format_report(report), file=out)
    return 1 if report["divergences"] else 0


def workflow_command(a, out=None, err=None, deps: runner.Deps | None = None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    if a.workflow_cmd == "parity":
        return _parity(a, out, err)
    if a.workflow_cmd == "list":
        return _list(a, out)
    if a.workflow_cmd == "validate":
        return _validate(a, out, err)
    return _run(a, out, err, deps)


# ---- runs -------------------------------------------------------------------------------------------


def _dur(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def format_runs(rows: list[dict]) -> str:
    if not rows:
        return "no runs"
    head = ("RUN", "STATUS", "WORKFLOW", "EVENT", "PROJECT", "JOBS", "CREATED")
    body = [
        (
            r["id"],
            r["status"],
            r["workflow"]["stem"],
            r["event"],
            r["project"],
            str(r["job_count"]),
            r["created_at"],
        )
        for r in rows
    ]
    widths = [max(len(x[i]) for x in (head, *body)) for i in range(len(head))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in (head, *body)
    )


def _match_step(step: dict, want: str) -> bool:
    return want in (str(step["index"]), step.get("id"), step["name"])


def select_run(run: dict, job: str | None, step: str | None) -> dict:
    """The run limited to the chosen job and step (the JSON shape stays the same)."""
    out = dict(run)
    jobs = [j for j in run["jobs"] if job in (None, j["key"], j["id"])]
    out["jobs"] = [
        {**j, "steps": [s for s in j["steps"] if step is None or _match_step(s, step)]} for j in jobs
    ]
    return out


def format_run(run: dict) -> str:
    wf = run["workflow"]
    key = f" key {run['key']}" if run.get("key") else ""
    lines = [
        f"run {run['id']}: {run['status']}",
        f"workflow {wf['name']} ({wf['file']}), event {run['event']}, project {run['project']}{key}",
        f"host {run['host_id']}, sha {run.get('sha') or '-'}, ref {run.get('ref') or '-'}",
        f"created {run['created_at']}, finished {run['finished_at'] or '-'}",
    ]
    for j in run["jobs"]:
        code = "" if j["exit_code"] is None else f", exit {j['exit_code']}"
        why = f" ({j['reason']})" if j.get("reason") else ""
        lines.append(f"job {j['key']} [{j['lane']}]: {j['status']}{why} {_dur(j['duration_s'])}{code}")
        for s in j["steps"]:
            tail = f"  exit {s['exit_code']}" if s["exit_code"] not in (None, 0) else ""
            tail += "  (timed out)" if s.get("timed_out") else ""
            tail += "  (log truncated)" if s.get("log_truncated") else ""
            lines.append(f"  {s['index']}. {s['status']:<9} {_dur(s['duration_s']):>7}  {s['name']}{tail}")
    return "\n".join(lines)


def _show(a, store: RunStore, out, err) -> int:
    try:
        run = store.load_run(a.run)
    except (LookupError, RunRecordError) as e:
        print(f"loadctl runs: {e}", file=err)
        return 2
    sel = select_run(run, a.job, a.step)
    if (a.job or a.step) and not any(j["steps"] if a.step else True for j in sel["jobs"]):
        print("loadctl runs: no such job or step in this run", file=err)
        return 2
    if a.json:
        print(json.dumps(sel, indent=2), file=out)
        return 0
    print(format_run(sel), file=out)
    if a.log:
        for j in sel["jobs"]:
            for s in j["steps"]:
                print(f"\n== {j['key']} step {s['index']}: {s['name']} ==", file=out)
                try:
                    with open(store.log_path(run["id"], s["log"]), encoding="utf-8", errors="replace") as f:
                        out.write(f.read())
                except OSError:
                    print("(no log)", file=out)
    return 0


def runs_command(a, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    store = RunStore(getattr(a, "runs_dir", None) or default_runs_dir())
    if getattr(a, "runs_cmd", None) == "show":
        return _show(a, store, out, err)
    rows = store.list_runs(
        a.limit, on_skip=lambda m: print(f"loadctl runs: skipped a damaged run: {m}", file=err)
    )
    print(json.dumps(rows, indent=2) if a.json else format_runs(rows), file=out)
    return 0
