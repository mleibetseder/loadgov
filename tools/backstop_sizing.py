#!/usr/bin/env python3
"""Suggest [backstop] values from the daemon's sample history (stdlib only, read-only).

    loadctl history --all --csv | python3 tools/backstop_sizing.py -
    python3 tools/backstop_sizing.py /var/lib/loadgov/history.csv [--days 7] [--margin-pct 20]

Reads the `loadctl history --csv` / `history.csv` format (columns `at`, `bw_mem_mb`, `bw_swap_mb`,
`bw_peak_mb`; docs/protocol.md "Sample history"), prints the peak build-work.slice memory.current /
memory.swap.current over the window and a suggested value for each: peak + max(1024 MB, margin %), rounded
up to 256 MB. memory_max_mb is never suggested above MemTotal - 2 GB nor below 4096 MB (the config refuses
less). Exit 0 with a suggestion, 2 when there is no usable data. It writes nothing and changes no cgroup.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import sys
import time

MIN_MAX_MB = 4096
RESERVE_MB = 2048
ROUND_MB = 256
MIN_MARGIN_MB = 1024
DEFAULT_PATH = "/var/lib/loadgov/history.csv"


def parse_at(text: str) -> float | None:
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def window(rows: list[dict], days: float) -> list[dict]:
    """The rows within `days` of the newest row (rows with a bad timestamp are dropped)."""
    stamped = [(parse_at(r.get("at", "")), r) for r in rows]
    stamped = [(t, r) for t, r in stamped if t is not None]
    if not stamped:
        return []
    cutoff = max(t for t, _ in stamped) - days * 86400
    return [r for t, r in stamped if t >= cutoff]


def peaks(rows: list[dict]) -> tuple[float, float]:
    """Peak memory MB (max of memory.current and memory.peak) and peak swap MB over the rows."""
    mem = [v for r in rows for k in ("bw_mem_mb", "bw_peak_mb") if (v := num(r.get(k))) is not None]
    swap = [v for r in rows if (v := num(r.get("bw_swap_mb"))) is not None]
    return max(mem, default=0.0), max(swap, default=0.0)


def suggest(peak_mb: float, margin_pct: float) -> int:
    value = peak_mb + max(MIN_MARGIN_MB, peak_mb * margin_pct / 100)
    return int(-(-value // ROUND_MB) * ROUND_MB)


def mem_total_mb(meminfo: str = "/proc/meminfo") -> int:
    with open(meminfo) as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    raise OSError("no MemTotal in " + meminfo)


def main(argv: list[str] | None = None, total_mb: int | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", default=DEFAULT_PATH, help="history CSV, or - for stdin")
    ap.add_argument("--days", type=float, default=7.0, help="window ending at the newest row (default 7)")
    ap.add_argument(
        "--margin-pct", type=float, default=20.0, help="margin over the peak (default 20, min 1 GB)"
    )
    a = ap.parse_args(argv)
    try:
        text = sys.stdin.read() if a.csv == "-" else open(a.csv).read()  # noqa: SIM115
    except OSError as e:
        print(f"backstop_sizing: cannot read {a.csv}: {e.strerror}", file=sys.stderr)
        return 2
    rows = window(list(csv.DictReader(io.StringIO(text))), a.days)
    used = [r for r in rows if num(r.get("bw_mem_mb")) is not None or num(r.get("bw_peak_mb")) is not None]
    if not used:
        print(
            "backstop_sizing: no rows with build-work memory readings (is build-work.slice present?)",
            file=sys.stderr,
        )
        return 2
    first, last = rows[0]["at"], rows[-1]["at"]
    span_d = (parse_at(last) - parse_at(first)) / 86400  # type: ignore[operator]
    peak_mem, peak_swap = peaks(used)
    total = total_mb if total_mb is not None else mem_total_mb()
    ceiling = total - RESERVE_MB
    mem = max(MIN_MAX_MB, min(suggest(peak_mem, a.margin_pct), ceiling))
    swap = suggest(peak_swap, a.margin_pct)
    print(f"window      {first} .. {last}  ({span_d:.1f} days, {len(rows)} rows)")
    print(f"peak        memory {peak_mem:.0f} MB (memory.current / memory.peak), swap {peak_swap:.0f} MB")
    print(f"MemTotal    {total} MB; memory_max_mb never above {ceiling} MB (MemTotal - 2 GB)")
    if span_d < a.days - 0.5:
        print(f"WARNING     only {span_d:.1f} days of history; wait for {a.days:g} days and a full gate day")
    if peak_mem + MIN_MARGIN_MB > ceiling:
        print(f"WARNING     peak + 1 GB exceeds {ceiling} MB: the suggestion is capped and may be below a "
              "legitimate peak; fix the workload or choose by hand")  # fmt: skip
    print()
    print("[backstop]")
    print(f"# source: backstop_sizing.py over {first[:10]}..{last[:10]}, peak build-work memory.current "
          f"{peak_mem:.0f} MB / swap {peak_swap:.0f} MB, margin max(1 GB, {a.margin_pct:g} %)")  # fmt: skip
    print("enabled = true")
    print(f"memory_max_mb = {mem}")
    print(f"memory_swap_max_mb = {swap}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
