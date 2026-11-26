"""Rolling per-(project, kind) summary of what finished jobs used (see engines/usage.py).

Counts and totals cover every job since the first record; percentiles use the last `KEEP` jobs of the
pair. A value that was unknown for a job (scope gone, no `memory.peak`) is left out of that statistic.
"""

from __future__ import annotations

from collections import OrderedDict, deque

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

    def record(self, project: str, kind: str, rec: dict, cause: str | None = None) -> None:
        p = self.pairs.pop((project, kind), None) or (
            {"count": 0, "duration_sum_s": 0.0, "cpu_total_s": 0.0, "docker_cpu_total_s": 0.0,
             "mem_peak_max_mb": None, "recent": deque(maxlen=KEEP), "causes": {}}
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
        if cause:
            p["causes"][cause] = p["causes"].get(cause, 0) + 1

    def recount(self, project: str, kind: str, old: str, new: str) -> None:
        """A late `job_end` report replaced the cause of an ending already counted."""
        c = (self.pairs.get((project, kind)) or {}).get("causes")
        if c is not None and c.get(old):
            c[old] -= 1
            c[new] = c.get(new, 0) + 1

    def expected_s(self, project: str, kind: str) -> float | None:
        """Median duration of the recent finished jobs of the pair (`duration_p50_s`), None when unknown."""
        p = self.pairs.get((project, kind))
        return pct([r[0] for r in p["recent"] if r[0] is not None], 0.5) if p else None

    def mem_peak_pct(self, project: str, kind: str, q: float) -> float | None:
        """Percentile (q in 0..1) of the recent memory peaks of the pair, None when unknown."""
        p = self.pairs.get((project, kind))
        return pct([r[4] for r in p["recent"] if r[4] is not None], q) if p else None

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
                    "causes": {k: v for k, v in sorted(p["causes"].items()) if v},
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
                    "causes": {
                        str(k): int(v) for k, v in (item.get("causes") or {}).items() if int(v) > 0
                    },  # absent in an older state file
                }
            except (KeyError, TypeError, ValueError, IndexError):
                continue  # an unreadable entry starts again; the rest is kept
        while len(self.pairs) > MAX_PAIRS:
            del self.pairs[next(iter(self.pairs))]


HIST_CAP = 50  # peaks kept per label ([compile] history picks how many of them count, at most this)
MAX_LABELS = 256  # labels kept per project; the least recently finished is evicted beyond that
MAX_PEAK_PROJECTS = 64


class PeakBook:
    """Memory peaks (MB) of finished compile jobs per (project, label = crate): the last `HIST_CAP` per label,
    at most `MAX_LABELS` labels per project and `MAX_PEAK_PROJECTS` projects (least recently updated out).
    Separate from `JobStats`: its own additive block in the saved state (`compile_peaks`)."""

    def __init__(self):
        self.book: OrderedDict[str, OrderedDict[str, deque]] = OrderedDict()

    def record(self, project: str, label: str, peak_mb: float | None, incomplete: bool = False) -> None:
        """`incomplete`: the job was stopped before its real peak, so `peak_mb` is only a lower bound. It is
        kept only when it is above every peak already kept for the label (so it can only raise the estimate);
        a label without history learns nothing from it."""
        if peak_mb is None or peak_mb < 0:
            return  # a job whose scope gave no memory.peak teaches nothing
        if incomplete:
            known = self.book.get(project, {}).get(label)
            if not known or peak_mb <= max(known):
                return
        labels = self.book.pop(project, None) or OrderedDict()
        self.book[project] = labels  # most recently updated last
        while len(self.book) > MAX_PEAK_PROJECTS:
            self.book.popitem(last=False)
        peaks = labels.pop(label, None) or deque(maxlen=HIST_CAP)
        labels[label] = peaks
        while len(labels) > MAX_LABELS:
            labels.popitem(last=False)
        peaks.append(float(peak_mb))

    def expected(self, project: str, label: str, history: int, q: float) -> float | None:
        """Percentile q (0..1, interpolated) of the last `history` peaks of the label, None if none."""
        peaks = self.book.get(project, {}).get(label)
        return pct(list(peaks)[-history:], q) if peaks else None

    def to_state(self) -> dict:
        return {pr: {lb: list(pk) for lb, pk in labels.items()} for pr, labels in self.book.items()}

    def restore(self, raw) -> None:
        for project, labels in raw.items() if isinstance(raw, dict) else ():
            if not isinstance(project, str) or not isinstance(labels, dict):
                continue
            for label, peaks in labels.items():
                if isinstance(label, str) and isinstance(peaks, list):
                    for v in peaks:
                        if isinstance(v, int | float) and not isinstance(v, bool):
                            self.record(project, label, v)
