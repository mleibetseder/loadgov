"""Compat shim: /run/lock/load-governor.env for the legacy shell scripts to `source` on every tick."""

from __future__ import annotations

from loadgov.daemon.state import atomic_write_text
from loadgov.engines.limits import iso


def _num(v) -> str:
    return f"{v:g}"


def render_env(effective: dict, profile: str, ceiling: str | None, now: float) -> str:
    e = effective
    pairs = [
        ("MAX_CORES", _num(e["max_cores"])),
        ("TARGET_PCT", _num(e["target_cpu_pct"])),
        ("LOAD_MAX", _num(e["load_max"])),
        ("LOAD_HOLD", _num(e["load_hold"])),
        ("IMPL_MAX", _num(e["max_implementers"])),
        ("IMPL_MAX_QUIET", _num(e["impl_max_quiet"])),
        ("MEM_MIN_GB", _num(e["ram_floor_gb"])),
        ("LOW_GB", _num(e["ram_floor_gb"])),
        ("CARGO_BUILD_JOBS", _num(e["cargo_build_jobs"])),
        ("VITEST_MAX_WORKERS", _num(e["vitest_max_workers"])),
        ("GATE_SHARD_BUDGET", _num(e["gate_shard_budget"])),
        ("PROFILE", profile),
        ("OWNER_CEILING", ceiling or ""),
        ("UPDATED_AT", iso(now)),
    ]
    return "".join(f"{k}={v}\n" for k, v in pairs)


def write_env(path: str, text: str) -> None:
    atomic_write_text(path, text)
