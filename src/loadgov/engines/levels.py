"""The legacy monitor's OK / BUSY / LIMIT levels (its limits, unchanged)."""

from __future__ import annotations

from loadgov.config import DiskThreshold

# (busy, limit). For ram lower is worse. The disk levels are configurable ([levels] disk_busy, disk_limit).
LIMITS = {
    "cpu_pct": (85, 97),
    "cpu_psi60": (40, 70),
    "load1_per_core": (1.5, 2.5),
    "ram_avail_mb": (6144, 3584),
    "swap_used_pct": (85, 95),
}
DISK_BUSY, DISK_LIMIT = DiskThreshold(25.0), DiskThreshold(10.0)  # GB free, the defaults
ORDER = {"OK": 0, "BUSY": 1, "LIMIT": 2}


def level(value: float, busy: float, limit: float) -> str:
    if busy > limit:  # lower is worse
        return "LIMIT" if value <= limit else "BUSY" if value <= busy else "OK"
    return "LIMIT" if value >= limit else "BUSY" if value >= busy else "OK"


def _crossed(p: dict, thr: DiskThreshold, inclusive: bool) -> bool:
    if thr.pct:
        return p["used_pct"] is not None and p["used_pct"] >= thr.value
    return p["free_gb"] <= thr.value if inclusive else p["free_gb"] < thr.value


def _paths(d: dict) -> list[dict]:
    """The per-path samples; a bare `{free_gb}` (sizes unknown) counts as one path without a percentage."""
    return d.get("per_path") or [{"path": "/", "free_gb": d["free_gb"], "total_gb": None, "used_pct": None}]


def disk_check(
    d: dict, busy: DiskThreshold = DISK_BUSY, limit: DiskThreshold = DISK_LIMIT, inclusive: bool = True
) -> tuple[str, dict, str | None]:
    """The disk level: the worst over the watched paths (ties: the least free space).

    Returns (level, that path's sample, why). A path is LIMIT / BUSY when its free GB or its used percent
    crosses the threshold. `inclusive`: a GB threshold also counts when free space equals it (the status
    level, as before); the alert engine passes False (strictly below). Percent always counts at `>=`.
    """
    best = None
    for p in _paths(d):
        if _crossed(p, limit, inclusive):
            lvl = "LIMIT"
        elif _crossed(p, busy, inclusive):
            lvl = "BUSY"
        else:
            lvl = "OK"
        if best is None or (ORDER[lvl], -p["free_gb"]) > (ORDER[best[0]], -best[1]["free_gb"]):
            best = (lvl, p)
    lvl, p = best
    thr = limit if lvl == "LIMIT" else busy
    why = None
    if lvl != "OK":
        cmp = "<=" if inclusive else "<"
        why = (
            f"disk {p['path']} {p['used_pct']:g} % used >= {thr}"
            if thr.pct
            else f"disk {p['path']} {p['free_gb']:g} GB free {cmp} {thr}"
        )
    return lvl, p, why


def worst(*levels: str) -> str:
    return max(levels, key=ORDER.__getitem__)


def effective_psi(raw: float, busy_pct: float, gate_pct: float) -> float:
    """CPU pressure that counts: the raw value when the CPU is busy (busy % >= gate), else 0.

    Throttled cgroups (a docker `--cpus` cap, loadgov's own cpu.max) raise CPU PSI while cores sit idle.
    gate 0 always counts the raw value. Monotonic in busy %: a busier host never counts less.
    """
    return raw if busy_pct >= gate_pct else 0.0


def evaluate(
    s: dict, disk_busy: DiskThreshold = DISK_BUSY, disk_limit: DiskThreshold = DISK_LIMIT
) -> tuple[dict[str, str], str]:
    values = {
        "cpu_pct": s["cpu"]["busy_pct"],
        "cpu_psi60": s["cpu"]["psi_effective"],
        "load1_per_core": s["cpu"]["load1_per_core"],
        "ram_avail_mb": s["memory"]["avail_mb"],
        "swap_used_pct": s["swap"]["used_pct"],
    }
    lv = {k: level(values[k], *LIMITS[k]) for k in LIMITS}
    cpu = worst(lv["cpu_pct"], lv["cpu_psi60"], lv["load1_per_core"])
    # CPU counts as LIMIT only when the share and the pressure agree; a short spike is BUSY.
    if cpu == "LIMIT" and not (lv["cpu_psi60"] == "LIMIT" or lv["load1_per_core"] == "LIMIT"):
        cpu = "BUSY"
    # `disk` and the overall level always use the built-in GB rule: start paths (`--headroom level`,
    # level-wait, hwmon gate) read them, and disk must never delay a start. The configured thresholds
    # only drive `disk_alert` (and the alert engine); it is not part of the overall level.
    levels = {
        "cpu": cpu,
        "ram": lv["ram_avail_mb"],
        "swap": lv["swap_used_pct"],
        "disk": disk_check(s["disk"])[0],
    }
    overall = worst(*levels.values())
    levels["disk_alert"] = disk_check(s["disk"], disk_busy, disk_limit)[0]
    return levels, overall
