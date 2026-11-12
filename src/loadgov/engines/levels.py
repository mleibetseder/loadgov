"""The legacy monitor's OK / BUSY / LIMIT levels (its limits, unchanged)."""

from __future__ import annotations

# (busy, limit). For ram and disk lower is worse.
LIMITS = {
    "cpu_pct": (85, 97),
    "cpu_psi60": (40, 70),
    "load1_per_core": (1.5, 2.5),
    "ram_avail_mb": (6144, 3584),
    "swap_used_pct": (85, 95),
    "disk_free_gb": (25, 10),
}
ORDER = {"OK": 0, "BUSY": 1, "LIMIT": 2}


def level(value: float, busy: float, limit: float) -> str:
    if busy > limit:  # lower is worse
        return "LIMIT" if value <= limit else "BUSY" if value <= busy else "OK"
    return "LIMIT" if value >= limit else "BUSY" if value >= busy else "OK"


def worst(*levels: str) -> str:
    return max(levels, key=ORDER.__getitem__)


def evaluate(s: dict) -> tuple[dict[str, str], str]:
    values = {
        "cpu_pct": s["cpu"]["busy_pct"],
        "cpu_psi60": s["cpu"]["psi_some_avg60"],
        "load1_per_core": s["cpu"]["load1_per_core"],
        "ram_avail_mb": s["memory"]["avail_mb"],
        "swap_used_pct": s["swap"]["used_pct"],
        "disk_free_gb": s["disk"]["free_gb"],
    }
    lv = {k: level(values[k], *LIMITS[k]) for k in LIMITS}
    cpu = worst(lv["cpu_pct"], lv["cpu_psi60"], lv["load1_per_core"])
    # CPU counts as LIMIT only when the share and the pressure agree; a short spike is BUSY.
    if cpu == "LIMIT" and not (lv["cpu_psi60"] == "LIMIT" or lv["load1_per_core"] == "LIMIT"):
        cpu = "BUSY"
    levels = {
        "cpu": cpu,
        "ram": lv["ram_avail_mb"],
        "swap": lv["swap_used_pct"],
        "disk": lv["disk_free_gb"],
    }
    return levels, worst(*levels.values())
