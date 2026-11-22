"""Headroom decision: does the host have room for one more heavy job? Pure: sample + thresholds in."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from loadgov.config import Thresholds
from loadgov.host.procfs import HostFS

HOLD_KINDS = ("gate", "compile")  # an owner `hold` stops new heavy work, not implementers/reviewers


@dataclass(frozen=True)
class Decision:
    passed: bool
    reasons: list[str]
    would_block_io: bool  # io PSI is over its limit (blocks only when io_psi_gate is on)
    values: dict
    paused: list[str] = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)  # the applied (profile-scaled) thresholds
    factor: dict = field(default_factory=dict)
    reason: str = ""  # one line naming the lane that decided (review lane only; "" for the load lane)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pass"] = d.pop("passed")
        return d


def paused_reasons(
    fs: HostFS, kind: str, hold: str | None, freeze: str | None, marker: str | None = None
) -> list[str]:
    """`marker`: the host-wide pause file name ([memguard.pause_marker]) written by a host guard."""
    out = []
    if marker and fs.read_lock_file(marker):
        out.append(f"host guard pause file {fs.lock_path(marker)}")
    if freeze:
        out.append(f"owner freeze: {freeze}")
    if hold and kind in HOLD_KINDS:
        out.append(f"owner hold: {hold}")
    return out


def profile_factor(effective: dict, default: dict) -> dict:
    """Load thresholds scale with load_max against the default profile; mem_min rises with ram_floor_gb."""
    return {
        "load": effective["load_max"] / default["load_max"],
        "mem_add_gb": max(0.0, effective["ram_floor_gb"] - default["ram_floor_gb"]),
    }


def scale(th: Thresholds, factor: dict) -> Thresholds:
    return replace(
        th,
        load1_max=th.load1_max * factor["load"],
        load5_max=None if th.load5_max is None else th.load5_max * factor["load"],
        mem_min_gb=th.mem_min_gb + factor["mem_add_gb"],
    )


def decide(
    sample: dict,
    th: Thresholds,
    *,
    factor: dict | None = None,
    io_psi_gate: bool = False,
    io_psi_max: float = 25.0,
    paused: list[str] | None = None,
) -> Decision:
    """All comparisons are strict like wait-headroom.sh; memory is MemAvailable/1024 GB like the scripts."""
    factor = factor or {"load": 1.0, "mem_add_gb": 0.0}
    cpus = sample["host"]["cores"]
    bad = [  # e.g. "nproc-2" on a 2-core host: refuse to start work rather than crash or compare to <= 0
        f"misconfigured: {k} resolves to {x:g} on {cpus} CPUs"
        for k, x in (("load1_max", th.load1_max), ("load5_max", th.load5_max))
        if x is not None and x <= 0
    ]
    th = scale(th, factor)
    paused = paused or []
    v = {
        "load1": sample["cpu"]["load1"],
        "load5": sample["cpu"]["load5"],
        "cpu_psi": sample["cpu"]["psi_effective"],
        "mem_gb": sample["memory"]["avail_mb"] / 1024,
        "swap_mb": sample["swap"]["used_mb"],
        "swap_in_rate": sample["swap"].get("in_rate", 0.0),
        "mem_psi": sample["memory"]["psi_some_avg60"],
        "io_psi": sample["io"]["psi_some_avg60"],
        "cpus": sample["host"]["cores"],
    }
    why = list(bad)
    if not v["load1"] < th.load1_max:
        why.append(f"load1 {v['load1']:.2f} >= {th.load1_max:.2f}")
    if th.load5_max is not None and not v["load5"] < th.load5_max:
        why.append(f"load5 {v['load5']:.2f} >= {th.load5_max:.2f}")
    if not v["cpu_psi"] < th.psi_max:
        why.append(f"cpu psi {v['cpu_psi']:.1f}% >= {th.psi_max:g}%")
    if not v["mem_gb"] >= th.mem_min_gb:
        why.append(f"mem available {v['mem_gb']:.1f} GB < {th.mem_min_gb:g} GB")
    # Old swap alone is no reason to wait: it blocks only while pages are actively swapped back in.
    if (
        v["swap_mb"] > th.swap_max_mb
        and v["swap_in_rate"] > th.swap_in_max
        and v["mem_gb"] < 2 * th.mem_min_gb
    ):
        why.append(
            f"swap used {v['swap_mb']} MB > {th.swap_max_mb:g} MB and swapping in "
            f"{v['swap_in_rate']:.0f} pages/s > {th.swap_in_max:g} pages/s"
        )
    if not v["mem_psi"] < th.mem_psi_max:
        why.append(f"mem psi {v['mem_psi']:.1f}% >= {th.mem_psi_max:g}%")
    why += [f"paused: {p}" for p in paused]
    over_io = not v["io_psi"] < io_psi_max
    if io_psi_gate and over_io:
        why.append(f"io psi {v['io_psi']:.1f}% >= {io_psi_max:g}%")
    return Decision(
        not why,
        why,
        over_io,
        {k: round(x, 2) if isinstance(x, float) else x for k, x in v.items()},
        paused,
        asdict(th) | {"io_psi_max": io_psi_max, "io_psi_gate": io_psi_gate},
        {k: round(x, 4) for k, x in factor.items()},
    )


def decide_review(
    sample: dict,
    th: Thresholds,
    *,
    running: int,
    max_reviewers: int,
    ram_floor_gb: float = 0.0,
    guard_paused: bool = False,
    factor: dict | None = None,
    io_psi_gate: bool = False,
    io_psi_max: float = 25.0,
    paused: list[str] | None = None,
) -> Decision:
    """The review lane: a review reads code and makes little CPU load, so load, PSI and swap do not apply.
    Only the memory floor and the parallel cap (`running` registered review jobs < `max_reviewers`) do.
    The floor is at least the profile's `ram_floor_gb` (the memory guard's low level), and no review starts
    while the memory guard is pausing work (`guard_paused`)."""
    factor = factor or {"load": 1.0, "mem_add_gb": 0.0}
    th = scale(th, factor)
    th = replace(th, mem_min_gb=max(th.mem_min_gb, ram_floor_gb))
    paused = paused or []
    mem_gb = sample["memory"]["avail_mb"] / 1024
    v = {
        "load1": sample["cpu"]["load1"],
        "load5": sample["cpu"]["load5"],
        "cpu_psi": sample["cpu"]["psi_some_avg60"],
        "mem_gb": mem_gb,
        "swap_mb": sample["swap"]["used_mb"],
        "mem_psi": sample["memory"]["psi_some_avg60"],
        "io_psi": sample["io"]["psi_some_avg60"],
        "cpus": sample["host"]["cores"],
        "reviewers": running,
        "max_reviewers": max_reviewers,
    }
    why = []
    if not running < max_reviewers:
        why.append(f"review lane: {running} of {max_reviewers} reviewers running")
    if not mem_gb >= th.mem_min_gb:
        why.append(f"mem available {mem_gb:.1f} GB < {th.mem_min_gb:g} GB")
    if guard_paused:
        why.append("memory guard is pausing work")
    why += [f"paused: {p}" for p in paused]
    over_io = not v["io_psi"] < io_psi_max
    return Decision(
        not why,
        why,
        over_io,
        {k: round(x, 2) if isinstance(x, float) else x for k, x in v.items()},
        paused,
        asdict(th) | {"io_psi_max": io_psi_max, "io_psi_gate": io_psi_gate},
        {k: round(x, 4) for k, x in factor.items()},
        f"review lane: {running} of {max_reviewers} reviewers running, {mem_gb:.1f} GB free",
    )


def decide_light(
    sample: dict,
    th: Thresholds,
    *,
    project: str,
    running: int,
    project_running: int,
    max_light: int,
    max_per_project: int,
    ram_floor_gb: float = 0.0,
    guard_paused: bool = False,
    factor: dict | None = None,
    io_psi_gate: bool = False,
    io_psi_max: float = 25.0,
    paused: list[str] | None = None,
) -> Decision:
    """The light lane: small read-only checks (lint, formatters in check mode) under hard scope limits.
    No heavy or compile slot and no load, PSI or swap checks. Passes on the memory floor (at least the
    profile's `ram_floor_gb`), no pause, `running` registered light jobs < `max_light` host-wide and
    `project_running` < `max_per_project` for this project. An owner `hold` does not stop it."""
    factor = factor or {"load": 1.0, "mem_add_gb": 0.0}
    th = scale(th, factor)
    th = replace(th, mem_min_gb=max(th.mem_min_gb, ram_floor_gb))
    paused = paused or []
    mem_gb = sample["memory"]["avail_mb"] / 1024
    v = {
        "load1": sample["cpu"]["load1"],
        "load5": sample["cpu"]["load5"],
        "cpu_psi": sample["cpu"]["psi_some_avg60"],
        "mem_gb": mem_gb,
        "swap_mb": sample["swap"]["used_mb"],
        "mem_psi": sample["memory"]["psi_some_avg60"],
        "io_psi": sample["io"]["psi_some_avg60"],
        "cpus": sample["host"]["cores"],
        "light_running": running,
        "max_light": max_light,
        "project_running": project_running,
        "max_light_per_project": max_per_project,
    }
    why = []
    if not running < max_light:
        why.append(f"light lane: {running} of {max_light} running")
    if not project_running < max_per_project:
        why.append(f"light lane: {project} {project_running} of {max_per_project} running")
    if not mem_gb >= th.mem_min_gb:
        why.append(f"mem available {mem_gb:.1f} GB < {th.mem_min_gb:g} GB")
    if guard_paused:
        why.append("memory guard is pausing work")
    why += [f"paused: {p}" for p in paused]
    over_io = not v["io_psi"] < io_psi_max
    return Decision(
        not why,
        why,
        over_io,
        {k: round(x, 2) if isinstance(x, float) else x for k, x in v.items()},
        paused,
        asdict(th) | {"io_psi_max": io_psi_max, "io_psi_gate": io_psi_gate},
        {k: round(x, 4) for k, x in factor.items()},
        f"light lane: {running} of {max_light} running ({project} {project_running} of {max_per_project}), "
        f"{mem_gb:.1f} GB free",
    )
