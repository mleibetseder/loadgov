#!/usr/bin/env python3
"""Compare loadgov's observe-mode (WOULD ...) decisions with what the legacy loops really did.

Stdlib only; run as `python3 -I tools/shadow_compare.py ...` (docs/deployment.md, observe mode).
Times are read as UTC. Exit codes: 0 all counts within tolerance, 1 a day/class is outside it, 2 usage
error or no events at all.

Inputs (all optional, give at least one legacy and one shadow source):
  --audit FILE            loadgov audit.jsonl: op=governor (reason "WOULD cap=..."), op=memguard
                          (reason "WOULD PAUSE|RESUME|ABORT|REAP build daemons ...")
  --mem-guard-log FILE    legacy memguard logs, repeatable; the format is detected per line:
                          a guard log   "<ISO>Z PAUSE|RESUME|ABORT ..."
                          a per-episode guard log "<ISO>Z PAUSED|RESUMED ..." (see --episode-project)
                          a reaper log  "YYYY-MM-DD HH:MM:SS LOW MEMORY: ..." (REAP)
  --governor-log FILE     legacy governor log "<ISO>Z cap=2 cores busy=23% (reason)"
  --alerts-log FILE       legacy alert log "ALERT <kind> HH:MM:SS ..." (no date: it is
                          rebuilt backwards from --alerts-end-date, default the file's mtime day)
  --shadow-alerts FILE    loadgov's copy of the alert lines (same format; [compat] alert_log_paths)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})Z?\s+(.*)$")
INFO_ONLY = {
    "REAP orphan",
    "REAP scoped",
}  # shown in the table and the unmatched list, never fail the compare
EPISODE_PAUSE, EPISODE_RESUME = "PAUSE episode", "RESUME episode"
ALERT = re.compile(r"^ALERT (\w+) (\d{2}:\d{2}:\d{2})\b")
KIND = re.compile(r"^(?:WOULD\s+)?(PAUSE|RESUME|ABORT|REAP)\b")
CLOSES_PAUSE = re.compile(
    r"^(?:REAPED orphaned .*(?:: gone|: every target is gone or changed|: giving up after )|RELEASED\b)"
)
CAP = re.compile(r"^(?:WOULD\s+)?cap=")
LEGACY_MEM = {
    "PAUSE": "PAUSE",
    "RESUME": "RESUME",
    "ABORT": "ABORT",
    "PAUSED": EPISODE_PAUSE,
    "RESUMED": EPISODE_RESUME,
}


def parse_ts(day: str, hms: str) -> float:
    return dt.datetime.fromisoformat(f"{day}T{hms}").replace(tzinfo=dt.UTC).timestamp()


def parse_audit(lines, episode_project: str | None = None) -> list[tuple[str, float, str]]:
    """Shadow decisions. The PAUSE/RESUME lines of `episode_project` are collapsed into episodes (a legacy
    guard that logs one PAUSED/RESUMED per episode for all its containers and agents; the shadow logs one
    line per tree)."""
    out = []
    depth = 0  # open episode pauses
    for raw in lines:
        try:
            row = json.loads(raw)
            ts = row["ts"]
            reason = row.get("reason") or ""
            when = parse_ts(ts[:10], ts[11:19])
        except (ValueError, KeyError, TypeError):
            continue
        if episode_project and row.get("project") == episode_project and depth and CLOSES_PAUSE.match(reason):
            depth -= 1  # an orphan reaped or a pause released without a RESUME also closes its open pause
            continue
        if row.get("op") == "governor" and CAP.match(reason):
            out.append(("CAP", when, reason))
        elif row.get("op") == "reap" and KIND.match(reason):
            out.append(("REAP scoped", when, reason))  # request-driven (loadctl reap-daemons, pre-gate)
        elif row.get("op") == "memguard" and (m := KIND.match(reason)):
            kind = m.group(1)
            if kind == "REAP":
                # Only the legacy rule "below 1536 MB, kill all" has a counterpart (legacy memory guard);
                # out-of-scope and pressure reaps, and orphan reaps, are new behaviour.
                if re.match(r"^(?:WOULD\s+)?REAP orphaned", reason):
                    kind = "REAP orphan"
                elif not re.search(r"below 1536 MB", reason):
                    kind = "REAP scoped"
            elif episode_project and row.get("project") == episode_project and kind in ("PAUSE", "RESUME"):
                if kind == "PAUSE":
                    depth += 1
                    if depth > 1:
                        continue
                    kind = EPISODE_PAUSE
                else:
                    if depth == 0:
                        continue
                    depth -= 1
                    if depth > 0:
                        continue
                    kind = EPISODE_RESUME
            out.append((kind, when, reason))
    return out


def parse_legacy_mem(lines) -> list[tuple[str, float, str]]:
    out = []
    for raw in lines:
        m = ISO.match(raw.strip())
        if not m:
            continue
        rest = m.group(3)
        word = rest.split(None, 1)[0] if rest else ""
        kind = LEGACY_MEM.get(word) or ("REAP" if rest.startswith("LOW MEMORY: stopping") else None)
        if kind:
            out.append((kind, parse_ts(m.group(1), m.group(2)), raw.strip()))
    return out


def parse_governor(lines) -> list[tuple[str, float, str]]:
    out = []
    for raw in lines:
        m = ISO.match(raw.strip())
        if m and CAP.match(m.group(3)):
            out.append(("CAP", parse_ts(m.group(1), m.group(2)), raw.strip()))
    return out


def parse_alerts(lines, end_day: dt.date) -> list[tuple[str, float, str]]:
    """Alert lines carry only HH:MM:SS. Walk backwards from the last line, which is on end_day;
    a time later than the next line's time means the previous midnight was crossed."""
    items = []
    for raw in lines:
        m = ALERT.match(raw.strip())
        if m:
            items.append((m.group(1), m.group(2), raw.strip()))
    out = []
    day = end_day
    nxt = None
    for kind, hms, raw in reversed(items):
        if nxt is not None and hms > nxt:
            day -= dt.timedelta(days=1)
        nxt = hms
        out.append((f"ALERT {kind}", parse_ts(day.isoformat(), hms), raw))
    out.reverse()
    return out


def day_of(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")


def match(a: list[float], b: list[float], window: float) -> tuple[list[int], list[int]]:
    """One-to-one greedy match of two sorted time lists; returns the unmatched indexes of each."""
    i = j = 0
    ua, ub = [], []
    while i < len(a) and j < len(b):
        if abs(a[i] - b[j]) <= window:
            i += 1
            j += 1
        elif a[i] < b[j]:
            ua.append(i)
            i += 1
        else:
            ub.append(j)
            j += 1
    ua += range(i, len(a))
    ub += range(j, len(b))
    return ua, ub


def compare(legacy, shadow, window, rel_tol, abs_tol, since=None, until=None):
    """Returns (rows, mismatches, ok). rows: (day, class, n_legacy, n_shadow, n_matched, ok)."""

    def keep(e):
        return (since is None or day_of(e[1]) >= since) and (until is None or day_of(e[1]) <= until)

    legacy = [e for e in legacy if keep(e)]
    shadow = [e for e in shadow if keep(e)]
    rows, mism = [], []
    for cls in sorted({e[0] for e in legacy} | {e[0] for e in shadow}):
        la = sorted(e for e in legacy if e[0] == cls)
        sh = sorted(e for e in shadow if e[0] == cls)
        la_t, sh_t = [e[1] for e in la], [e[1] for e in sh]
        ul, us = match(la_t, sh_t, window)
        mism += [("legacy only", cls, la[i][1], la[i][2]) for i in ul]
        mism += [("shadow only", cls, sh[i][1], sh[i][2]) for i in us]
        for d in sorted({day_of(t) for t in la_t + sh_t}):
            nl = sum(day_of(t) == d for t in la_t)
            ns = sum(day_of(t) == d for t in sh_t)
            matched = nl - sum(day_of(la_t[i]) == d for i in ul)
            ok = cls in INFO_ONLY or abs(nl - ns) <= max(abs_tol, rel_tol * max(nl, ns))
            rows.append((d, cls, nl, ns, matched, ok))
    mism.sort(key=lambda m: m[2])
    return rows, mism, all(r[5] for r in rows)


def read(path):
    with open(path, errors="replace") as f:
        return f.read().splitlines()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shadow_compare.py", description="loadgov shadow vs legacy loops")
    p.add_argument("--audit", help="loadgov audit.jsonl (/var/log/loadgov/audit.jsonl)")
    p.add_argument("--mem-guard-log", action="append", default=[], metavar="FILE")
    p.add_argument("--governor-log", metavar="FILE")
    p.add_argument("--alerts-log", metavar="FILE")
    p.add_argument("--shadow-alerts", metavar="FILE")
    p.add_argument(
        "--alerts-end-date", metavar="YYYY-MM-DD", help="day of the last alert line (default: mtime)"
    )
    p.add_argument(
        "--episode-project",
        metavar="PROJECT",
        help="project whose legacy guard logs one PAUSED/RESUMED per episode (collapses the shadow lines)",
    )
    p.add_argument("--since", metavar="YYYY-MM-DD")
    p.add_argument("--until", metavar="YYYY-MM-DD")
    p.add_argument("--window-s", type=float, default=120.0, help="max time distance of a matched pair")
    p.add_argument("--tolerance", type=float, default=0.2, help="relative count tolerance per day and class")
    p.add_argument("--abs-tolerance", type=int, default=2, help="absolute count tolerance (the larger wins)")
    a = p.parse_args(argv)

    legacy, shadow = [], []
    try:
        if a.audit:
            shadow += parse_audit(read(a.audit), a.episode_project)
        for f in a.mem_guard_log:
            legacy += parse_legacy_mem(read(f))
        if a.governor_log:
            legacy += parse_governor(read(a.governor_log))
        for path, dest in ((a.alerts_log, legacy), (a.shadow_alerts, shadow)):
            if path:
                if a.alerts_end_date:
                    end = dt.date.fromisoformat(a.alerts_end_date)
                else:
                    end = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.UTC).date()
                dest += parse_alerts(read(path), end)
    except (OSError, ValueError) as e:
        print(f"shadow_compare: {e}", file=sys.stderr)
        return 2
    if not legacy and not shadow:
        print("shadow_compare: no events found in the given files", file=sys.stderr)
        return 2

    rows, mism, ok = compare(legacy, shadow, a.window_s, a.tolerance, a.abs_tolerance, a.since, a.until)
    print(f"{'day':<11}{'class':<14}{'legacy':>7}{'shadow':>7}{'matched':>8}  result")
    for d, cls, nl, ns, ml, rok in rows:
        print(f"{d:<11}{cls:<14}{nl:>7}{ns:>7}{ml:>8}  {'ok' if rok else 'MISMATCH'}")
    if mism:
        print(f"\nunmatched events (window {a.window_s:g} s):")
        for side, cls, ts, text in mism:
            print(
                f"  {side:<12}{dt.datetime.fromtimestamp(ts, dt.UTC):%Y-%m-%d %H:%M:%S} {cls}: {text[:100]}"
            )
    print(
        f"\nRESULT {'PASS' if ok else 'FAIL'} "
        f"(tolerance {a.tolerance:g} relative / {a.abs_tolerance} absolute per day and class)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
