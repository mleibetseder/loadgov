"""Rolling per-(project, kind) summary of what finished jobs used (see engines/usage.py).

Counts and totals cover every job since the first record; percentiles use the last `KEEP` jobs of the
pair. A value that was unknown for a job (scope gone, no `memory.peak`) is left out of that statistic.
"""

from __future__ import annotations

from collections import deque

KEEP = 200
MAX_PAIRS = 64  # per-(project, kind) summaries kept; the least recently updated is evicted beyond that
FIELDS = ("duration_s", "cpu_s", "docker_cpu_s", "host_cpu_s", "mem_peak_mb")


def pct(vals: list[float], q: float) -> float | None:
    """Percentile by linear interpolation (q in 0..1); None for no values."""
    if not vals:
        return None
    v = sorted(vals)
    pos = (len(v) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (pos - lo), 3)


class JobStats:
    def __init__(self):
        self.pairs: dict[tuple[str, str], dict] = {}

    def record(self, project: str, kind: str, rec: dict) -> None:
        p = self.pairs.pop((project, kind), None) or (
            {"count": 0, "duration_sum_s": 0.0, "cpu_total_s": 0.0, "docker_cpu_total_s": 0.0,
             "mem_peak_max_mb": None, "recent": deque(maxlen=KEEP)}
        )  # fmt: skip
        self.pairs[(project, kind)] = p  # most recently updated last
        while len(self.pairs) > MAX_PAIRS:
            del self.pairs[next(iter(self.pairs))]
        p["count"] += 1
        p["duration_sum_s"] += rec.get("duration_s") or 0.0
        p["cpu_total_s"] += rec.get("cpu_s") or 0.0
        p["docker_cpu_total_s"] += rec.get("docker_cpu_s") or 0.0
        peak = rec.get("mem_peak_mb")
        if peak is not None and (p["mem_peak_max_mb"] is None or peak > p["mem_peak_max_mb"]):
            p["mem_peak_max_mb"] = peak
        p["recent"].append([rec.get(f) for f in FIELDS])

    def expected_s(self, project: str, kind: str) -> float | None:
        """Median duration of the recent finished jobs of the pair (`duration_p50_s`), None when unknown."""
        p = self.pairs.get((project, kind))
        return pct([r[0] for r in p["recent"] if r[0] is not None], 0.5) if p else None

    def summary(self) -> list[dict]:
        out = []
        for (project, kind), p in sorted(self.pairs.items()):
            col = {f: [r[i] for r in p["recent"] if r[i] is not None] for i, f in enumerate(FIELDS)}
            total = [r[1] + r[2] for r in p["recent"] if r[1] is not None and r[2] is not None]
            out.append(
                {
                    "project": project,
                    "kind": kind,
                    "count": p["count"],
                    "duration_sum_s": round(p["duration_sum_s"], 1),
                    "duration_p50_s": pct(col["duration_s"], 0.5),
                    "cpu_total_s": round(p["cpu_total_s"], 3),
                    "cpu_p50_s": pct(col["cpu_s"], 0.5),
                    "cpu_p95_s": pct(col["cpu_s"], 0.95),
                    "docker_cpu_total_s": round(p["docker_cpu_total_s"], 3),
                    "docker_cpu_p50_s": pct(col["docker_cpu_s"], 0.5),
                    "docker_cpu_p95_s": pct(col["docker_cpu_s"], 0.95),
                    "total_cpu_p50_s": pct(total, 0.5),
                    "total_cpu_p95_s": pct(total, 0.95),
                    "host_cpu_p50_s": pct(col["host_cpu_s"], 0.5),
                    "mem_peak_p50_mb": pct(col["mem_peak_mb"], 0.5),
                    "mem_peak_p95_mb": pct(col["mem_peak_mb"], 0.95),
                    "mem_peak_max_mb": p["mem_peak_max_mb"],
                }
            )
        return out

    def to_state(self) -> list[dict]:
        return [
            {
                "project": pr,
                "kind": k,
                **{x: v for x, v in p.items() if x != "recent"},
                "recent": list(p["recent"]),
            }
            for (pr, k), p in self.pairs.items()
        ]

    def restore(self, raw) -> None:
        for item in raw if isinstance(raw, list) else []:
            try:
                recent = [[None if x is None else float(x) for x in r[: len(FIELDS)]] for r in item["recent"]]
                mx = item["mem_peak_max_mb"]
                self.pairs[(str(item["project"]), str(item["kind"]))] = {
                    "count": int(item["count"]),
                    "duration_sum_s": float(item["duration_sum_s"]),
                    "cpu_total_s": float(item["cpu_total_s"]),
                    "docker_cpu_total_s": float(item["docker_cpu_total_s"]),
                    "mem_peak_max_mb": None if mx is None else float(mx),
                    "recent": deque((r for r in recent if len(r) == len(FIELDS)), maxlen=KEEP),
                }
            except (KeyError, TypeError, ValueError, IndexError):
                continue  # an unreadable entry starts again; the rest is kept
        while len(self.pairs) > MAX_PAIRS:
            del self.pairs[next(iter(self.pairs))]
