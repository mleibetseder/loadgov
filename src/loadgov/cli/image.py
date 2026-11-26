"""loadctl image: submit, list, cancel, now and the runner of the image train.

    loadctl image submit --project P --label L --channel beta|release [--key KEY] [--cwd DIR] -- cmd...
    loadctl image list [--json]
    loadctl image cancel ID [--project P | --owner]
    loadctl image now ID --owner --reason "..."
    loadctl image runner [--once]

The daemon records and arbitrates (docs/protocol.md "The image train"); only the runner runs a job, exactly
like `loadctl run --kind image --project P --label L --key K -- cmd...` in the job's cwd with the
allowlisted environment, so the job gets its own scope, the heavy slot, job_start/job_end and job_stats.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from loadgov.cli import loadctl
from loadgov.cli.client import DaemonUnavailable, request
from loadgov.config import IMAGE_ENV_ALLOW

CHANNELS = ("beta", "release")
NEAR_H = 12  # an empty train is shown in `loadctl status` when its window opens within this many hours


def add_image_parser(sub) -> None:
    im = sub.add_parser("image", help="the image train: beta/release container builds run in a night window")
    ims = im.add_subparsers(dest="image_cmd", required=True)
    sm = ims.add_parser("submit", help="queue an image build (it runs in the night window)")
    sm.add_argument("--project", required=True)
    sm.add_argument("--label", required=True)
    sm.add_argument("--channel", required=True, choices=CHANNELS)
    sm.add_argument("--key", help="issue key of this work, e.g. PROJ-42 (default: $LOADGOV_KEY)")
    sm.add_argument("--cwd", help="working directory of the job (default: the current directory)")
    sm.add_argument("--owner", action="store_true", help="act as the human owner (waives the beta interval)")
    sm.add_argument("--reason", help="why (required with --owner)")
    sm.add_argument("job_cmd", nargs=argparse.REMAINDER, metavar="cmd", help="-- command and arguments")
    ls = ims.add_parser("list", help="the queue with planned starts")
    ls.add_argument("--json", action="store_true")
    ca = ims.add_parser("cancel", help="remove a queued job (its project, or the owner)")
    ca.add_argument("id", type=int)
    ca.add_argument("--project", help="the job's project")
    ca.add_argument("--owner", action="store_true", help="act as the human owner")
    nw = ims.add_parser("now", help="owner: let a queued job run outside the window")
    nw.add_argument("id", type=int)
    nw.add_argument("--owner", action="store_true", help="act as the human owner (required)")
    nw.add_argument("--reason", required=True)
    rn = ims.add_parser(
        "runner", help="run the jobs the daemon releases (systemd timer; --once: at most one)"
    )
    rn.add_argument("--once", action="store_true", help="run at most one job, then exit")
    rn.add_argument("--slice", help=argparse.SUPPRESS)  # tests: a private slice instead of build-work-*


# ---- display --------------------------------------------------------------------------------------


def _at(iso_ts: str | None, tz: str) -> str:
    """HH:MM of a status timestamp in the window's time zone ('?' when unknown)."""
    try:
        t = datetime.datetime.fromisoformat((iso_ts or "").replace("Z", "+00:00"))
        return t.astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except (ValueError, OSError, KeyError):
        return "?"


def _opens_in(next_start: str | None, now: str | None) -> str | None:
    """`7 h` or `45 min` until the next window start; None when unknown or not within NEAR_H hours."""
    try:
        a = datetime.datetime.fromisoformat((next_start or "").replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat((now or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    mins = round((a - b).total_seconds() / 60)
    if mins < 0 or mins > NEAR_H * 60:
        return None
    return f"{mins} min" if mins < 60 else f"{round(mins / 60)} h"


def format_image_line(im: dict | None, now: str | None = None) -> str | None:
    """`image train: 3 queued, next 00:40 (web-app beta)`. An empty queue prints only when the window is open
    or opens within NEAR_H hours of `now` (the status `at`), as `image train: empty, window 00:00-06:00 UTC,
    opens in 7 h`. None when the train is not worth a line."""
    if not im:
        return None
    tz, rows = im["timezone"], im.get("queue") or []
    mark = " (override)" if im.get("source") == "override" else ""  # the owner changed it at run time
    if not rows and not im.get("running"):
        if im.get("in_window"):
            return f"image train: empty, window {im['window']} {tz}, open{mark}"
        if (opens := _opens_in(im.get("next_window_start"), now)) is None:
            return None
        return f"image train: empty, window {im['window']} {tz}, opens in {opens}{mark}"
    waiting = [r for r in rows if r["state"] != "running"]
    parts = [f"{im['queued']} queued"] if im["queued"] or not im.get("running") else []
    if im.get("running"):
        parts.append("1 running")
    if waiting:
        nxt = waiting[0]
        start = nxt["planned_start"] or im.get("next_window_start")
        parts.append(f"next {_at(start, tz)} ({nxt['project']} {nxt['channel']})")
    return "image train: " + ", ".join(parts) + (f", window {im['window']} {tz} (override)" if mark else "")


def format_queue(im: dict) -> str:
    tz = im["timezone"]
    state = "open" if im["in_window"] else "closed, next " + _at(im["next_window_start"], tz)
    head = f"window {im['window']} {tz}, {state}, beta every {im['beta_interval_h']} h"
    if im.get("source") == "override":
        head += " (override)"
    lines = [head]
    for r in im["queue"]:
        when = (
            f"{_at(r['planned_start'], tz)}-{_at(r['planned_finish'], tz)}"
            if r["planned_start"]
            else "unknown"
        )
        what = r.get("key") or r["label"]
        lines.append(
            f"  #{r['id']} {r['position']}. {r['project']} {r['channel']} {what} [{r['state']}] {when}"
        )
    if not im["queue"]:
        lines.append("  (empty)")
    return "\n".join(lines)


# ---- commands -------------------------------------------------------------------------------------


def _fail(reply: dict) -> int:
    """Print an error reply; 4 for a refusal or denial, 5 otherwise."""
    code = reply.get("error")
    if code == "owner_denied":
        print(f"owner denied: {reply['message']}", file=sys.stderr)
        return loadctl.EXIT_DENIED
    print(f"loadctl: {code}: {reply.get('message')}", file=sys.stderr)
    return loadctl.EXIT_DENIED if code == "refused" else loadctl.EXIT_ERROR


def _submit(a) -> int:
    cmd = a.job_cmd[1:] if a.job_cmd[:1] == ["--"] else list(a.job_cmd)
    if not cmd:
        print("loadctl image submit: no command (use: ... --channel beta -- cmd...)", file=sys.stderr)
        return 2
    if a.owner and not a.reason:
        print("loadctl image submit: --owner needs --reason", file=sys.stderr)
        return 2
    key, why = loadctl.resolve_key(a)
    if why:
        print(f"loadctl image submit: {why}", file=sys.stderr)
        return 2
    st = request(a.socket, "status")
    allow = ((st.get("status") or {}).get("image") or {}).get("env_allow") if st.get("ok") else None
    env = {k: os.environ[k] for k in (allow or IMAGE_ENV_ALLOW) if k in os.environ}
    args = {"project": a.project, "label": a.label, "channel": a.channel, "key": key, "argv": cmd,
            "cwd": os.path.abspath(a.cwd or os.getcwd()), "env": env}  # fmt: skip
    if a.owner:
        args |= {"owner": True, "reason": a.reason}
    r = request(a.socket, "image_submit", {k: v for k, v in args.items() if v is not None})
    if not r.get("ok"):
        return _fail(r)
    tz = ((st.get("status") or {}).get("image") or {}).get("timezone", "UTC")
    start = f", planned start {_at(r['planned_start'], tz)} {tz}" if r.get("planned_start") else ""
    print(f"queued image job {r['id']}: {a.project} {a.channel} {a.label} (position {r['position']}{start})")
    return 0


def _list(a) -> int:
    r = request(a.socket, "image_list")
    if not r.get("ok"):
        return _fail(r)
    print(json.dumps(r["image"], indent=2) if a.json else format_queue(r["image"]))
    return 0


def _simple(a, op: str, args: dict, done: str) -> int:
    r = request(a.socket, op, {k: v for k, v in args.items() if v is not None})
    if not r.get("ok"):
        return _fail(r)
    print(done)
    return 0


@dataclass
class Deps:
    """What the runner needs from the host; tests replace these."""

    run: Callable = None  # (Namespace of `loadctl run`) -> exit code
    now: Callable = time.time
    env: dict = field(default_factory=lambda: os.environ)

    def __post_init__(self):
        if self.run is None:
            from loadgov.cli.run import run_command

            self.run = run_command


def run_job(a, job: dict, d: Deps) -> tuple[int, bool]:
    """Run one released job like `loadctl run --kind image`: (exit code, started). The job's working
    directory and its allowlisted environment replace the runner's for the duration of the run."""
    ns = argparse.Namespace(
        socket=a.socket, kind="image", project=job["project"], label=job["label"], key=job.get("key"),
        floor_mb=None, on_floor="kill", headroom="full", no_wait=False, slice=getattr(a, "slice", None),
        start_by=job.get("start_by"), job_cmd=["--", *job["argv"]], outcome={},
    )  # fmt: skip
    saved_env, saved_cwd = dict(os.environ), os.getcwd()
    try:
        os.chdir(job["cwd"])
    except OSError as e:
        print(f"loadctl image runner: cannot enter {job['cwd']}: {e}", file=sys.stderr)
        return 127, True
    os.environ.clear()
    os.environ.update(job.get("env") or {})
    try:
        rc = d.run(ns)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)
    # Only run_command's own report says the job never started; the job's exit code (77 included) never does.
    return rc, not ns.outcome.get("start_by_missed", False)


def runner(a, d: Deps | None = None) -> int:
    d = d or Deps()
    done = 0
    while True:
        r = request(a.socket, "image_next")
        if not r.get("ok"):
            return _fail(r)
        job = r["job"]
        if job is None:
            return 0
        t0 = d.now()
        rc, started = run_job(a, job, d)
        dur = max(0.0, d.now() - t0)
        reply = request(a.socket, "image_done", {"id": job["id"], "exit_code": rc, "duration_s": dur,
                                                 "started": started})  # fmt: skip
        word = f"exit={rc}" if started else "not started (the window closed)"
        print(f"IMAGE_DONE {job['id']} {job['project']} {job['label']} {word} duration={dur:.0f}s")
        if not reply.get("ok"):
            return _fail(reply)
        done += 1
        if a.once or not started:
            return 0


def image_command(a, d: Deps | None = None) -> int:
    try:
        if a.image_cmd == "submit":
            return _submit(a)
        if a.image_cmd == "list":
            return _list(a)
        if a.image_cmd == "cancel":
            args = {"id": a.id, "project": a.project, "owner": True if a.owner else None}
            return _simple(a, "image_cancel", args, f"cancelled image job {a.id}")
        if a.image_cmd == "now":
            args = {"id": a.id, "owner": a.owner, "reason": a.reason}
            return _simple(a, "image_now", args, f"image job {a.id} may run outside the window")
        return runner(a, d)
    except DaemonUnavailable as e:
        print(f"loadctl: {e}", file=sys.stderr)
        return loadctl.EXIT_UNAVAILABLE
    except KeyboardInterrupt:
        return 130
