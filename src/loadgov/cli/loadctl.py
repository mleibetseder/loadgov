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
from loadgov.protocol import DEFAULT_SOCKET

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
        f"impl {e['max_implementers']}, ram floor {e['ram_floor_gb']:g} GB"
    )


def format_status(s: dict) -> str:
    lines = [format_hwmon(s).replace("hwmon ", "loadgov ", 1)]
    c, m, io = s["cpu"], s["memory"], s["io"]
    lines.append(
        f"  psi   cpu {c['psi_some_avg10']:.0f}/{c['psi_some_avg60']:.0f}  "
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
            + (f", last {bs['last_event']}" if bs["last_event"] else "")
        )
    for name, sl in s.get("slots", {}).items():
        lines.append(f"  slot  {format_slot(name, sl)}")
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
    return "\n".join(lines)


def format_slot(name: str, sl: dict) -> str:
    if sl["state"] == "free":
        out = f"{name}: free"
    else:
        who = sl["holder"] or "unknown"
        out = f"{name}: {sl['state']} by {who} [{sl['source']}] since {sl['held_since'] or '?'}"
    out += f", {sl['queue']} waiting" if sl["queue"] else ""
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
    se = sub.add_parser("set", help="change the profile or single limits")
    se.add_argument("--profile")
    se.add_argument("--max-cores", type=float, dest="max_cores")
    se.add_argument("--target-cpu", type=float, dest="target_cpu_pct")
    se.add_argument("--load-max", type=float, dest="load_max")
    se.add_argument("--max-impl", type=int, dest="max_implementers")
    se.add_argument("--ram-floor-gb", type=float, dest="ram_floor_gb")
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
    add_history_parser(sub)
    lg = sub.add_parser("log", help="last audit lines")
    lg.add_argument("-n", type=int, default=20)
    return ap


OVERRIDE_KEYS = ("load1_max", "load5_max", "psi_max", "mem_min_gb", "swap_max_mb", "mem_psi_max")


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
            print(f"{tag}_SLOT_OK {who} waited={waited}s{note}", file=out)
            return 0
        if reply["status"] == "timeout" or not a.wait or (a.timeout_s and waited >= a.timeout_s):
            print(
                f"{tag}_SLOT_WAIT {who}: held by {reply.get('holder', 'unknown')} waited={waited}s",
                file=sys.stderr,
            )
            if a.wait:  # leave the queue now rather than after the daemon notices
                with contextlib.suppress(DaemonUnavailable):
                    request(a.socket, "slot_release", {"slot": a.slot, "pid": pid})
            return 1
        if waited >= next_note:
            print(
                f"{tag}_SLOT_WAIT {who}: held by {reply.get('holder', 'unknown')} waited={waited}s",
                file=sys.stderr,
            )
            next_note = waited + SLOT_NOTE_S
        (sleep or time.sleep)(SLOT_POLL_S)


def headroom_state(reply: dict) -> str:
    """The state text of wait-headroom.sh's HEADROOM_* lines."""
    d, th = reply["decision"], reply["thresholds"]
    v = d["values"]
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
        r = request(a.socket, "headroom", {**args, **({"overrides": over} if over else {})})
        if r.get("error") == "not_ready":
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
        keys = ("profile", "max_cores", "target_cpu_pct", "load_max", "max_implementers", "ram_floor_gb")
        args = {k: getattr(a, k) for k in (*keys, "ttl", "until", "reason", "project")}
        args["owner"] = a.owner
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
    if op == "status":
        s = reply["status"]
        if a.json:
            print(json.dumps(s, indent=2))
        elif a.hwmon:
            print(format_hwmon(s))
        else:
            print(format_status(s))
    elif op == "limits":
        print(json.dumps(reply["limits"], indent=2) if a.json else format_limits(reply["limits"]))
    elif op == "slot_status":
        if a.json:
            print(json.dumps(reply["slots"], indent=2))
        else:
            for name, sl in reply["slots"].items():
                print(format_slot(name, sl))
    elif op == "slot_release":
        if reply["released"] and a.fd is not None:
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
