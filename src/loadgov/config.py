"""Static configuration: the four profiles, read from an optional TOML file (stdlib `tomllib`)."""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from urllib.parse import urlsplit

DEFAULT_CONFIG = "/etc/loadgov/loadgov.toml"

# The five tunable parameters: (min, max, step).
RANGES: dict[str, tuple[float, float, float]] = {
    "max_cores": (1.0, 6.0, 0.5),
    "target_cpu_pct": (35, 90, 5),
    "load_max": (1.0, 6.0, 0.5),
    "max_implementers": (0, 4, 1),
    "ram_floor_gb": (2.0, 6.0, 0.5),
}
PARAMS = tuple(RANGES)
# The restrictive (safer/quieter) direction per limit: -1 = lower is restrictive, +1 = higher is.
# A higher RAM floor makes the memory guard act earlier. load_hold is not a ceiling-capped limit.
RESTRICTIVE: dict[str, int] = {
    "max_cores": -1,
    "target_cpu_pct": -1,
    "load_max": -1,
    "max_implementers": -1,
    "cargo_build_jobs": -1,
    "vitest_max_workers": -1,
    "gate_shard_budget": -1,
    "ram_floor_gb": +1,
}
INT_PARAMS = ("max_implementers",)


class ConfigError(ValueError):
    pass


def tighter(param: str, new: float, old: float) -> bool:
    """`new` is more restrictive than `old` for this limit."""
    return (new - old) * RESTRICTIVE[param] > 0


def loosens(param: str, new: float, old: float) -> bool:
    """`new` is less restrictive than `old` for this limit."""
    return tighter(param, old, new)


def tighten(param: str, a: float, b: float) -> float:
    """The more restrictive of two values."""
    return a if tighter(param, a, b) else b


def check_param(name: str, value) -> float | int:
    """Validate one tunable against RANGES; return it as int (max_implementers) or float."""
    if name not in RANGES:
        raise ConfigError(f"unknown parameter {name!r}")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{name}: expected a number, got {value!r}")
    lo, hi, step = RANGES[name]
    steps = (value - lo) / step
    if not lo <= value <= hi or abs(steps - round(steps)) > 1e-9:
        raise ConfigError(f"{name}: {value} is not in {lo}..{hi} step {step}")
    return int(value) if name in INT_PARAMS else float(value)


@dataclass(frozen=True)
class Profile:
    max_cores: float = 3.5
    target_cpu_pct: float = 65.0
    load_max: float = 3.0
    max_implementers: int = 2
    cargo_build_jobs: int = 2
    ram_floor_gb: float = 3.0
    load_hold: float = 5.0  # its own key, never derived from load_max
    impl_max_quiet: int | None = None  # None = max_implementers
    vitest_max_workers: int = 2
    gate_shard_budget: int = 2
    hold_heavy_slot: bool = False  # drain holds the heavy slot (SlotManager.set_profile_hold)

    def __post_init__(self):
        if self.impl_max_quiet is None:
            object.__setattr__(self, "impl_max_quiet", self.max_implementers)


BUILTIN_PROFILES: dict[str, Profile] = {
    "silent": Profile(2.0, 50.0, 2.0, 1, 1),
    "balanced": Profile(),
    "boost": Profile(5.5, 85.0, 5.0, 3, 3),
    "drain": Profile(2.0, 50.0, 2.0, 0, 1, hold_heavy_slot=True),
}


KINDS = ("impl", "review", "gate", "compile")
# Headroom thresholds (the stricter values for every kind; per-project values come from [projects.*]).
# Load limits may be "nproc" expressions, resolved against the host's core count when a decision is made.
HEADROOM_DEFAULTS: dict = {
    "load1_max": "nproc-2",
    "load5_max": "nproc-1.5",  # false disables the check
    "psi_max": 20,
    "mem_min_gb": 4,
    "swap_max_mb": 1024,
    "mem_psi_max": 5,
    "confirm_s": 60,
    "poll_s": 30,
    "max_wait_s": 7200,
}
DEFAULT_PROJECT = "default"
_NPROC_RE = re.compile(r"^nproc(?:\s*([-+*/])\s*(\d+(?:\.\d+)?))?$")


@dataclass(frozen=True)
class Thresholds:
    load1_max: float
    load5_max: float | None
    psi_max: float
    mem_min_gb: float
    swap_max_mb: float
    mem_psi_max: float
    confirm_s: float
    poll_s: float
    max_wait_s: float


@dataclass(frozen=True)
class HeadroomConfig:
    io_psi_gate: bool = False  # io PSI is only recorded (would_block_io) until flipped with trial data
    io_psi_max: float = 25.0
    kinds: dict = field(default_factory=dict)  # kind -> raw overrides, every project
    projects: dict = field(default_factory=dict)  # project -> kind -> raw overrides, config only


@dataclass(frozen=True)
class RestConfig:
    enabled: bool = False
    bind: str | None = None  # required when enabled: one specific, non-loopback address
    port: int = 7420
    allow: tuple[str, ...] = ()  # source IPs or CIDRs; empty = the bearer token is the only check
    rate_per_s: float = 5.0
    max_connections: int = 16  # concurrent connections; the rest are closed unread
    allow_private_bridge: bool = False  # accept a bind in 172.16.0.0/12 (docker bridges live there)


PRIVATE_BRIDGE_NET = ipaddress.ip_network("172.16.0.0/12")


def check_rest_bind(bind, allow_private_bridge: bool = False) -> str:
    """The bind address must be one specific, non-loopback address, and not on a container network:
    docker bridges, br-* and veth* interfaces draw their addresses from 172.16.0.0/12, so that range is
    refused unless `allow_private_bridge = true` says the LAN really uses it."""
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        raise ConfigError(f"rest.bind: {bind!r} is not an IP address") from None
    if ip.is_unspecified or ip.is_loopback:
        raise ConfigError(
            f"rest.bind: {bind} is refused (use the LAN address, never 0.0.0.0, :: or loopback)"
        )
    if not allow_private_bridge and ip.version == 4 and ip in PRIVATE_BRIDGE_NET:
        raise ConfigError(
            f"rest.bind: {bind} is in 172.16.0.0/12 (docker bridge range); "
            "set allow_private_bridge = true if this is your LAN address"
        )
    return str(ip)


def _parse_rest(raw) -> RestConfig:
    if not isinstance(raw, dict):
        raise ConfigError("rest: expected a table")
    for key in raw:
        if key not in (
            "enabled",
            "bind",
            "port",
            "allow",
            "rate_per_s",
            "max_connections",
            "allow_private_bridge",
        ):
            raise ConfigError(f"rest: unknown key {key!r}")
    d = RestConfig()
    enabled = raw.get("enabled", d.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError("rest.enabled: expected a boolean")
    bridge = raw.get("allow_private_bridge", d.allow_private_bridge)
    if not isinstance(bridge, bool):
        raise ConfigError("rest.allow_private_bridge: expected a boolean")
    bind = raw.get("bind")
    if bind is None and enabled:
        raise ConfigError("rest.bind: required when rest.enabled = true (the host's own LAN address)")
    if bind is not None:
        bind = check_rest_bind(bind, bridge)
    port = raw.get("port", d.port)
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigError("rest.port: expected an integer 1..65535")
    allow = raw.get("allow", [])
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        raise ConfigError("rest.allow: expected a list of IP addresses or CIDRs")
    for a in allow:
        try:
            ipaddress.ip_network(a, strict=False)
        except ValueError:
            raise ConfigError(f"rest.allow: {a!r} is not an IP address or CIDR") from None
    rate = raw.get("rate_per_s", d.rate_per_s)
    if isinstance(rate, bool) or not isinstance(rate, int | float) or rate <= 0:
        raise ConfigError("rest.rate_per_s: expected a number > 0")
    maxc = raw.get("max_connections", d.max_connections)
    if not _is_int(maxc) or not 1 <= maxc <= 64:
        raise ConfigError("rest.max_connections: expected an integer 1..64")
    return RestConfig(enabled, bind, port, tuple(allow), float(rate), maxc, bridge)


@dataclass(frozen=True)
class HaConfig:
    enabled: bool = False  # push alerts to a Home Assistant webhook (the id lives in tokens.env)
    url: str = "http://homeassistant.local:8123"


@dataclass(frozen=True)
class CompatConfig:
    alert_log_paths: tuple[str, ...] = ()  # files that get the legacy watcher ALERT lines
    mirror_cgroups: tuple[str, ...] = ()  # cgroups (below the cgroup root) that follow the governor's cap
    governor_log_path: str | None = None  # file that gets the legacy governor log lines


def check_ha_url(url) -> str:
    """http(s) with a host and nothing that could smuggle credentials or a second target."""
    if not isinstance(url, str):
        raise ConfigError("ha.url: expected a string")
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ConfigError("ha.url: expected an http:// or https:// URL with a host")
    if u.username or u.password or u.query or u.fragment:
        raise ConfigError("ha.url: no credentials, query or fragment allowed")
    try:
        u.port  # noqa: B018  (raises ValueError for a bad port)
    except ValueError:
        raise ConfigError("ha.url: bad port") from None
    return url.rstrip("/")


def _parse_ha(raw) -> HaConfig:
    if not isinstance(raw, dict) or set(raw) - {"enabled", "url"}:
        raise ConfigError("ha: expected a table with the keys enabled and url")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("ha.enabled: expected a boolean")
    return HaConfig(enabled, check_ha_url(raw.get("url", HaConfig.url)))


def _cgroup_name_ok(m) -> bool:
    return (
        isinstance(m, str)
        and bool(m)
        and not m.startswith("/")
        and ".." not in m.split("/")
        and "\0" not in m
    )


def _parse_compat(raw) -> CompatConfig:
    allowed = {"alert_log_paths", "mirror_cgroups", "governor_log_path"}
    if isinstance(raw, dict) and any(k.startswith("sync_") for k in raw):
        raise ConfigError(
            "compat: the old single-cgroup sync option was removed; "
            "[compat] mirror_cgroups = [<cgroup>, ...] replaces it (docs/protocol.md, Mirror)"
        )
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ConfigError("compat: allowed keys are alert_log_paths, mirror_cgroups, governor_log_path")
    paths = raw.get("alert_log_paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) and os.path.isabs(p) for p in paths):
        raise ConfigError("compat.alert_log_paths: expected a list of absolute file paths")
    if any("\0" in p for p in paths):
        raise ConfigError("compat.alert_log_paths: NUL in path")
    mirrors = raw.get("mirror_cgroups", [])
    if not isinstance(mirrors, list) or not all(_cgroup_name_ok(m) for m in mirrors):
        raise ConfigError("compat.mirror_cgroups: expected a list of cgroup paths below the cgroup root")
    log_path = raw.get("governor_log_path")
    if log_path is not None and (
        not isinstance(log_path, str) or not os.path.isabs(log_path) or "\0" in log_path
    ):
        raise ConfigError("compat.governor_log_path: expected an absolute file path")
    return CompatConfig(tuple(paths), tuple(mirrors), log_path)


@dataclass(frozen=True)
class GuardProject:
    """Per-project memory guard rule (replaces the global rule for that project's work)."""

    low_mb: int
    high_mb: int
    swap_mb: int | None = None  # also pause when swap used exceeds this while avail < high_mb
    resume_samples: int = 3  # daemon ticks (5 s) at or above high_mb before the LIFO resume
    containers: str | None = None  # regex of container names this project allows to be docker-paused
    agent_cwd: str | None = None  # `claude -p` sessions with their cwd below this path belong to it


@dataclass(frozen=True)
class LegacySnapshot:
    """Detects implementer trees of a legacy dispatcher: a `bash <prefix>*` whose snapshot file has a
    line starting with `marker`; the trees are reported under `project`."""

    prefix: str
    marker: str
    project: str


@dataclass(frozen=True)
class PauseMarker:
    """A host-wide pause file under the lock directory, written while `project` has work paused."""

    file: str
    project: str


DENY_CONTAINERS = ("*-test-db*", "*-review-*")  # default globs that are never docker-paused


@dataclass(frozen=True)
class MemguardConfig:
    enforce: bool = False  # [memguard] enforce: freeze/stop/pause/terminate; off = WOULD events only
    pre_gate_reap_gb: float = 8.0  # `loadctl run --kind gate` reaps JVM daemons below this (enforce only)
    log_path: str | None = None  # file that gets the legacy memory-guard log style lines
    projects: dict[str, GuardProject] = field(default_factory=dict)  # config only, no built-ins
    deny_containers: tuple[str, ...] = DENY_CONTAINERS  # fnmatch globs, never paused whatever a regex says
    legacy_snapshot: LegacySnapshot | None = None
    pause_marker: PauseMarker | None = None


@dataclass(frozen=True)
class PrincipalConfig:
    """Extra owner-claim denials on top of the built-in `build-work*` cgroups (docs/protocol.md)."""

    ancestor_deny: tuple[str, ...] = ()  # regexes matched against every ancestor's command line
    deny_cgroups: tuple[str, ...] = ()  # cgroup path prefixes, e.g. "/legacy-loop"


@dataclass(frozen=True)
class ReaperConfig:
    enforce: bool = False  # [reaper] enforce: terminate trees whose agent session is gone


BACKSTOP_MIN_MB = 4096  # memory.max below 4 GB is refused: it would kill ordinary builds


@dataclass(frozen=True)
class BackstopConfig:
    """Hard memory limit on build-work.slice. No built-in values: the owner sets them."""

    enabled: bool = False  # the enforce flag: off = nothing is written to any cgroup file
    memory_max_mb: int | None = None
    memory_swap_max_mb: int | None = None


@dataclass(frozen=True)
class Config:
    default_profile: str = "balanced"
    profiles: dict[str, Profile] = field(default_factory=lambda: dict(BUILTIN_PROFILES))
    slots_enforce: bool = False  # [slots] enforce: take the real flock files; off = log WOULD events only
    compile_lock: str | None = None  # [slots] compile_lock: file name under the lock dir (env default)
    headroom: HeadroomConfig = field(default_factory=HeadroomConfig)
    rest: RestConfig = field(default_factory=RestConfig)
    ha: HaConfig = field(default_factory=HaConfig)
    compat: CompatConfig = field(default_factory=CompatConfig)
    governor_enforce: bool = False  # [governor] enforce: write cpu.max; off = log WOULD events only
    memguard: MemguardConfig = field(default_factory=MemguardConfig)
    reaper: ReaperConfig = field(default_factory=ReaperConfig)
    backstop: BackstopConfig = field(default_factory=BackstopConfig)
    principal: PrincipalConfig = field(default_factory=PrincipalConfig)


def eval_nproc(value, nproc: int) -> float:
    """A number, or an expression `nproc`, `nproc-2`, `nproc*0.5` (+ - * /)."""
    if isinstance(value, str):
        m = _NPROC_RE.match(value.strip())
        if not m:
            raise ConfigError(f"bad expression {value!r} (use a number or e.g. 'nproc-2')")
        op, num = m.groups()
        if op is None:
            return float(nproc)
        n = float(num)
        if op == "/" and n == 0:
            raise ConfigError(f"bad expression {value!r}: division by zero")
        return {"-": nproc - n, "+": nproc + n, "*": nproc * n, "/": nproc / n if n else 0.0}[op]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"expected a number or 'nproc' expression, got {value!r}")
    return float(value)


def _check_kind_overrides(where: str, raw) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    for key, v in raw.items():
        if key not in HEADROOM_DEFAULTS:
            raise ConfigError(f"{where}: unknown key {key!r}")
        if key == "load5_max" and v is False:
            continue
        if key not in ("load1_max", "load5_max") and isinstance(v, str):
            raise ConfigError(f"{where}.{key}: expected a number")
        try:
            # Shape only: "nproc-2" is negative on one core but fine on the real host (checked per decision).
            n = eval_nproc(v, 1_000_000)
        except ConfigError as e:
            raise ConfigError(f"{where}.{key}: {e}") from None
        if n < 0:
            raise ConfigError(f"{where}.{key}: expected a number >= 0")
    return raw


def thresholds(h: HeadroomConfig, kind: str, project: str | None, nproc: int) -> Thresholds:
    """defaults < [headroom.kinds.<kind>] < [projects.<project>.kinds.<kind>]."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    per_project = h.projects.get(project or DEFAULT_PROJECT, {})
    raw = {**HEADROOM_DEFAULTS, **h.kinds.get(kind, {}), **per_project.get(kind, {})}
    l5 = raw.pop("load5_max")
    num = {k: eval_nproc(v, nproc) for k, v in raw.items()}
    return Thresholds(load5_max=None if l5 is False else eval_nproc(l5, nproc), **num)


def _parse_headroom(raw) -> HeadroomConfig:
    if not isinstance(raw, dict):
        raise ConfigError("headroom: expected a table")
    for key in raw:
        if key not in ("io_psi_gate", "io_psi_max", "kinds"):
            raise ConfigError(f"headroom: unknown key {key!r}")
    gate = raw.get("io_psi_gate", False)
    if not isinstance(gate, bool):
        raise ConfigError("headroom.io_psi_gate: expected a boolean")
    io_max = raw.get("io_psi_max", 25.0)
    if isinstance(io_max, bool) or not isinstance(io_max, int | float) or io_max <= 0:
        raise ConfigError("headroom.io_psi_max: expected a number > 0")
    kinds = raw.get("kinds", {})
    if not isinstance(kinds, dict):
        raise ConfigError("headroom.kinds: expected a table")
    for k, v in kinds.items():
        if k not in KINDS:
            raise ConfigError(f"headroom.kinds.{k}: unknown kind ({', '.join(KINDS)})")
        _check_kind_overrides(f"headroom.kinds.{k}", v)
    return HeadroomConfig(gate, float(io_max), dict(kinds))


def _parse_projects(projects, h: HeadroomConfig) -> HeadroomConfig:
    if not isinstance(projects, dict):
        raise ConfigError("projects: expected a table")
    merged = {p: {k: dict(v) for k, v in kinds.items()} for p, kinds in h.projects.items()}
    for name, body in projects.items():
        if not isinstance(body, dict):
            raise ConfigError(f"projects.{name}: expected a table")
        kinds = body.get("kinds", {})  # other keys of a project table belong to other features
        if not isinstance(kinds, dict):
            raise ConfigError(f"projects.{name}.kinds: expected a table")
        for k, v in kinds.items():
            if k not in KINDS:
                raise ConfigError(f"projects.{name}.kinds.{k}: unknown kind ({', '.join(KINDS)})")
            _check_kind_overrides(f"projects.{name}.kinds.{k}", v)
            merged.setdefault(name, {}).setdefault(k, {}).update(v)
    return replace(h, projects=merged)


def check_lock_name(where: str, name) -> str:
    """A lock file name: a plain `*.lock` name under the lock directory."""
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.lock", name):
        raise ConfigError(f"{where}: expected a plain file name ending in .lock")
    return name


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _profile(name: str, base: Profile, raw: dict) -> Profile:
    known = {f.name for f in fields(Profile)}
    for key in raw:
        if key not in known:
            raise ConfigError(f"profiles.{name}: unknown key {key!r}")
    if "max_implementers" in raw and "impl_max_quiet" not in raw:
        raw = {**raw, "impl_max_quiet": raw["max_implementers"]}  # follows unless given
    out = replace(base, **raw)
    for p in PARAMS:
        try:
            check_param(p, getattr(out, p))
        except ConfigError as e:
            raise ConfigError(f"profiles.{name}: {e}") from None
    for key in ("cargo_build_jobs", "vitest_max_workers", "gate_shard_budget"):
        v = getattr(out, key)
        if not _is_int(v) or v < 1:
            raise ConfigError(f"profiles.{name}.{key}: expected an integer >= 1")
    if not _is_int(out.impl_max_quiet) or not 0 <= out.impl_max_quiet <= out.max_implementers:
        raise ConfigError(f"profiles.{name}.impl_max_quiet: expected an integer 0..max_implementers")
    if isinstance(out.load_hold, bool) or not isinstance(out.load_hold, int | float) or out.load_hold <= 0:
        raise ConfigError(f"profiles.{name}.load_hold: expected a number > 0")
    if not isinstance(out.hold_heavy_slot, bool):
        raise ConfigError(f"profiles.{name}.hold_heavy_slot: expected a boolean")
    return out


_MEMGUARD_KEYS = {
    "enforce",
    "pre_gate_reap_gb",
    "log_path",
    "projects",
    "deny_containers",
    "legacy_snapshot",
    "pause_marker",
}
_GUARD_KEYS = ("low_mb", "high_mb", "swap_mb", "resume_samples", "containers", "agent_cwd")


def _parse_memguard(raw) -> MemguardConfig:
    if not isinstance(raw, dict) or set(raw) - _MEMGUARD_KEYS:
        raise ConfigError(f"memguard: allowed keys are {', '.join(sorted(_MEMGUARD_KEYS))}")
    enforce = raw.get("enforce", False)
    if not isinstance(enforce, bool):
        raise ConfigError("memguard.enforce: expected a boolean")
    gb = raw.get("pre_gate_reap_gb", MemguardConfig.pre_gate_reap_gb)
    if isinstance(gb, bool) or not isinstance(gb, int | float) or not 0 <= gb <= 64:
        raise ConfigError("memguard.pre_gate_reap_gb: expected a number 0..64 (0 = off)")
    log_path = raw.get("log_path")
    if log_path is not None and (not isinstance(log_path, str) or not os.path.isabs(log_path)):
        raise ConfigError("memguard.log_path: expected an absolute file path")
    projects: dict[str, GuardProject] = {}
    table = raw.get("projects", {})
    if not isinstance(table, dict):
        raise ConfigError("memguard.projects: expected a table")
    for name, body in table.items():
        where = f"memguard.projects.{name}"
        if not isinstance(body, dict) or set(body) - set(_GUARD_KEYS):
            raise ConfigError(f"{where}: allowed keys are {', '.join(_GUARD_KEYS)}")
        base = projects.get(name)
        vals = {k: getattr(base, k) for k in _GUARD_KEYS} if base else {}
        vals.update(body)
        if "low_mb" not in vals or "high_mb" not in vals:
            raise ConfigError(f"{where}: low_mb and high_mb are required")
        for k in ("low_mb", "high_mb", "resume_samples"):
            if k in vals and (not _is_int(vals[k]) or vals[k] < 1):
                raise ConfigError(f"{where}.{k}: expected an integer >= 1")
        if vals.get("swap_mb") is not None and (not _is_int(vals["swap_mb"]) or vals["swap_mb"] < 1):
            raise ConfigError(f"{where}.swap_mb: expected an integer >= 1")
        if vals["high_mb"] - vals["low_mb"] < 2048:  # the resume level must clear the pause level
            raise ConfigError(f"{where}: high_mb must be at least 2048 MB above low_mb")
        for k in ("containers", "agent_cwd"):
            if vals.get(k) is not None and not isinstance(vals[k], str):
                raise ConfigError(f"{where}.{k}: expected a string")
        if vals.get("containers") is not None:
            try:
                re.compile(vals["containers"])
            except re.error as e:
                raise ConfigError(f"{where}.containers: bad regex ({e})") from None
        if vals.get("agent_cwd") is not None and not os.path.isabs(vals["agent_cwd"]):
            raise ConfigError(f"{where}.agent_cwd: expected an absolute path")
        projects[name] = GuardProject(**vals)
    deny = raw.get("deny_containers", list(DENY_CONTAINERS))
    if not isinstance(deny, list) or not all(isinstance(g, str) and g for g in deny):
        raise ConfigError("memguard.deny_containers: expected a list of name globs")
    snap = raw.get("legacy_snapshot")
    if snap is not None:
        snap = _table_of_strings("memguard.legacy_snapshot", snap, ("prefix", "marker", "project"))
        if not os.path.isabs(snap["prefix"]):
            raise ConfigError("memguard.legacy_snapshot.prefix: expected an absolute path prefix")
        snap = LegacySnapshot(**snap)
    marker = raw.get("pause_marker")
    if marker is not None:
        marker = _table_of_strings("memguard.pause_marker", marker, ("file", "project"))
        if "/" in marker["file"] or marker["file"] in (".", ".."):
            raise ConfigError("memguard.pause_marker.file: expected a plain file name")
        marker = PauseMarker(**marker)
    return MemguardConfig(enforce, float(gb), log_path, projects, tuple(deny), snap, marker)


def _table_of_strings(where: str, raw, keys: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(keys):
        raise ConfigError(f"{where}: expected a table with the keys {', '.join(keys)}")
    for k in keys:
        if not isinstance(raw[k], str) or not raw[k] or "\0" in raw[k]:
            raise ConfigError(f"{where}.{k}: expected a non-empty string")
    return dict(raw)


def _parse_principal(raw) -> PrincipalConfig:
    keys = ("ancestor_deny", "deny_cgroups")
    if not isinstance(raw, dict) or set(raw) - set(keys):
        raise ConfigError(f"principal: allowed keys are {', '.join(keys)}")
    for k in keys:
        v = raw.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ConfigError(f"principal.{k}: expected a list of non-empty strings")
    for rx in raw.get("ancestor_deny", []):
        try:
            re.compile(rx)
        except re.error as e:
            raise ConfigError(f"principal.ancestor_deny: bad regex {rx!r} ({e})") from None
    for cg in raw.get("deny_cgroups", []):
        if not cg.startswith("/"):
            raise ConfigError("principal.deny_cgroups: expected cgroup paths starting with '/'")
    return PrincipalConfig(tuple(raw.get("ancestor_deny", [])), tuple(raw.get("deny_cgroups", [])))


def _parse_reaper(raw) -> ReaperConfig:
    if not isinstance(raw, dict) or set(raw) - {"enforce"}:
        raise ConfigError("reaper: expected a table with only the key 'enforce'")
    if not isinstance(raw.get("enforce", False), bool):
        raise ConfigError("reaper.enforce: expected a boolean")
    return ReaperConfig(raw.get("enforce", False))


def _parse_backstop(raw) -> BackstopConfig:
    keys = ("enabled", "memory_max_mb", "memory_swap_max_mb")
    if not isinstance(raw, dict) or set(raw) - set(keys):
        raise ConfigError(f"backstop: allowed keys are {', '.join(keys)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("backstop.enabled: expected a boolean")
    mem, swap = raw.get("memory_max_mb"), raw.get("memory_swap_max_mb")
    if mem is not None and (not _is_int(mem) or mem < BACKSTOP_MIN_MB):
        raise ConfigError(f"backstop.memory_max_mb: expected an integer >= {BACKSTOP_MIN_MB} (4 GB)")
    if swap is not None and (not _is_int(swap) or swap < 0):
        raise ConfigError("backstop.memory_swap_max_mb: expected an integer >= 0")
    if enabled and (mem is None or swap is None):
        raise ConfigError(
            "backstop.enabled needs both memory_max_mb and memory_swap_max_mb (no built-in values)"
        )
    return BackstopConfig(enabled, mem, swap)


def parse_config(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML: {e}") from None
    for key in raw:
        if key not in (
            "default_profile",
            "profiles",
            "headroom",
            "projects",
            "slots",
            "rest",
            "ha",
            "compat",
            "governor",
            "memguard",
            "reaper",
            "backstop",
            "principal",
        ):
            raise ConfigError(f"unknown key {key!r}")
    profiles = dict(BUILTIN_PROFILES)
    for name, p in raw.get("profiles", {}).items():
        if name not in BUILTIN_PROFILES:
            raise ConfigError(f"profiles.{name}: unknown profile (silent, balanced, boost, drain)")
        if not isinstance(p, dict):
            raise ConfigError(f"profiles.{name}: expected a table")
        profiles[name] = _profile(name, BUILTIN_PROFILES[name], p)
    default = raw.get("default_profile", "balanced")
    if default not in profiles:
        raise ConfigError(f"default_profile {default!r} is not a profile")
    slots = raw.get("slots", {})
    if not isinstance(slots, dict) or set(slots) - {"enforce", "compile_lock"}:
        raise ConfigError("slots: allowed keys are enforce, compile_lock")
    if not isinstance(slots.get("enforce", False), bool):
        raise ConfigError("slots.enforce: expected a boolean")
    compile_lock = slots.get("compile_lock")
    if compile_lock is not None:
        check_lock_name("slots.compile_lock", compile_lock)
    governor = raw.get("governor", {})
    if not isinstance(governor, dict) or set(governor) - {"enforce"}:
        raise ConfigError("governor: expected a table with only the key 'enforce'")
    if not isinstance(governor.get("enforce", False), bool):
        raise ConfigError("governor.enforce: expected a boolean")
    headroom = _parse_projects(raw.get("projects", {}), _parse_headroom(raw.get("headroom", {})))
    return Config(
        default, profiles, slots.get("enforce", False), compile_lock, headroom,
        _parse_rest(raw.get("rest", {})),
        _parse_ha(raw.get("ha", {})), _parse_compat(raw.get("compat", {})), governor.get("enforce", False),
        _parse_memguard(raw.get("memguard", {})), _parse_reaper(raw.get("reaper", {})),
        _parse_backstop(raw.get("backstop", {})), _parse_principal(raw.get("principal", {})),
    )  # fmt: skip


def load_config(path: str | None) -> Config:
    """A missing file means the built-in defaults."""
    if not path:
        return Config()
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return Config()
    return parse_config(text)
