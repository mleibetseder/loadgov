"""loadctl: talk to loadgovd.

Exit codes: 0 ok, 2 usage, 3 daemon not reachable, 4 owner denied or request refused,
5 daemon returned another error.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import datetime
import fcntl
import json
import os
import sys
import time

from loadgov.cli.client import DaemonUnavailable, request
from loadgov.cli.history import add_history_parser, from_file, render, wants_file
from loadgov.config import KINDS
from loadgov.keys import KEY_ENV, key_error
from loadgov.protocol import DEFAULT_SOCKET, STARTING

EXIT_UNAVAILABLE = 3
EXIT_DENIED = 4
EXIT_ERROR = 5


def _local_time(at: str) -> str:
    """Status timestamps are UTC (`...Z`); legacy hwmon printed local time, so the hwmon lines do too."""
    try:
        t = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return at.replace("T", " ").rstrip("Z")
    return t.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _hm(at: str | None) -> str:
    """Local HH:MM of a status timestamp."""
    try:
        t = datetime.datetime.fromisoformat((at or "").replace("Z", "+00:00"))
    except ValueError:
        return at or "?"
    return t.astimezone().strftime("%H:%M")


def _span(seconds: int) -> str:
    return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


def resolve_key(a: argparse.Namespace, env=None) -> tuple[str | None, str | None]:
    """(key, error): `--key`, else $LOADGOV_KEY; empty counts as unset. The shape is checked here too, so
    a bad key stops the command before anything is queued or started."""
    key = getattr(a, "key", None) or (os.environ if env is None else env).get(KEY_ENV) or None
    why = key_error(key) if key is not None else None
    return (None, why) if why else (key, None)


def format_stray(it: dict) -> str:
    where = it["project"] or it["cwd"] or it["cgroup"]
    return (f"pid {it['pid']} start {it['start_time']}  {it['comm']}  {it['cpu_pct']:g}% CPU for "
            f"{it['age_s'] // 60} min  {where}  {it['cmdline'][:60]}")  # fmt: skip


def format_hwmon(s: dict) -> str:
    """The five-line format of legacy `hwmon status`, so scripts and people reading it keep working."""
    lv, c, m, w, d = s["levels"], s["cpu"], s["memory"], s["swap"], s["disk"]
    at = _local_time(s["at"])
    return "\n".join(
        [
            f"hwmon {at}  level {s['level']}",
            f"  cpu   {lv['cpu']:5}  {c['busy_pct']:5.1f} % busy, pressure "
            f"{c['psi_some_avg60']:.0f} %, load {c['load1']:.1f} on {s['host']['cores']} cores",
            f"  ram   {lv['ram']:5}  {m['avail_mb'] / 1024:.1f} of {m['total_mb'] / 1024:.1f} GB available",
            f"  swap  {lv['swap']:5}  {w['used_pct']:.0f} % used ({w['used_mb'] / 1024:.1f} GB)",
            f"  disk  {lv['disk']:5}  {d['free_gb']:.0f} GB free on {', '.join(d['paths'])}",
        ]
    )


def _lim(e: dict) -> str:
    return (
        f"cores {e['max_cores']:g}, cpu {e['target_cpu_pct']:g} %, load {e['load_max']:g}, "
        f"impl {e['max_implementers']}, reviewers {e['max_reviewers']}, light {e['max_light']}, "
        f"ram floor {e['ram_floor_gb']:g} GB"
    )


def _n(v, fmt: str = "g") -> str:
    return "-" if v is None else format(v, fmt)


def format_job_eta(r: dict) -> str:
    if r.get("overdue"):
        return " overdue"
    return f" eta {_hm(r['eta'])}" if r.get("eta") else ""


def format_jobs(rows: list) -> str:
    if not rows:
        return "no jobs running"
    return "\n".join(
        f"{r['project']} {r['kind']} {r.get('key') or r['label']} pid {r['pid']} since {r['since']}"
        + format_job_eta(r)
        for r in rows
    )


def format_endings(rows: list) -> str:
    """One line per ended job, newest first: when, project kind key-or-label, duration, exit, cause."""
    if not rows:
        return "no jobs ended in that time"
    out = []
    for r in rows:
        exit_ = "-" if r.get("exit_code") is None else str(r["exit_code"])
        sig = f" signal {r['signal']}" if r.get("signal") else ""
        dur = "-" if r.get("duration_s") is None else _span(int(r["duration_s"]))
        out.append(
            f"{r['ended_at']} {r['project']} {r['kind']} {r.get('key') or r['label']} "
            f"took {dur} exit {exit_}{sig} {r['cause']}"
        )
    return "\n".join(out)


def _slot_why(reply: dict) -> str:
    """What a waiting client reports: the daemon's reason (memory admission), else the holder."""
    return reply.get("reason") or f"held by {reply.get('holder', 'unknown')}"


def format_waiters(sl: dict) -> list[str]:
    """One line per waiter: `next: 1. project kind KEY waiting 3m eta 18:40` (eta = expected start)."""
    lines = []
    for w in sl.get("waiters") or []:
        what = " ".join(x for x in (w["project"], w.get("kind"), w.get("key") or w["label"]) if x)
        eta = f" eta {_hm(w['eta'])}" if w.get("eta") else ""
        why = f" ({w['reason']})" if w.get("reason") else ""
        lines.append(
            f"{'next:' if w['position'] == 1 else '     '} {w['position']}. {what} "
            f"waiting {_span(w['wait_s'])}{eta}{why}"
        )
    return lines


def format_job_stats(rows: list) -> str:
    """A table per project and kind; seconds and MB, p50/p95 over the last 200 jobs, '-' = unknown."""
    if not rows:
        return "no finished jobs recorded"
    head = ("project", "kind", "jobs", "dur p50 s", "own cpu p50/p95 s", "docker cpu p50/p95 s",
            "total cpu p50/p95 s", "host cpu p50 s", "mem peak p50/p95/max MB")  # fmt: skip
    body = [
        (r["project"], r["kind"], str(r["count"]), _n(r["duration_p50_s"], ".0f"),
         f"{_n(r['cpu_p50_s'], '.1f')}/{_n(r['cpu_p95_s'], '.1f')}",
         f"{_n(r['docker_cpu_p50_s'], '.1f')}/{_n(r['docker_cpu_p95_s'], '.1f')}",
         f"{_n(r['total_cpu_p50_s'], '.1f')}/{_n(r['total_cpu_p95_s'], '.1f')}",
         _n(r["host_cpu_p50_s"], ".1f"),
         "/".join(_n(r[k], ".0f") for k in ("mem_peak_p50_mb", "mem_peak_p95_mb", "mem_peak_max_mb")))
        for r in rows
    ]  # fmt: skip
    w = [max(len(x[i]) for x in (head, *body)) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(row)).rstrip() for row in (head, *body))


def format_shmem(bs: dict) -> str:
    """`, shmem N MB in build-work.slice` once the shmem share exceeds `leak_alert_mb`, else ''."""
    over = (bs.get("shmem_mb") or 0) > bs.get("leak_alert_mb", 1024)
    return f", shmem {bs['shmem_mb']} MB in build-work.slice" if over else ""


def format_backstop_projects(bs: dict) -> str:
    """`  backstop projects: a 8192 MB, b 8192 MB capped 9000 MB (not applied), c shmem 2048 MB`: capped
    projects and those whose shmem share exceeds `leak_alert_mb`; '' when there is none."""
    parts = []
    for name, p in sorted((bs.get("projects") or {}).items()):
        bits = []
        if p.get("cap_mb"):
            bits.append(f"{p['cap_mb']} MB")
            if p.get("capped_mb"):
                bits.append(f"capped {p['capped_mb']} MB")
            if not p.get("applied"):
                bits.append("(not applied)")
        if (p.get("shmem_mb") or 0) > bs.get("leak_alert_mb", 1024):
            bits.append(f"shmem {p['shmem_mb']} MB")
        if bits:
            parts.append(f"{name} " + " ".join(bits))
    return "  backstop projects: " + ", ".join(parts) if parts else ""


def format_status(s: dict) -> str:
    host = s["host"].get("id")  # an older daemon has no host id
    lines = format_hwmon(s).replace("hwmon ", f"loadgov {host} " if host else "loadgov ", 1).split("\n")
    if (
        "used_pct" in s["disk"]
    ):  # the legacy five-line format stays as it was; an older daemon has no used_pct
        lines[4] = lines[4].replace(" on ", f", {s['disk']['used_pct']:.0f} % used on ", 1)
    if s["host"].get("mode") == "monitor":  # an older daemon has no mode
        lines.append(
            "  mode  monitor: every enforce flag is forced off, decisions are only reported as WOULD"
        )
    c, m, io = s["cpu"], s["memory"], s["io"]
    cpu_psi = f"cpu {c['psi_some_avg10']:.0f}/{c['psi_some_avg60']:.0f}"
    if c.get("psi_effective", c["psi_some_avg60"]) != c["psi_some_avg60"]:
        cpu_psi += f" (throttling only, counts {c['psi_effective']:.0f})"
    lines.append(
        f"  psi   {cpu_psi}  "
        f"mem {m['psi_some_avg10']:.0f}/{m['psi_some_avg60']:.0f}  "
        f"io {io['psi_some_avg10']:.0f}/{io['psi_some_avg60']:.0f} % (avg10/avg60)"
    )
    for name, cg in s["cgroups"].items():
        if cg is not None:
            cores = "unlimited" if cg["cores"] is None else f"{cg['cores']} cores"
            lines.append(f"  cap   {name}: {cores}")
    hs = s["heavy_slot"]
    if hs["held"]:
        flag = "  STUCK" if hs["stuck"] else ""
        age = f"{hs['age_s'] // 60} min" if hs["age_s"] is not None else "age unknown"
        who = hs["holder"] or f"locked by pid {','.join(map(str, hs['lock_pids']))}"
        lines.append(f"  slot  heavy: {who} ({age}){flag}")
    else:
        lines.append("  slot  heavy: free")
    if s["stopped"]:
        lines.append(f"  stopped {len(s['stopped'])} process(es) in governed cgroups")
    mg = s.get("memguard")
    if mg:
        mode = "enforce" if mg["enforce"] else "observe"
        lines.append(
            f"  guard {mode}: pause < {mg['low_mb']} MB, resume >= {mg['resume_mb']} MB, "
            f"{len(mg['paused'])} paused"
        )
        for p in mg["paused"]:
            lines.append(
                f"        {p['id']} {p['kind']} {p['how']} {p['what']}" + ("" if p["real"] else " (would)")
            )
    bs = s.get("backstop")
    if bs and bs["enabled"]:
        state = "applied" if bs["applied"] else f"NOT applied ({bs['note']})"
        lines.append(
            f"  backstop memory.max {bs['memory_max_mb']} MB, swap.max {bs['memory_swap_max_mb']} MB: "
            f"{state}, {bs['oom_kills']} OOM kill(s)"
            + (f", {bs['max_events']} max hit(s)" if bs.get("max_events") else "")
            + (f", {bs['swap_fail_events']} swap fail(s)" if bs.get("swap_fail_events") else "")
            + (f", last {bs['last_event']}" if bs["last_event"] else "")
            + format_shmem(bs)
        )
    elif bs and (bs.get("shmem_mb") or 0) > bs.get("leak_alert_mb", 1024):
        lines.append(f"  backstop off{format_shmem(bs)}")
    if bs and (line := format_backstop_projects(bs)):
        lines.append(line)
    strays = (s.get("strays") or {}).get("items") or []
    if strays:
        lines.append(f"  strays: {len(strays)}")
    for name, sl in s.get("slots", {}).items():
        lines.append(f"  slot  {format_slot(name, sl)}")
        lines.extend(f"        {w}" for w in format_waiters(sl))
    for j in s.get("jobs") or []:
        slot = f" [{j['slot']}]" if j.get("slot") else ""
        lines.append(
            f"  job   {j['project']} {j['kind']} {j.get('key') or j['label']}{slot}{format_job_eta(j)}"
        )
    lt = s.get("light")
    if lt:
        lines.append(f"  light {lt['running']}/{lt['max']} (per project max {lt['per_project_max']})")
    from loadgov.cli.image import format_image_line

    if line := format_image_line(s.get("image"), s.get("at")):
        lines.append(f"  {line}")
    pj = format_projects(s.get("projects"))
    if pj:
        lines.append(f"  projects {pj}")
    lines.append(f"  jvm   {s['jvm_daemons']} Gradle/Kotlin daemon(s)")
    lines.append("  top   " + ", ".join(f"{p['comm']}({p['pcpu']:.0f}%,{p['rss_mb']}MB)" for p in s["top"]))
    if "limits" in s:
        lim = s["limits"]
        ceil = f", ceiling {lim['ceiling']}" if lim["ceiling"] else ""
        lines.append(f"  limits {lim['profile']}{ceil}: {_lim(lim['effective'])}")
    dmn = s["daemon"]
    on = [k for k, v in dmn.get("enforce", {}).items() if v]
    detail = f" ({', '.join(on)})" if dmn["mode"] == "partial" else ""
    lines.append(f"  daemon {dmn['version']} pid {dmn['pid']} mode {dmn['mode']}{detail}")
    if dmn.get("memory_protection_mb") == 0:
        lines.append("  warn  memory protection 0 MB: set MemoryMin on system.slice (docs/deployment.md)")
    if tw := (s.get("rest") or {}).get("tokens_warning"):
        lines.append(f"  warn  rest {tw} (fix tokens.env before a restart: docs/deployment.md)")
    ui = s.get("ui") or {}
    if ui.get("enabled") and not ui.get("built"):
        lines.append("  warn  ui enabled but no build is installed (ui/README.md, docs/deployment.md)")
    return "\n".join(lines)


def _slot_cell(sl: dict) -> str:
    if sl.get("state") is None:
        return "-"
    out = "free" if sl["state"] == "free" else (sl.get("holder") or "held")
    return out + (f" +{sl['queue']}" if sl.get("queue") else "")


def _age_text(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    return (
        f"{seconds}s" if seconds < 120 else f"{seconds // 60}m" if seconds < 7200 else f"{seconds // 3600}h"
    )


def format_fleet(fleet: dict) -> str:
    """One row per host. A host that cannot be reached shows its last data aged, and the error below."""
    head = ("host", "mode", "level", "load/core", "light", "heavy", "compile", "image", "reach")
    rows, notes = [], []
    for h in fleet["hosts"]:
        load = (h.get("cpu") or {}).get("load1_per_core")
        lt, im = h.get("light") or {}, h.get("image") or {}
        if h["reachable"]:
            reach = "local" if h.get("local") else f"ok {_age_text(h.get('age_s'))}"
        else:
            reach = "DOWN" + ("" if h.get("age_s") is None else f" (last {_age_text(h['age_s'])} ago)")
        rows.append((
            h["id"] + (" (id mismatch)" if h.get("id_mismatch") else ""), h["mode"], h.get("level") or "-",
            "-" if load is None else f"{load:.2f}",
            "-" if lt.get("running") is None or lt.get("max") is None else f"{lt['running']}/{lt['max']}",
            _slot_cell(h.get("heavy") or {}), _slot_cell(h.get("compile") or {}),
            "-" if im.get("queued") is None else f"{im['queued']} queued"
            + (f", next {_hm(im['next_start'])}" if im.get("next_start") else ""),
            reach,
        ))  # fmt: skip
        if h.get("id_mismatch"):
            notes.append(
                f"{h['id']}: the host reports id {h.get('reported_id')!r}; check its [host] id and the url"
            )
        if h.get("error"):
            notes.append(f"{h['id']}: {h['error']}")
    w = [max(len(r[i]) for r in (head, *rows)) for i in range(len(head))]
    table = ["  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in (head, *rows)]
    return "\n".join([*table, *notes])


def format_projects(projects: dict | None) -> str:
    """Busy projects by CPU: `web 2.4 cores 6.1 GB (1 job, 2 waiting)`. Idle ones are skipped."""
    rows = []
    for name, p in (projects or {}).items():
        if not (p.get("jobs") or p.get("waiting") or p.get("cpu_pct")):
            continue
        mem = p.get("mem_mb")
        text = f"{name} " + ("? cores" if p.get("cores") is None else f"{p['cores']:g} cores")
        text += "" if mem is None else f" {mem / 1024:.1f} GB"
        bits = [f"{p['jobs']} job{'s' * (p['jobs'] != 1)}"] if p.get("jobs") else []
        bits += [f"{p['waiting']} waiting"] if p.get("waiting") else []
        rows.append((p.get("cpu_pct") or 0, text + (f" ({', '.join(bits)})" if bits else "")))
    return ", ".join(t for _, t in sorted(rows, key=lambda r: -r[0]))


def format_slot(name: str, sl: dict) -> str:
    if sl["state"] == "free":
        out = f"{name}: free"
    else:
        who = sl["holder"] or "unknown"
        out = f"{name}: {sl['state']} by {who} [{sl['source']}] since {sl['held_since'] or '?'}"
        out += f" key {sl['key']}" if sl.get("key") else ""
    out += f", {sl['queue']} waiting" if sl["queue"] else ""
    out += f", nested: {sl['nested']}" if sl.get("nested") else ""
    out += f", hold pending ({sl['hold']})" if sl["hold"] and sl["state"] != "held-by-hold" else ""
    return out + ("  STUCK" if sl["stuck"] else "")


def format_limits(lim: dict) -> str:
    r = lim["restriction"]
    lines = [f"profile {lim['profile']}: {_lim(lim['effective'])}"]
    if lim["ceiling"]:
        c = lim["ceiling"]
        lines.append(f"  ceiling {c['profile']}: {c['reason']} (until {c['expires_at'] or 'cleared'})")
    for o in lim["overrides"]:
        lines.append(
            f"  {o['principal']} {o['param']}={o['value']:g} (was {o['previous']:g}) "
            f"until {o['expires_at'] or 'cleared'}: {o['reason']}"
        )
    lines.append(f"  restriction: {r['kind'] + ' (' + r['reason'] + ')' if r['active'] else 'none'}")
    return "\n".join(lines)


def format_reply(op: str, reply: dict) -> str:
    if op == "stray_kill":
        return f"refused: {reply['reason']}"
    if op == "set" and "after" in reply:  # image settings
        b, n = reply["before"], reply["after"]
        return (f"image {'cleared' if reply['cleared'] else 'set'}: window {n['window']} {n['timezone']}, "
                f"beta interval {n['beta_interval_h']} h ({n['source']}); "
                f"was {b['window']} {b['timezone']}, {b['beta_interval_h']} h ({b['source']})")  # fmt: skip
    if reply.get("restrictedBy") == "system":
        return f"refused: the memory guard holds the pause ({reply['reason']})"
    if reply.get("refused"):
        c = f"{reply['ceiling']} ceiling: " if reply.get("ceiling") else ""
        return f"refused: owner restriction active ({c}{reply['reason']})"
    if op == "reset":
        return f"reset: removed {len(reply['removed'])} override(s); {_lim(reply['effective'])}"
    if op == "ceiling_clear":
        return "ceiling cleared" if reply["cleared"] else "no ceiling was set"
    exp = f", until {reply['expires_at']}" if reply.get("expires_at") else ""
    if op == "ceiling_set":
        return f"ceiling {reply['ceiling']} set{exp}: {_lim(reply['effective'])}"
    cap = " (ttl capped to 12h)" if reply.get("ttl_capped") else ""
    ch = ", ".join(f"{k} {v['before']:g} -> {v['after']:g}" for k, v in reply["changed"].items())
    return f"ok ({reply['principal']}){exp}{cap}: {ch or 'no change'}; profile {reply['profile']}"


def _timing(p: argparse.ArgumentParser) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ttl", help="90s, 15m, 2h")
    g.add_argument("--until", help="'07:00 Europe/Berlin'")
    p.add_argument("--reason", required=True)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="loadctl", description="Control and inspect loadgovd.")
    ap.add_argument("--socket", default=os.environ.get("LOADGOV_SOCKET", DEFAULT_SOCKET))
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="show host load, levels, caps and the heavy slot")
    fmt = st.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print the raw status JSON")
    fmt.add_argument("--hwmon", action="store_true", help="print legacy hwmon's five-line format")
    fl = sub.add_parser("fleet", help="every host of the fleet (read-only; see docs/protocol.md, Fleet)")
    fls = fl.add_subparsers(dest="fleet_cmd", required=True)
    fst = fls.add_parser("status", help="one row per host: mode, level, load, light, slots, image, reachable")
    fst.add_argument("--json", action="store_true", help="print the raw fleet JSON")
    jb = sub.add_parser("jobs", help="running `loadctl run` jobs, or with --stats what finished jobs used")
    jb.add_argument("--stats", action="store_true", help="per project and kind: count, duration, CPU, memory")
    jb.add_argument("--ended", action="store_true", help="recent job endings with exit code and cause")
    jb.add_argument("--since", metavar="DURATION", help="with --ended: only endings in the last 90s, 15m, 2h")
    jb.add_argument("--json", action="store_true")
    se = sub.add_parser("set", help="change the profile or single limits")
    se.add_argument("--profile")
    se.add_argument("--max-cores", type=float, dest="max_cores")
    se.add_argument("--target-cpu", type=float, dest="target_cpu_pct")
    se.add_argument("--load-max", type=float, dest="load_max")
    se.add_argument("--max-impl", type=int, dest="max_implementers")
    se.add_argument("--max-reviewers", type=int, dest="max_reviewers")
    se.add_argument("--max-light", type=int, dest="max_light")
    se.add_argument("--ram-floor-gb", type=float, dest="ram_floor_gb")
    se.add_argument("--image-window", dest="image_window", metavar="HH:MM-HH:MM",
                    help="owner: image night window (may cross midnight), over [image] window")  # fmt: skip
    se.add_argument("--image-timezone", dest="image_timezone", metavar="ZONE",
                    help="owner: IANA time zone of the image window")  # fmt: skip
    se.add_argument("--image-beta-interval-h", type=int, dest="image_beta_interval_h", metavar="N",
                    help="owner: a project's beta starts at most every N hours (0-336)")  # fmt: skip
    se.add_argument("--image-clear", action="store_true", dest="image_clear",
                    help="owner: drop the image overrides, back to the [image] config values")  # fmt: skip
    se.add_argument("--owner", action="store_true", help="act as the human owner (checked by the daemon)")
    se.add_argument("--project")
    _timing(se)
    ce = sub.add_parser("ceiling", help="owner ceiling: caps every limit at a profile")
    cs = ce.add_subparsers(dest="ceiling_cmd", required=True)
    cset = cs.add_parser("set", help="set the ceiling")
    cset.add_argument("profile")
    _timing(cset)
    cs.add_parser("clear", help="remove the ceiling")
    rs = sub.add_parser("reset", help="owner: remove owner overrides (or every override with --all)")
    rs.add_argument("params", nargs="*", metavar="param", help="e.g. max_cores load_max")
    rs.add_argument("--all", action="store_true", help="also remove all agent overrides")
    rs.add_argument("--owner", action="store_true", help="act as the human owner (required)")
    rs.add_argument("--reason", required=True)
    li = sub.add_parser("limits", help="effective limits, overrides, ceiling, restriction")
    li.add_argument("--json", action="store_true")
    sl = sub.add_parser("slot", help="heavy/compile slot arbiter")
    ss = sl.add_subparsers(dest="slot_cmd", required=True)
    sa = ss.add_parser("acquire", help="take a slot (exit 0 granted, 1 busy or timeout)")
    sa.add_argument("slot", choices=["heavy", "compile"])
    sa.add_argument("--project", required=True)
    sa.add_argument("--label", required=True)
    sa.add_argument("--pid", type=int, help="holder pid, default: the parent shell of loadctl")
    sa.add_argument("--wait", action="store_true", help="wait in the FIFO queue")
    sa.add_argument(
        "--fd",
        type=int,
        help="the shell's open fd on the lock file (exec 9>>lock); required unless --no-lock",
    )
    sa.add_argument(
        "--no-lock",
        action="store_true",
        help="arbitration only, no real flock (tests); never use in a script",
    )
    sa.add_argument("--timeout", type=float, dest="timeout_s", help="give up after this many seconds")
    sa.add_argument("--key", help=f"issue key of this work, e.g. PROJ-42 (default: ${KEY_ENV})")
    sr = ss.add_parser("release", help="release the slot held for --pid")
    sr.add_argument("slot", choices=["heavy", "compile"])
    sr.add_argument("--pid", type=int, help="holder pid, default: the parent shell of loadctl")
    sr.add_argument(
        "--fd", type=int, help="also unlock this inherited lock fd (the shell keeps its descriptor)"
    )
    sr.add_argument("--owner", action="store_true", help="owner: release a slot held for another process")
    sst = ss.add_parser("status", help="state of both slots")
    sst.add_argument("--json", action="store_true")
    fr = sub.add_parser("freeze", help="owner: stop all build work now (until unfreeze, or --ttl)")
    fr.add_argument("--project", help="only this project's work")
    fr.add_argument("--ttl", help="90s, 15m, 2h; default: until unfreeze")
    fr.add_argument("--owner", action="store_true", help="act as the human owner (required)")
    fr.add_argument("--reason", required=True)
    uf = sub.add_parser("unfreeze", help="owner: resume what freeze stopped (memory guard pauses wait)")
    uf.add_argument("--project")
    uf.add_argument("--owner", action="store_true", help="act as the human owner (required)")
    uf.add_argument("--reason")
    sub.add_parser("reap-daemons", help="stop idle Gradle/Kotlin build daemons (frees memory)")
    sy = sub.add_parser("stray", help="list stray processes (orphaned CPU hogs); --kill PID: owner kill")
    sy.add_argument("--json", action="store_true")
    sy.add_argument("--kill", type=int, metavar="PID", help="owner: SIGTERM (then SIGKILL) this stray")
    sy.add_argument(
        "--start-time", type=int, dest="start_time", help="its start time (status field), checked"
    )
    sy.add_argument("--owner", action="store_true", help="act as the human owner (required with --kill)")
    sy.add_argument("--reason", help="why (audit)")
    ho = sub.add_parser("hold", help="owner: hold the slots (drain) until turned off")
    ho.add_argument("state", choices=["on", "off"])
    ho.add_argument("--slot", choices=["heavy", "compile", "all"], default="all")
    ho.add_argument("--owner", action="store_true", help="act as the human owner (required)")
    ho.add_argument("--reason", help="required for 'on'")
    wa = sub.add_parser("wait", help="block until the host has headroom for one more heavy job")
    wa.add_argument("--kind", required=True, choices=KINDS)
    wa.add_argument("--project")
    wa.add_argument("--label", default="job")
    wa.add_argument(
        "--timeout",
        type=float,
        help="give up after this many seconds (default: config, 7200; 0 = wait forever)",
    )
    wa.add_argument("--confirm", type=float, help="seconds between the two passing samples (default 60)")
    wa.add_argument("--poll", type=float, help="seconds between checks (default 30)")
    for key in OVERRIDE_KEYS:
        wa.add_argument(
            "--" + key.replace("_", "-"), type=float, dest=key, metavar="N",
            help="replace the configured threshold for this call (the profile scaling still applies)",
        )  # fmt: skip
    lw = sub.add_parser("level-wait", help="block until the hwmon level is below LIMIT on 2 samples")
    lw.add_argument("max_s", nargs="?", type=float, default=1800.0, help="give up after this many seconds")
    lw.add_argument("--poll", type=float, default=15.0, help="seconds between samples (default 15)")
    run_args(sub)
    from loadgov.cli.image import add_image_parser

    add_image_parser(sub)
    add_history_parser(sub)
    from loadgov.workflow.cli import add_runs_parser, add_workflow_parser

    add_workflow_parser(sub)
    add_runs_parser(sub)
    lg = sub.add_parser("log", help="last audit lines")
    lg.add_argument("-n", type=int, default=20)
    return ap


OVERRIDE_KEYS = (
    "load1_max",
    "load5_max",
    "psi_max",
    "mem_min_gb",
    "swap_max_mb",
    "swap_in_max",
    "mem_psi_max",
)


def run_args(sub) -> None:
    from loadgov.cli.run import add_run_parser

    add_run_parser(sub)


SLOT_POLL_S = 2.0
SLOT_NOTE_S = 300


def _client_lock(fd: int, reply: dict) -> str | None:
    """Take the granted slot's real lock on the shell's inherited descriptor and write the holder line.

    flock on an inherited fd locks the shared open file description, so the lock outlives loadctl and
    the daemon, exactly like `flock -n 9` in the legacy scripts. Returns None on success, "busy" when
    somebody else holds the lock, else an error message."""
    try:
        st, want = os.fstat(fd), os.stat(reply["lock_path"])
    except OSError as e:
        return f"fd {fd} is not usable or the lock file is missing: {e}"
    if (st.st_dev, st.st_ino) != (want.st_dev, want.st_ino):
        return f"fd {fd} is not open on {reply['lock_path']}"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return "busy"
    except OSError as e:
        return f"cannot lock fd {fd}: {e}"
    try:
        with open(reply["holder_path"], "w") as f:
            f.write(reply["holder_line"] + "\n")
    except OSError as e:
        return f"cannot write {reply['holder_path']}: {e}"
    return None


def _open_lock(path: str) -> int:
    """The lock file for `loadctl run`: no O_CLOEXEC, so the job inherits the descriptor and the flock
    lives as long as loadctl run or the job does."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    os.set_inheritable(fd, True)
    return fd


def slot_acquire(a: argparse.Namespace, out=None, sleep=None) -> int:
    """Poll `slot_acquire` until granted; same messages as heavy-slot.sh (every 300 s while waiting).

    `a.open_lock` (loadctl run) opens the lock file itself after the grant and leaves the descriptor in
    `a.lock_fd`; otherwise the shell's `--fd` is used."""
    out = out or sys.stdout
    tag = "HEAVY" if a.slot == "heavy" else "COMPILE"
    who = f"{a.project} {a.label}"
    pid = a.pid or os.getppid()
    start = time.monotonic()
    next_note = 0.0
    args = {"slot": a.slot, "project": a.project, "label": a.label, "pid": pid, "wait": a.wait}
    a.lock_fd = None
    if a.fd is not None or getattr(a, "open_lock", False):
        args["client_lock"] = True
    if a.timeout_s:
        args["timeout_s"] = a.timeout_s
    key, why = resolve_key(a)
    if why:
        print(f"loadctl: {why}", file=sys.stderr)
        return 2
    if key:
        args["key"] = key
    if getattr(a, "kind", None):
        args["kind"] = a.kind
    while True:
        try:
            reply = request(a.socket, "slot_acquire", args)
        except DaemonUnavailable as e:
            print(f"loadctl: {e}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        if not reply.get("ok"):
            print(f"loadctl: {reply.get('error')}: {reply.get('message')}", file=sys.stderr)
            return EXIT_ERROR
        waited = int(time.monotonic() - start)
        if reply["status"] == "granted" and reply.get("lock_path"):
            fd, failed = a.fd, None
            if fd is None and getattr(a, "open_lock", False):
                try:
                    fd = _open_lock(reply["lock_path"])
                except OSError as e:
                    failed = f"cannot open {reply['lock_path']}: {e}"
            if fd is None and failed is None:
                failed = "no --fd given"
            if failed is None:
                failed = _client_lock(fd, reply)
            opened = fd is not None and fd != a.fd
            if failed and opened:
                os.close(fd)
            elif opened:
                a.lock_fd = fd
            if failed:  # give the slot back; "busy" means a legacy holder won the race
                with contextlib.suppress(DaemonUnavailable):
                    request(a.socket, "slot_release", {"slot": a.slot, "pid": pid})
                if failed != "busy":
                    print(f"loadctl: {failed}", file=sys.stderr)
                    return EXIT_ERROR
                reply = {**reply, "status": "queued", "holder": "unknown (lock taken by another process)"}
        if reply["status"] == "granted":
            note = " (no lock: arbitration only)" if getattr(a, "no_lock", False) else ""
            if reply.get("nested"):
                note = f" (nested under {reply.get('holder')}: no new lock)"
            print(f"{tag}_SLOT_OK {who} waited={waited}s{note}", file=out)
            return 0
        if reply["status"] == "timeout" or not a.wait or (a.timeout_s and waited >= a.timeout_s):
            print(
                f"{tag}_SLOT_WAIT {who}: {_slot_why(reply)} waited={waited}s",
                file=sys.stderr,
            )
            if a.wait:  # leave the queue now rather than after the daemon notices
                with contextlib.suppress(DaemonUnavailable):
                    request(a.socket, "slot_release", {"slot": a.slot, "pid": pid})
            return 1
        if waited >= next_note:
            print(
                f"{tag}_SLOT_WAIT {who}: {_slot_why(reply)} waited={waited}s",
                file=sys.stderr,
            )
            next_note = waited + SLOT_NOTE_S
        (sleep or time.sleep)(SLOT_POLL_S)


def headroom_state(reply: dict) -> str:
    """The state text of wait-headroom.sh's HEADROOM_* lines."""
    d, th = reply["decision"], reply["thresholds"]
    v = d["values"]
    if d.get("reason"):  # the review lane: no load or PSI in its decision
        return d["reason"] + "".join(f" {r}" for r in d["reasons"] if r.startswith("paused:"))
    l5max = "-" if th["load5_max"] is None else f"{th['load5_max']:.2f}"
    out = (
        f"load={v['load1']:g}/{v['load5']:g} (max {th['load1_max']:.2f}/{l5max}, {v['cpus']} CPUs) "
        f"psi={v['cpu_psi']:g}% (max {th['psi_max']:g}%) mem={v['mem_gb']:.1f}G (min {th['mem_min_gb']:g}G) "
        f"swap={v['swap_mb']}MB mempsi={v['mem_psi']:g}%"
    )
    return out + "".join(f" {r}" for r in d["reasons"] if r.startswith("paused:"))


NOT_READY_STARTUP_S = 120.0  # no --timeout and no reply yet: how long a starting daemon is waited for
NOT_READY_POLL_S = 5.0


def sample_key(reply: dict):
    """Which daemon sample a headroom reply is on, as integer milliseconds: `sample_at_ms` when the daemon
    sends it, else `sample_at` (whole seconds) times 1000, so replies of two daemon versions still compare."""
    ms = reply.get("sample_at_ms")
    if isinstance(ms, int) and not isinstance(ms, bool):
        return ms
    return calendar.timegm(time.strptime(reply["sample_at"], "%Y-%m-%dT%H:%M:%SZ")) * 1000


def wait_headroom(a, sleep=time.sleep, out=None, last: dict | None = None, err=None) -> int:
    """Like wait-headroom.sh: a pass must hold on two samples `confirm` seconds apart.

    `not_ready` replies (daemon starting or restarting, stale sample) are retried within the wait budget
    (120 s when none is known yet). `last`, when given, receives the most recent daemon reply as
    `last["reply"]` (used by loadgov-mcp).
    `err` is the stream for progress and errors (default sys.stderr, looked up per call). Poll sleeps never
    pass the timeout; a started confirmation completes, so a call may overrun by up to `confirm`."""
    out = out or sys.stdout
    err = err or sys.stderr
    over = {k: getattr(a, k) for k in OVERRIDE_KEYS if getattr(a, k, None) is not None}

    def query() -> dict | None:
        args = {"kind": a.kind, **({"project": a.project} if a.project else {})}
        # ready_wait=0: the start-up wait is this function's own budget, not the client's 30 s
        r = request(a.socket, "headroom", {**args, **({"overrides": over} if over else {})}, ready_wait=0)
        if r.get("error") in (STARTING, "not_ready"):
            return None
        if not r.get("ok"):
            raise RuntimeError(f"{r.get('error')}: {r.get('message')}")
        if last is not None:
            last["reply"] = r
        return r

    confirm, poll, max_wait = a.confirm, a.poll, a.timeout  # None until the first reply supplies them
    waited = 0.0
    last_ok: dict | None = None  # the newest usable reply, for the state text

    def fetch() -> dict | None:
        """The newest decision; None while the daemon is not ready (starting, restarting, stale sample)."""
        nonlocal confirm, poll, max_wait, last_ok
        x = query()
        if x is None:
            return None
        t = x["timing"]
        confirm = t["confirm_s"] if confirm is None else confirm
        poll = max(t["poll_s"] if poll is None else poll, 1.0)  # a zero poll would spin
        max_wait = t["max_wait_s"] if max_wait is None else max_wait
        last_ok = x
        return x

    def budget() -> float:
        """Seconds of waiting allowed (0 = forever); before the first reply a startup cap applies."""
        return NOT_READY_STARTUP_S if max_wait is None else max_wait

    def expired() -> bool:
        return budget() > 0 and waited >= budget()  # max_wait 0 = wait forever (host-wait.sh)

    def nap(d: float) -> None:
        nonlocal waited
        if budget() > 0:
            d = min(d, max(budget() - waited, 0.0))
        sleep(d)
        waited += d

    def timeout() -> int:
        state = headroom_state(last_ok) if last_ok else "daemon not ready"
        print(f"HEADROOM_TIMEOUT {a.label} {state} after {waited:.0f}s: not started", file=err)
        return 1

    def newer(x: dict | None, than) -> bool:
        return x is not None and sample_key(x) > than

    try:
        r = fetch()
        while r is None:  # precondition P2: a starting or restarting daemon is waited out, not an error
            if expired():
                return timeout()
            if waited == 0:
                print(f"HEADROOM_WAIT {a.label} daemon not ready waited=0s", file=err)
            nap(NOT_READY_POLL_S if poll is None else max(poll, 1.0))
            r = fetch()
        recheck = False
        while True:
            if r["decision"]["pass"]:
                first = sample_key(r)
                sleep(confirm)  # never shortened: the two samples must be confirm_s apart
                waited += confirm
                r = fetch()
                while not newer(r, first) and not expired():  # the confirm needs a newer sample
                    nap(poll)
                    r = fetch()
                if newer(r, first) and r["decision"]["pass"]:
                    print(f"HEADROOM_OK {a.label} {headroom_state(r)} waited={waited:.0f}s", file=out)
                    return 0
                recheck = r is not None  # a failed confirm: look again at once, like host-wait.sh
            if expired():
                return timeout()
            if not recheck:
                if r is not None and waited % 300 < poll:
                    print(f"HEADROOM_WAIT {a.label} {headroom_state(r)} waited={waited:.0f}s", file=err)
                nap(poll)
            recheck = False
            r = fetch()
            while r is None and not expired():  # went not_ready mid-wait: keep polling inside the budget
                nap(poll)
                r = fetch()
            if r is None:
                return timeout()
    except DaemonUnavailable as e:
        print(f"loadctl: {e}", file=err)
        return EXIT_UNAVAILABLE
    except RuntimeError as e:
        print(f"loadctl: {e}", file=err)
        return EXIT_ERROR


def level_wait(a, sleep=time.sleep, clock=time.monotonic, out=None) -> int:
    """hwmon wait: block until the level is below LIMIT on 2 samples in a row, print the five lines."""
    out = out or sys.stdout
    end, ok_in_row = clock() + a.max_s, 0
    try:
        while True:
            r = request(a.socket, "status")
            if not r.get("ok"):
                print(f"loadctl: {r.get('error')}: {r.get('message')}", file=sys.stderr)
                return EXIT_ERROR
            s = r["status"]
            passed = s["level"] == a.require if getattr(a, "require", None) else s["level"] != "LIMIT"
            ok_in_row = ok_in_row + 1 if passed else 0
            if ok_in_row >= 2:
                print(format_hwmon(s), file=out)
                return 0
            if clock() >= end:
                print("hwmon wait: still at LIMIT after", int(a.max_s), "s", file=out)
                print(format_hwmon(s), file=out)
                return 1
            sleep(max(a.poll, 1.0))
    except DaemonUnavailable as e:
        print(f"loadctl: {e}", file=sys.stderr)
        return EXIT_UNAVAILABLE


def main(argv: list[str] | None = None, sleep=time.sleep) -> int:
    a = _parser().parse_args(argv)
    if a.cmd == "run":
        from loadgov.cli.run import run_command

        return run_command(a)
    if a.cmd == "image":
        from loadgov.cli.image import image_command

        return image_command(a)
    if a.cmd == "workflow":
        from loadgov.workflow.cli import workflow_command

        return workflow_command(a)
    if a.cmd == "runs":
        from loadgov.workflow.cli import runs_command

        return runs_command(a)
    if a.cmd == "history" and wants_file(a):
        return from_file(a)
    if a.cmd == "level-wait":
        return level_wait(a, sleep)
    if a.cmd == "wait":
        for flag in ("timeout", "confirm", "poll"):
            if getattr(a, flag) is not None and getattr(a, flag) < 0:
                print(f"loadctl: --{flag} must be >= 0", file=sys.stderr)
                return 2
        return wait_headroom(a, sleep)
    if a.cmd == "slot" and a.slot_cmd == "acquire":
        if a.fd is None and not a.no_lock:
            print(
                "loadctl: slot acquire takes no lock without a descriptor: "
                "use --fd N (see compat/heavy-slot.sh) "
                "or --no-lock for arbitration-only testing",
                file=sys.stderr,
            )
            return 2
        return slot_acquire(a)
    if a.cmd == "set":
        op = "set"
        keys = (
            "profile",
            "max_cores",
            "target_cpu_pct",
            "load_max",
            "max_implementers",
            "max_reviewers",
            "max_light",
            "ram_floor_gb",
        )
        args = {k: getattr(a, k) for k in (*keys, "ttl", "until", "reason", "project")}
        args["owner"] = a.owner
        for k in ("image_window", "image_timezone", "image_beta_interval_h"):
            if getattr(a, k) is not None:
                args[k] = getattr(a, k)
        if a.image_clear:
            args["image_clear"] = True
    elif a.cmd == "reset":
        op = "reset"
        args = {"params": a.params, "all": a.all, "reason": a.reason, "owner": a.owner}
    elif a.cmd == "slot":
        op = "slot_" + a.slot_cmd
        args = {} if a.slot_cmd == "status" else {"slot": a.slot, "pid": a.pid or os.getppid()}
        if a.slot_cmd == "release" and a.owner:
            args["owner"] = True
    elif a.cmd in ("freeze", "unfreeze"):
        op = a.cmd
        args = {"project": a.project, "owner": a.owner, "reason": a.reason}
        if a.cmd == "freeze":
            args["ttl"] = a.ttl
    elif a.cmd == "reap-daemons":
        op = "reap"
        args = {"reason": "loadctl reap-daemons"}
    elif a.cmd == "stray" and a.kill is not None:
        op = "stray_kill"
        args = {"pid": a.kill, "start_time": a.start_time, "owner": a.owner, "reason": a.reason}
    elif a.cmd == "jobs" and a.ended:
        op = "job_endings"
        args = {"since": a.since}
    elif a.cmd == "stray" or a.cmd == "jobs":
        op = "status"
        args = {}
    elif a.cmd == "fleet":
        op = "fleet"
        args = {}
    elif a.cmd == "hold":
        op = "hold"
        args = {"state": a.state, "slot": a.slot, "owner": a.owner, "reason": a.reason}
    elif a.cmd == "ceiling":
        op = "ceiling_" + a.ceiling_cmd
        args = {"owner": True}
        if a.ceiling_cmd == "set":
            args |= {"profile": a.profile, "reason": a.reason, "ttl": a.ttl, "until": a.until}
    else:
        op = a.cmd
        args = {"n": a.n} if a.cmd in ("log", "history") else {}
    args = {k: v for k, v in args.items() if v is not None}
    try:
        reply = request(a.socket, op, args)
    except DaemonUnavailable as e:
        print(f"loadctl: {e}", file=sys.stderr)
        print("loadctl: is it running? systemctl status loadgovd", file=sys.stderr)
        return EXIT_UNAVAILABLE
    if reply.get("refused"):
        print(format_reply(op, reply), file=sys.stderr)
        return EXIT_DENIED
    if reply.get("error") == "owner_denied":
        print(f"owner denied: {reply['message']}", file=sys.stderr)
        return EXIT_DENIED
    if not reply.get("ok"):
        print(f"loadctl: {reply.get('error')}: {reply.get('message')}", file=sys.stderr)
        return EXIT_ERROR
    if a.cmd == "stray" and op == "status":
        items = (reply["status"].get("strays") or {}).get("items") or []
        if a.json:
            print(json.dumps(items, indent=2))
        elif not items:
            print("no stray processes")
        for it in items if not a.json else ():
            print(format_stray(it))
    elif op == "stray_kill":
        print(reply["message"])
    elif op == "job_endings":
        rows = reply["endings"]
        print(json.dumps(rows, indent=2) if a.json else format_endings(rows))
    elif a.cmd == "jobs":
        rows = reply["status"].get("job_stats" if a.stats else "jobs") or []
        if a.json:
            print(json.dumps(rows, indent=2))
        else:
            print(format_job_stats(rows) if a.stats else format_jobs(rows))
    elif op == "status":
        s = reply["status"]
        if a.json:
            print(json.dumps(s, indent=2))
        elif a.hwmon:
            print(format_hwmon(s))
        else:
            print(format_status(s))
    elif op == "fleet":
        print(json.dumps(reply["fleet"], indent=2) if a.json else format_fleet(reply["fleet"]))
    elif op == "limits":
        print(json.dumps(reply["limits"], indent=2) if a.json else format_limits(reply["limits"]))
    elif op == "slot_status":
        if a.json:
            print(json.dumps(reply["slots"], indent=2))
        else:
            for name, sl in reply["slots"].items():
                print(format_slot(name, sl))
                for w in format_waiters(sl):
                    print(f"  {w}")
    elif op == "slot_release":
        if reply["released"] and a.fd is not None and not reply.get("nested"):  # the fd may be the holder's
            with contextlib.suppress(OSError):
                fcntl.flock(a.fd, fcntl.LOCK_UN)
        print(
            f"{a.slot} slot released"
            if reply["released"]
            else f"{a.slot} slot was not held for pid {args['pid']}"
        )
    elif op == "freeze":
        word = "would freeze" if reply["would"] else "froze"
        until = f" until {reply['expires_at']}" if reply["expires_at"] else ""
        print(f"{word} {len(reply['frozen'])} tree(s){until}")
    elif op == "unfreeze":
        kept = (
            f"; {len(reply['still_paused'])} guard pause(s) stay until memory recovers"
            if reply["still_paused"]
            else ""
        )
        print(f"unfroze {len(reply['thawed'])} tree(s){kept}")
    elif op == "reap":
        word = "would stop" if reply["would"] else "stopped"
        pids = ", ".join(str(r["pid"]) for r in reply["reaped"])
        print(f"{word} {len(reply['reaped'])} build daemon(s) {pids}")
    elif op == "hold":
        print(f"hold {a.state}: " + "; ".join(format_slot(n, s) for n, s in reply["slots"].items()))
    elif op == "history":
        print(render(reply, a))
    elif op == "log":
        for e in reply["lines"]:
            print(f"{e['ts']} {e['principal']:5} {e['op']} {e['result']}: {e['reason'] or ''}")
    else:
        print(format_reply(op, reply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
