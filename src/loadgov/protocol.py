"""Socket protocol v1 and the status schema (documented in docs/protocol.md)."""

from __future__ import annotations

import json

VERSION = 1
DEFAULT_SOCKET = "/run/loadgovd/loadgovd.sock"
STARTING = "starting"  # error code of a request before the first sample; the client retries it

NUM = (int, float)
OPT_STR = (str, type(None))
OPT_INT = (int, type(None))
OPT_NUM = (int, float, type(None))
OPT_DICT = (dict, type(None))
LEVEL = ("OK", "BUSY", "LIMIT")

# Dotted path → allowed types. Lists of objects are checked per item with "path[]".
STATUS_SCHEMA: dict[str, tuple] = {
    "v": (int,),
    "at": (str,),
    "at_ms": (int,),
    "host.cores": (int,),
    "cpu.busy_pct": NUM,
    "cpu.psi_some_avg10": NUM,
    "cpu.psi_some_avg60": NUM,
    "cpu.psi_effective": NUM,
    "cpu.load1": NUM,
    "cpu.load5": NUM,
    "cpu.load1_per_core": NUM,
    "memory.avail_mb": (int,),
    "memory.total_mb": (int,),
    "memory.psi_some_avg10": NUM,
    "memory.psi_some_avg60": NUM,
    "memory.psi_full_avg60": NUM,
    "swap.total_mb": (int,),
    "swap.used_mb": (int,),
    "swap.used_pct": NUM,
    "swap.in_rate": NUM,
    "swap.out_rate": NUM,
    "io.psi_some_avg10": NUM,
    "io.psi_some_avg60": NUM,
    "io.psi_full_avg60": NUM,
    "disk.free_gb": NUM,
    "disk.paths": (list,),
    "top": (list,),
    "top[].pid": (int,),
    "top[].comm": (str,),
    "top[].pcpu": NUM,
    "top[].rss_mb": (int,),
    "top[].cgroup": (str,),
    "jvm_daemons": (int,),
    "heavy_slot.held": (bool,),
    "heavy_slot.holder_pid": OPT_INT,
    "heavy_slot.lock_pids": (list,),
    "heavy_slot.holder": OPT_STR,
    "heavy_slot.last_holder": OPT_STR,
    "heavy_slot.since": OPT_STR,
    "heavy_slot.age_s": OPT_INT,
    "heavy_slot.stuck": (bool,),
    "slots.heavy.state": (str,),
    "slots.heavy.holder": OPT_STR,
    "slots.heavy.source": OPT_STR,
    "slots.heavy.queue": (int,),
    "slots.heavy.nested": (int,),
    "slots.heavy.held_since": OPT_STR,
    "slots.heavy.held_s": OPT_INT,
    "slots.heavy.idle_s": OPT_INT,
    "slots.heavy.stuck": (bool,),
    "slots.heavy.hold": OPT_STR,
    "slots.heavy.hold_origin": OPT_STR,
    "slots.heavy.key": OPT_STR,
    "slots.heavy.url": OPT_STR,
    "slots.heavy.waiters": (list,),
    "slots.heavy.waiters[].position": (int,),
    "slots.heavy.waiters[].project": (str,),
    "slots.heavy.waiters[].label": (str,),
    "slots.heavy.waiters[].kind": OPT_STR,
    "slots.heavy.waiters[].key": OPT_STR,
    "slots.heavy.waiters[].url": OPT_STR,
    "slots.heavy.waiters[].pid": (int,),
    "slots.heavy.waiters[].queued_since": (str,),
    "slots.heavy.waiters[].wait_s": (int,),
    "slots.heavy.waiters[].expected_s": OPT_NUM,
    "slots.heavy.waiters[].eta": OPT_STR,
    "slots.compile.state": (str,),
    "slots.compile.holder": OPT_STR,
    "slots.compile.source": OPT_STR,
    "slots.compile.queue": (int,),
    "slots.compile.nested": (int,),
    "slots.compile.held_since": OPT_STR,
    "slots.compile.held_s": OPT_INT,
    "slots.compile.idle_s": OPT_INT,
    "slots.compile.stuck": (bool,),
    "slots.compile.hold": OPT_STR,
    "slots.compile.hold_origin": OPT_STR,
    "slots.compile.key": OPT_STR,
    "slots.compile.url": OPT_STR,
    "slots.compile.waiters": (list,),
    "slots.compile.waiters[].position": (int,),
    "slots.compile.waiters[].project": (str,),
    "slots.compile.waiters[].label": (str,),
    "slots.compile.waiters[].kind": OPT_STR,
    "slots.compile.waiters[].key": OPT_STR,
    "slots.compile.waiters[].url": OPT_STR,
    "slots.compile.waiters[].pid": (int,),
    "slots.compile.waiters[].queued_since": (str,),
    "slots.compile.waiters[].wait_s": (int,),
    "slots.compile.waiters[].expected_s": OPT_NUM,
    "slots.compile.waiters[].eta": OPT_STR,
    "jobs": (list,),
    "jobs[].kind": (str,),
    "jobs[].project": (str,),
    "jobs[].label": (str,),
    "jobs[].unit": (str,),
    "jobs[].cgroup": (str,),
    "jobs[].pid": (int,),
    "jobs[].slot": OPT_STR,
    "jobs[].since": (str,),
    "jobs[].key": OPT_STR,
    "jobs[].url": OPT_STR,
    "jobs[].expected_s": OPT_NUM,
    "jobs[].eta": OPT_STR,
    "jobs[].overdue": (bool,),
    "job_stats": (list,),
    "job_stats[].project": (str,),
    "job_stats[].kind": (str,),
    "job_stats[].count": (int,),
    "job_stats[].duration_sum_s": NUM,
    "job_stats[].duration_p50_s": OPT_NUM,
    "job_stats[].cpu_p50_s": OPT_NUM,
    "job_stats[].cpu_p95_s": OPT_NUM,
    "job_stats[].docker_cpu_p50_s": OPT_NUM,
    "job_stats[].docker_cpu_p95_s": OPT_NUM,
    "job_stats[].total_cpu_p50_s": OPT_NUM,
    "job_stats[].total_cpu_p95_s": OPT_NUM,
    "job_stats[].host_cpu_p50_s": OPT_NUM,
    "job_stats[].mem_peak_p50_mb": OPT_NUM,
    "job_stats[].mem_peak_p95_mb": OPT_NUM,
    "job_stats[].mem_peak_max_mb": OPT_NUM,
    "job_stats[].cpu_total_s": NUM,
    "job_stats[].docker_cpu_total_s": NUM,
    "projects": (dict,),
    "projects{}.cpu_pct": OPT_NUM,
    "projects{}.cores": OPT_NUM,
    "projects{}.docker_cpu_pct": NUM,
    "projects{}.mem_mb": OPT_INT,
    "projects{}.jobs": (int,),
    "projects{}.waiting": (int,),
    "projects{}.slice": (str,),
    "cgroups": (dict,),
    "stopped": (list,),
    "stopped[].pid": (int,),
    "stopped[].comm": (str,),
    "stopped[].cgroup": (str,),
    "levels.cpu": (str,),
    "levels.ram": (str,),
    "levels.swap": (str,),
    "levels.disk": (str,),
    "level": (str,),
    "limits.profile": (str,),
    "limits.ceiling": OPT_STR,
    "limits.ceiling_info": OPT_DICT,
    "limits.profile_info": OPT_DICT,
    "limits.overrides": (list,),
    "limits.restricted": (bool,),
    "limits.restricted_by": OPT_STR,
    "limits.freeze": OPT_STR,
    "limits.effective": (dict,),
    "rest.enabled": (bool,),
    "rest.state": (str,),
    "rest.error": OPT_STR,
    "rest.bind": OPT_STR,
    "rest.port": (int,),
    "alerts.active": (list,),
    "alerts.active[].key": (str,),
    "alerts.active[].level": (str,),
    "alerts.active[].since": (str,),
    "alerts.events": (list,),
    "alerts.events[].type": (str,),
    "alerts.events[].key": (str,),
    "alerts.events[].at": (str,),
    "ha_push.enabled": (bool,),
    "ha_push.state": (str,),
    "ha_push.queued": (int,),
    "ha_push.dropped": (int,),
    "ha_push.failed": (int,),
    "ha_push.last_ok": OPT_STR,
    "ha_push.last_error": OPT_STR,
    "governor.enforce": (bool,),
    "governor.cap_cores": (int, float, type(None)),
    "governor.target_pct": NUM,
    "governor.max_cores": NUM,
    "governor.min_cores": NUM,
    "governor.last_busy_pct": (int, float, type(None)),
    "governor.last_change_at": OPT_STR,
    "governor.last_reason": OPT_STR,
    "governor.targets": (list,),
    "governor.targets[].path": (str,),
    "governor.targets[].present": (bool,),
    "governor.targets[].cpu_max": OPT_STR,
    "memguard.enforce": (bool,),
    "memguard.reaper_enforce": (bool,),
    "memguard.low_mb": (int,),
    "memguard.resume_mb": (int,),
    "memguard.crit_mb": (int,),
    "memguard.baseline_swap_mb": (int,),
    "memguard.containers_age_s": OPT_NUM,
    "memguard.paused": (list,),
    "memguard.paused[].id": (str,),
    "memguard.paused[].kind": (str,),
    "memguard.paused[].how": (str,),
    "memguard.paused[].what": (str,),
    "memguard.paused[].project": OPT_STR,
    "memguard.paused[].since": (str,),
    "memguard.paused[].reason": (str,),
    "memguard.paused[].principal": (str,),
    "memguard.paused[].real": (bool,),
    "memguard.last_decision": OPT_STR,
    "memguard.last_reason": OPT_STR,
    "memguard.last_at": OPT_STR,
    "memguard.cooldown_s": NUM,
    "memguard.foreign_stopped": (list,),
    "memguard.foreign_stopped[].pid": (int,),
    "memguard.foreign_stopped[].comm": (str,),
    "memguard.foreign_stopped[].cgroup": (str,),
    "headroom.io_would_block_count": (int,),
    "headroom.io_would_block_last": OPT_STR,
    "backstop.enabled": (bool,),
    "backstop.applied": (bool,),
    "backstop.memory_max_mb": OPT_INT,
    "backstop.memory_swap_max_mb": OPT_INT,
    "backstop.oom_kills": (int,),
    "backstop.max_events": (int,),
    "backstop.swap_fail_events": (int,),
    "backstop.last_event": OPT_STR,
    "backstop.note": OPT_STR,
    "strays.enabled": (bool,),
    "strays.enforce": (bool,),
    "strays.check_s": (int,),
    "strays.last_check": OPT_STR,
    "strays.items": (list,),
    "strays.items[].pid": (int,),
    "strays.items[].start_time": (int,),
    "strays.items[].comm": (str,),
    "strays.items[].cmdline": (str,),
    "strays.items[].cwd": OPT_STR,
    "strays.items[].cgroup": (str,),
    "strays.items[].project": OPT_STR,
    "strays.items[].cpu_pct": NUM,
    "strays.items[].hot_since": (str,),
    "strays.items[].age_s": (int,),
    "strays.items[].orphan": (bool,),
    "strays.items[].session.claude_pid": OPT_INT,
    "strays.items[].session.session_id": OPT_STR,
    "capabilities": (list,),
    "daemon.version": (str,),
    "daemon.pid": (int,),
    "daemon.started_at": (str,),
    "daemon.mode": (str,),
    "daemon.enforce": (dict,),
    "daemon.tick_s": NUM,
    "daemon.memory_protection_mb": (int,),
}


def _get(d, path: str):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            raise KeyError(path)
        d = d[part]
    return d


def validate_status(status: dict) -> list[str]:
    """Return a list of schema violations (empty = valid)."""
    errors = []
    for path, types in STATUS_SCHEMA.items():
        if "{}" in path:  # one entry per key of a dict of objects
            base, _, sub = path.partition("{}.")
            try:
                items = _get(status, base)
            except KeyError:
                continue
            if not isinstance(items, dict):
                continue  # reported by the base entry
            for name, item in items.items():
                try:
                    v = _get(item, sub)
                except KeyError:
                    errors.append(f"{base}.{name}.{sub}: missing")
                    continue
                if not isinstance(v, types) or isinstance(v, bool) and bool not in types:
                    errors.append(f"{base}.{name}.{sub}: {type(v).__name__}")
            continue
        if "[]" in path:
            base, _, sub = path.partition("[].")
            try:
                items = _get(status, base)
            except KeyError:
                continue  # reported by the base entry
            for i, item in enumerate(items):
                try:
                    v = _get(item, sub)
                except KeyError:
                    errors.append(f"{base}[{i}].{sub}: missing")
                    continue
                if not isinstance(v, types) or isinstance(v, bool) and bool not in types:
                    errors.append(f"{base}[{i}].{sub}: {type(v).__name__}")
            continue
        try:
            v = _get(status, path)
        except KeyError:
            errors.append(f"{path}: missing")
            continue
        if not isinstance(v, types) or (isinstance(v, bool) and bool not in types):
            errors.append(f"{path}: {type(v).__name__}")
    for path in ("level", "levels.cpu", "levels.ram", "levels.swap", "levels.disk"):
        try:
            if _get(status, path) not in LEVEL:
                errors.append(f"{path}: not one of {LEVEL}")
        except KeyError:
            pass
    return errors


def encode(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def decode(line: bytes) -> dict:
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("message must be a JSON object")
    return obj
