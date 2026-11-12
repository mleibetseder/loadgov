"""`loadctl history [n] [--csv|--json]`: the newest samples of the daemon's ring buffer (history.csv)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from loadgov.engines.history import COLUMNS, KEEP, SOCKET_MAX, History


def add_history_parser(sub) -> None:
    hi = sub.add_parser("history", help="last n samples of the 15 s history (default 40; about 7 days kept)")
    hi.add_argument("n", nargs="?", type=int, default=40)
    hi.add_argument("--all", action="store_true", help="every kept row (reads the file, not the socket)")
    hi.add_argument(
        "--state-dir", default="/var/lib/loadgov", help="where history.csv is read from for n > 5760"
    )
    g = hi.add_mutually_exclusive_group()
    g.add_argument("--csv", action="store_true", help="the history.csv rows with their header (hwmon style)")
    g.add_argument("--json", action="store_true", help="a list of objects, numbers as numbers")


def _typed(v: str):
    if v == "":
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def render(reply: dict, args: argparse.Namespace) -> str:
    cols, rows = reply["columns"], reply["rows"]
    if args.json:
        return json.dumps([{c: _typed(v) for c, v in zip(cols, r, strict=True)} for r in rows], indent=2)
    if args.csv:
        return "\n".join(",".join(r) for r in [cols, *rows])
    table = [cols, *rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(cols))]
    return "\n".join("  ".join(v.rjust(w) for v, w in zip(r, widths, strict=True)) for r in table)


def wants_file(a: argparse.Namespace) -> bool:
    """More than the socket op returns (one day): read history.csv directly instead, read-only."""
    return a.all or a.n > SOCKET_MAX


def from_file(a: argparse.Namespace) -> int:
    path = os.path.join(a.state_dir, "history.csv")
    if not os.path.exists(path):
        print(f"loadctl: no history file at {path} (use --state-dir)", file=sys.stderr)
        return 5
    rows = History(path, time.time).tail(KEEP if a.all else a.n)
    print(render({"columns": list(COLUMNS), "rows": rows}, a))
    return 0
