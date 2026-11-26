"""loadgovd: sample the host every tick, publish status.json, decide limits, answer on the unix socket.

Nothing is enforced unless the matching `enforce` flag is on (AGENTS.md rule 2). The daemon writes
nothing outside its run, state and log directories and the compat env file without it.

`loadgovd --recover --force` is the one thaw-all: it releases everything the paused registry
holds; it runs with the daemon stopped. Stopping or killing the daemon never thaws anything.

Start: `READY=1` goes out as soon as the socket listens and the saved state is restored; the first
sample and tick run after it (`status` answers `not_ready` until then), so a slow first tick (a full
/proc scan, `docker ps`, page faults under memory pressure) cannot hit `TimeoutStartSec`.

Watchdog: a small thread (`Heartbeat`) sends `WATCHDOG=1` every quarter of `WATCHDOG_USEC`, but only
while the last tick that completed without an exception is younger than `WATCHDOG_STALL_S`. A tick
that is merely slow (a pressure stall, a slow `docker ps`) therefore keeps the daemon alive, while a
hung loop or a persistently failing tick stops the keep-alive and systemd restarts the daemon;
one failing tick alone is logged and the loop carries on.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import contextlib
import json
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import asdict, replace

from loadgov import __version__
from loadgov.config import (
    DEFAULT_CONFIG,
    DEFAULT_PROJECT,
    KINDS,
    Config,
    ConfigError,
    apply_host_mode,
    changed_keys,
    derive_host_id,
    load_config,
    thresholds,
)
from loadgov.daemon import backstop_ops, image_ops, job_ops, memguard_ops, recover, slot_ops, stray_ops
from loadgov.daemon.audit import AuditLog
from loadgov.daemon.compat import render_env, write_env
from loadgov.daemon.fleet import FleetCollector, build_view
from loadgov.daemon.ha_push import HaPush
from loadgov.daemon.principal import caller_info, owner_denial
from loadgov.daemon.rest import DEFAULT_TOKENS_FILE, RestServer
from loadgov.daemon.state import StateStore
from loadgov.daemon.ui import UiServer
from loadgov.engines import slots as slots_engine
from loadgov.engines.governor import PRIMARY, Governor, event_line
from loadgov.engines.headroom import decide, decide_light, decide_review, paused_reasons, profile_factor
from loadgov.engines.history import COLUMNS, SOCKET_MAX, History, row_for
from loadgov.engines.image import VIEW_MAX, ImageTrain, find_builds, window_state
from loadgov.engines.jobs import JobTable
from loadgov.engines.limits import Limits, iso
from loadgov.engines.memguard import thresholds as guard_thresholds
from loadgov.engines.projects import ProjectMeter
from loadgov.engines.slots import SlotManager
from loadgov.engines.timers import parse_ttl, parse_until
from loadgov.engines.watch import LegacyTee, Watch, interval_top3, legacy_line
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS
from loadgov.protocol import DEFAULT_SOCKET, STARTING, VERSION, decode, encode
from loadgov.sample import Sampler

DEFAULT_ENV_FILE = "/run/lock/load-governor.env"

log = logging.getLogger("loadgovd")
FAST_TICK_S = 1.0
HEADROOM_OVERRIDES = (
    "load1_max",
    "load5_max",
    "psi_max",
    "mem_min_gb",
    "swap_max_mb",
    "swap_in_max",
    "mem_psi_max",
)
IMAGE_OPS = ("image_submit", "image_list", "image_cancel", "image_now", "image_next", "image_done")
WATCH_EVERY_S = 30  # alert evaluation interval
PARAM_ARGS = (
    "max_cores", "target_cpu_pct", "load_max", "max_implementers", "max_reviewers", "max_light",
    "ram_floor_gb",
)  # fmt: skip


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def sd_notify(msg: str) -> None:
    """Send a message to systemd's notify socket, if any (Type=notify, WatchdogSec)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s, contextlib.suppress(OSError):
        s.connect(addr)
        s.sendall(msg.encode())


WATCHDOG_STALL_S = 300.0  # no good tick for this long = a real hang or a persistent failure


class Heartbeat(threading.Thread):
    """Sends `WATCHDOG=1` from its own thread while the tick loop made progress recently.

    Under memory thrash the loop can stall for minutes on page faults; keeping the keep-alive on the
    loop itself would get the guard killed exactly when it is needed. The thread only vouches for the
    loop while `beat()` (called after each good tick) is at most `stall_s` old."""

    def __init__(self, interval_s: float, stall_s: float = WATCHDOG_STALL_S, clock=time.monotonic):
        super().__init__(name="loadgov-heartbeat", daemon=True)
        self.interval_s, self.stall_s, self.clock = interval_s, stall_s, clock
        self.last = clock()
        self._done = threading.Event()

    def beat(self) -> None:
        self.last = self.clock()

    def alive(self) -> bool:
        return self.clock() - self.last <= self.stall_s

    def ping(self) -> None:
        if self.alive():
            sd_notify("WATCHDOG=1")

    def run(self) -> None:
        while not self._done.wait(self.interval_s):
            self.ping()

    def stop(self) -> None:
        self._done.set()


def watchdog_interval_s(environ=os.environ) -> float:
    """A quarter of the unit's WatchdogSec (systemd passes it as WATCHDOG_USEC); 15 s when unknown."""
    try:
        usec = int(environ.get("WATCHDOG_USEC", ""))
    except ValueError:
        return 15.0
    return max(1.0, usec / 4e6) if usec > 0 else 15.0


class Daemon:
    def __init__(
        self,
        fs: HostFS,
        socket_path: str = DEFAULT_SOCKET,
        run_dir: str = "/run/loadgovd",
        compat_link: str | None = "/run/loadgovd.json",
        tick_s: float = 5.0,
        config: Config | None = None,
        state_dir: str = "/var/lib/loadgov",
        log_dir: str = "/var/log/loadgov",
        env_file: str | None = DEFAULT_ENV_FILE,
        clock=time.time,
        tokens_file: str = DEFAULT_TOKENS_FILE,
        rest_options: dict | None = None,  # test-only RestServer arguments
        ui_options: dict | None = None,  # test-only UiServer arguments
        memguard_options: dict | None = None,  # test-only MemGuardRuntime arguments (fake signals, docker)
        backstop_options: dict | None = None,  # test-only BackstopRuntime arguments (target slice)
        stray_options: dict | None = None,  # test-only StrayRuntime arguments (fake signals)
        config_path: str | None = None,  # re-read on SIGHUP
        hostname: str | None = None,  # test-only: the name the default host id is derived from
    ):
        self.fs = fs
        self.clock = clock
        config = config or Config()
        self.raw_config = config  # as written; self.config is what the engines run on
        config = apply_host_mode(config)
        self.config, self.config_path, self._reload = config, config_path, False
        self.store = StateStore(state_dir)
        saved_host = self.store.load_host_id()
        # configured id, else the one saved by an earlier start, else derived from the hostname
        self.host_id = config.host.id or saved_host or derive_host_id(hostname or socket.gethostname())
        self.audit, self.env_file = (
            AuditLog(log_dir, config.audit.max_mb, config.audit.keep, self.host_id),
            env_file,
        )
        if self.store.rejected_host_id is not None:  # the next state save writes a valid id: audited once
            self.audit.append({"ts": iso(clock()), "op": "host_id_invalid", "principal": "system",
                               "source": "state", "before": self.store.rejected_host_id,
                               "after": self.host_id,
                               "reason": "the saved host id is not valid; ignored"})  # fmt: skip
        if saved_host and saved_host != self.host_id:  # the next state save keeps the new id: audited once
            self.audit.append({"ts": iso(clock()), "op": "host_id_changed", "principal": "system",
                               "source": "config", "before": saved_host, "after": self.host_id,
                               "reason": "host.id in the config differs from the saved id"})  # fmt: skip
        self.limits = Limits(
            config.profiles,
            self.store.load(config.default_profile, set(config.profiles)),
            clock,
            config.default_profile,
        )
        slots_engine.set_compile_lock(config.compile_lock)
        self.slots = SlotManager(fs, clock, config.slots_enforce)
        self.slots.restore(self.store.load_slots())
        self.jobs = JobTable(fs, clock, pattern=self._project_containers)
        self.slots.docker_cpu = self.jobs.docker_cpu
        self.slots.expected_s = self.jobs.stats.expected_s
        self.slots.compile_cfg = config.compile
        self.slots.expected_peak = self.jobs.expected_peak_mb
        self.slots.memory = self._memory_view
        self.slots.work_running = self._work_running
        self.slots.url_template = self.jobs.url_template = config.keys.url
        self.projects = ProjectMeter(
            fs, lambda: self.jobs.jobs, self._waiting_projects, self._configured_projects, clock
        )
        self.governor = Governor(fs, clock, config.governor_enforce, config.compat.mirror_cgroups)
        self.runner = slot_ops.run_argv
        self.jobs.restore(self.store.load_jobs())
        self.jobs.stats.restore(self.store.load_job_stats())
        self.jobs.endings.restore(self.store.load_job_endings())
        self.jobs.peaks.restore(self.store.load_compile_peaks())
        self.image = ImageTrain(clock)
        self.image.restore(self.store.load_image())
        self.memguard = memguard_ops.MemGuardRuntime(self, **(memguard_options or {}))
        self.memguard.restore(self.store.load_memguard())  # paused work stays paused: re-adopt
        # systemctl for the backstop: refused on an injected cgroup root (backstop_ops.trusted_runner)
        self.backstop = backstop_ops.BackstopRuntime(
            fs, config.backstop, clock, state_dir, self.emit_event, self.audit.append,
            lambda: self.jobs.jobs, **{"runner": self.runner, **(backstop_options or {})},
        )  # fmt: skip
        self.jobs.on_end = self.backstop.job_ended  # read-only tmpfs leftover scan at a job's end
        self.history = History(os.path.join(state_dir, "history.csv"), clock)
        self.strays = stray_ops.StrayRuntime(self, **(stray_options or {}))
        if self.slots.owner_hold_reason() and not self.limits.state.hold:
            self.limits.set_flag("hold", self.slots.owner_hold_reason())  # the flag follows the saved hold
        self._env_key: str | None = None
        self.sampler = Sampler(fs, config.compat.mirror_cgroups, config.headroom.psi_busy_gate_pct)
        self.sampler.host_id, self.sampler.host_mode = self.host_id, config.host.mode
        self.sampler.disk_busy, self.sampler.disk_limit = config.levels.disk_busy, config.levels.disk_limit
        self.socket_path = socket_path
        self.run_dir = run_dir
        self.status_path = os.path.join(run_dir, "status.json")
        self.compat_link = compat_link
        self.tick_s = tick_s
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.status: dict | None = None
        self._stop = asyncio.Event()
        self.rest = RestServer(
            self, config.rest, tokens_file, fs, **{"runs_dir": config.ui.runs_dir, **(rest_options or {})}
        )
        self.ui = UiServer(self, config.ui, **(ui_options or {}))
        self.watch, self.tee = Watch(clock), LegacyTee(clock)
        self.watch.host = self.host_id
        self.fleet = FleetCollector(lambda: self.config.fleet, lambda: self.host_id, tokens_file, clock=clock)
        self.watch.disk_busy, self.watch.disk_limit = config.levels.disk_busy, config.levels.disk_limit
        self.ha = HaPush(config.ha, tokens_file, clock=clock)
        self._watch_every = max(1, round(WATCH_EVERY_S / tick_s))
        self._ticks, self._busy_acc = 0, []
        self._prev_procs: tuple[float, dict] | None = None
        self._io_blocks, self._io_block_last = 0, None  # headroom decisions with would_block_io (since start)

    def request_reload(self) -> None:
        """SIGHUP: only note it; `reload_config` runs in the main loop between ticks."""
        self._reload = True

    def reload_config(self) -> None:
        """Re-read the config file; on any error keep the running config. Restart-only settings keep their
        running value and are audited as `needs restart`."""
        self._reload = False
        self.rest.reload_tokens()  # the tokens file is part of the reload; a bad one keeps the current tokens
        try:
            if not self.config_path or not os.path.exists(self.config_path):
                raise ConfigError(f"config file {self.config_path!r} not found")  # never reload to defaults
            new = load_config(self.config_path)
        except (ConfigError, OSError, ValueError) as e:  # ValueError: not UTF-8
            self._audit_reload("reload refused", str(e))
            log.error("reload refused: %s", e)
            return
        old = self.raw_config
        pinned = replace(
            new,
            default_profile=old.default_profile, profiles=old.profiles, compile_lock=old.compile_lock,
            rest=old.rest, ui=old.ui, ha=old.ha, host=replace(new.host, id=old.host.id),
            compat=replace(new.compat, mirror_cgroups=old.compat.mirror_cgroups),
            memguard=replace(new.memguard, projects=old.memguard.projects),
        )  # fmt: skip
        restart = [k for k in changed_keys(old, new) if k not in changed_keys(old, pinned)]
        changed = changed_keys(old, pinned)
        self._apply_config(pinned)
        if old.host.mode != pinned.host.mode:  # a mode switch changes what the daemon may do: always audited
            self.audit.append({"ts": iso(self.clock()), "op": "host_mode_changed", "principal": "system",
                               "source": "reload", "before": old.host.mode, "after": pinned.host.mode,
                               "reason": "[host] mode switched (monitor forces enforce off)"})  # fmt: skip
        if changed or not restart:  # audit first: the new config is live even if the step below fails
            self._audit_reload("reload", ", ".join(changed) or "no changes")
        if restart:
            self._audit_reload("needs restart", ", ".join(restart))
        try:
            self._after_change({})  # audit and persist the slot events the new enforce flag caused
        except Exception:  # like the tick steps: never stop the main loop; the next change drains the rest
            log.exception("reload: persisting the change failed")
        log.info("reload: changed %s; needs restart %s", changed, restart)

    def _audit_reload(self, op: str, reason: str) -> None:
        self.audit.append({"ts": iso(self.clock()), "op": op, "principal": "system", "source": "reload",
                           "reason": reason})  # fmt: skip

    def _apply_config(self, c: Config) -> None:
        """Hand the new config to every engine that holds a part of it. Engines read their flags on use, so
        a flag that goes off stops new actions and the resume rule releases paused work (undo is always
        allowed); a backstop that goes off is rolled back by its next tick, as after a restart."""
        self.raw_config, c = c, apply_host_mode(c)
        self.config = c
        self.sampler.host_mode = c.host.mode
        self.fleet.poke()
        self.audit.configure(c.audit.max_mb, c.audit.keep)
        self.slots.set_enforce(c.slots_enforce)
        self.slots.compile_cfg = c.compile  # live: the next slot decision uses it
        self.slots.url_template = self.jobs.url_template = c.keys.url
        self.governor.enforce = c.governor_enforce
        self.sampler.psi_busy_gate_pct = c.headroom.psi_busy_gate_pct
        self.sampler.disk_busy = self.watch.disk_busy = c.levels.disk_busy
        self.sampler.disk_limit = self.watch.disk_limit = c.levels.disk_limit
        self.backstop.configure(c.backstop)  # project caps are asserted again
        mg = self.memguard
        mg.cfg, mg.reaper_cfg = c.memguard, c.reaper
        mg.signals.enforce = mg.docker.enforce = c.memguard.enforce
        mg.reap_signals.enforce = c.reaper.enforce
        mg.engine.deny = tuple(c.memguard.deny_containers)
        mg.engine.swap_free_min_mb = c.memguard.swap_free_min_mb
        mg.engine.horizon_samples = c.memguard.horizon_samples
        st = self.strays
        st.cfg = st.detector.cfg = c.strays
        st.signals.enforce = c.strays.enforce
        st.detector.project_cwds = {n: g.agent_cwd for n, g in c.memguard.projects.items() if g.agent_cwd}

    def _memory_view(self) -> tuple[int, int, int] | None:
        """(MemAvailable, MemTotal, the memory guard's low floor) in MB from the last sample, for the
        compile slot's memory admission; None before the first sample."""
        m = (self.status or {}).get("memory")
        if not m:
            return None
        return m["avail_mb"], m["total_mb"], guard_thresholds(self.limits.effective()["ram_floor_gb"])[0]

    def _work_running(self, pid: int) -> bool:
        """Build work other than the compile of `pid` and its own ancestry: a registered job but a light one,
        or a held heavy slot (also a legacy holder), unless the waiter runs inside it (its gate or impl job:
        the job whose scope holds the pid or an ancestor, the heavy holder that is an ancestor)."""
        chain = self.slots.ancestry(pid)
        for j in self.jobs.jobs.values():
            if j.kind == "light" or j.pid in chain or any(self.slots._in_scope(c, j.cgroup) for c in chain):
                continue
            return True
        heavy = self.slots.slots["heavy"]
        if heavy.holder:
            return self.slots._ancestor_holder(heavy.holder, [pid]) is None
        return bool(heavy.legacy) and heavy.legacy.get("pid") not in chain

    def _waiting_projects(self) -> list[str]:
        return [w.project for sl in self.slots.slots.values() for w in sl.queue]

    def _configured_projects(self) -> list[str]:
        return [*self.config.headroom.projects, *self.config.memguard.projects]

    def _pause_marker_file(self) -> str | None:
        marker = self.config.memguard.pause_marker
        return marker.file if marker else None

    def tick(self) -> dict:
        s = self.sampler.sample()
        self._expire()  # an expired override must not feed the guard for one more tick
        self._memguard_step(s)  # then the guard's pause/abort, ahead of the slower engines
        self.fast_tick()
        self._watch_step(s)
        self._governor_step(self.sampler.cpu_times)
        self._backstop_step()
        self._strays_step()
        self._history_step(s)
        s["limits"] = self.limits_summary()
        s["rest"] = self.rest.status()
        s["ui"] = self.ui.status()
        s["slots"] = self.slots.summary()
        if "heavy_slot" in s:  # the same stuck rule (held long and no CPU progress) as the slot status
            s["heavy_slot"] = {**s["heavy_slot"], "stuck": s["slots"]["heavy"]["stuck"]}
        s["jobs"] = self.jobs.summary()
        s["light"] = self.light_status()
        s["image"] = self.image_status()
        s["job_stats"] = self.jobs.stats.summary()
        s["projects"] = self.projects.summary()
        s["alerts"] = self.watch.summary()
        s["ha_push"] = self.ha.status()
        s["governor"] = self.governor.status()
        s["memguard"] = self.memguard.status()
        s["headroom"] = self.headroom_status()
        s["backstop"] = self.backstop.status()
        s["strays"] = self.strays.status()
        s["capabilities"] = list(memguard_ops.CAPABILITIES)
        enforce = self.enforce_flags()
        s["daemon"] = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "mode": self.mode(),
            "enforce": enforce,
            "tick_s": self.tick_s,
            "memory_protection_mb": cgroup.effective_memory_min(self.fs) // (1024 * 1024),
        }
        self.status = s
        atomic_write_json(self.status_path, s)
        return s

    def _project_containers(self, project: str) -> str | None:
        """The project's memguard `containers` regex, for attributing Docker CPU to its jobs."""
        guard = self.config.memguard.projects.get(project)
        return guard.containers if guard else None

    def mode(self) -> str:
        """`enforce` when every engine enforces, `partial` when some do, else `observe`."""
        flags = self.enforce_flags().values()
        return "enforce" if all(flags) else "partial" if any(flags) else "observe"

    def enforce_flags(self) -> dict:
        """Which engines act for real (the config's enforce flags), for `daemon.enforce` and `daemon.mode`."""
        c = self.config
        return {
            "governor": c.governor_enforce,
            "slots": c.slots_enforce,
            "memguard": c.memguard.enforce,
            "reaper": c.reaper.enforce,
        }

    def fast_tick(self) -> None:
        """Release slots and drop jobs whose holder or scope is gone (runs every second, see `run`)."""
        changed = self.jobs.tick()
        image_ops.reap(self)  # a running image job whose runner died
        self.projects.sample()
        if self.slots.tick() or changed:
            self._after_change({})

    def _watch_step(self, s: dict) -> None:
        """Every 30 s: evaluate the alerts on the busy share averaged over the interval."""
        self._busy_acc.append(s["cpu"]["busy_pct"])
        self._ticks += 1
        if self._ticks % self._watch_every:
            return
        try:
            busy = round(sum(self._busy_acc) / len(self._busy_acc), 1)
            self._busy_acc.clear()
            now, procs = self.clock(), self.sampler.proc_ticks
            if self._prev_procs and now > self._prev_procs[0]:
                top3 = interval_top3(self._prev_procs[1], procs, now - self._prev_procs[0], self.fs.clk_tck)
            else:  # the first evaluation has no earlier snapshot: the lifetime top 3
                top3 = [{k: t[k] for k in ("pid", "comm", "pcpu", "rss_mb")} for t in s["top"]]
            self._prev_procs = (now, procs)
            eff = self.limits.effective()
            snap = self.limits.snapshot()
            ceiling = snap["ceiling"]["profile"] if snap["ceiling"] else None
            s_now = {**s, "slots": self.slots.summary()}
            self.watch.external = self._image_alerts()
            self._publish(
                self.watch.evaluate(s_now, busy, eff["target_cpu_pct"], snap["profile"], ceiling, top3)
            )
            self._tee(s, busy, top3)
        except Exception:  # alerting must never stop sampling
            log.exception("alert evaluation failed")

    def _governor_step(self, cpu_times: tuple[int, int] | None) -> None:
        """Feed the CPU governor the sample's jiffies and the effective limits (it acts every 10 s)."""
        try:
            eff = self.limits.effective()
            if cpu_times is not None:
                self.governor.tick(cpu_times, eff["max_cores"], eff["target_cpu_pct"])
            else:
                self.governor.clamp(eff["max_cores"])
            self._governor_events()
        except Exception:  # the governor must never stop sampling
            log.exception("governor step failed")

    def _governor_events(self) -> None:
        """Audit each cap change (`WOULD …` in observe mode) and tee it to the legacy governor log."""
        paths = [self.config.compat.governor_log_path] if self.config.compat.governor_log_path else []
        for ev in self.governor.drain_events():
            now = self.clock()
            text = ("" if ev["applied"] else "WOULD ") + event_line(ev)
            self.audit.append({
                "ts": iso(now), "op": "governor", "principal": "system", "source": "governor",
                "result": "applied" if ev["applied"] else "would",
                "reason": text + (f" [{ev['error']}]" if ev["error"] else ""),
                "before": "max" if ev["before"] is None else f"{ev['before']:g}", "after": f"{ev['cap']:g}",
            })  # fmt: skip
            for path in paths:
                try:
                    with open(path, "a") as f:
                        f.write(f"{iso(now)} {text}\n")
                except OSError as e:
                    log.warning("cannot append to governor log %s: %s", path, e.strerror)

    def _memguard_step(self, s: dict) -> None:
        try:
            self.memguard.tick(s)
        except Exception:  # the guard must never stop sampling; the next tick decides again
            log.exception("memory guard step failed")

    def _backstop_step(self) -> None:
        try:
            self.backstop.tick()
        except Exception:  # the backstop must never stop sampling; the next tick decides again
            log.exception("backstop step failed")

    def _strays_step(self) -> None:
        try:
            self.strays.tick()
        except Exception:  # the guard must never stop sampling; the next tick decides again
            log.exception("stray guard step failed")

    def _history_step(self, s: dict) -> None:
        try:
            files = {"mem": "memory.current", "swap": "memory.swap.current", "peak": "memory.peak"}
            raw = {k: cgroup.read_int(self.fs, PRIMARY, f) for k, f in files.items()}
            self.history.record(row_for(s, {k: None if v is None else v // 2**20 for k, v in raw.items()}))
        except Exception:  # history must never stop sampling
            log.exception("history step failed")

    def _save_state(self) -> bool:
        try:
            self.store.save(self.limits.state, self.slots.to_state(), self.jobs.to_state(),
                            self.memguard.engine.to_state(), self.jobs.stats.to_state(),
                            self.image.to_state(), self.host_id, self.jobs.endings.to_state(),
                            compile_peaks=self.jobs.peaks.to_state())  # fmt: skip
        except OSError as e:
            log.error("cannot save state: %s", e)
            return False
        return True

    def _publish(self, events: list[dict]) -> None:
        for ev in events:
            self.ha.enqueue(ev)

    def _tee(self, s: dict, busy: float, top3: list[dict]) -> None:
        """Append the legacy watcher lines to each [compat] alert_log_paths file."""
        kinds = self.tee.evaluate(busy, s["memory"]["avail_mb"], s["swap"]["used_mb"])
        paths = self.config.compat.alert_log_paths
        if not kinds or not paths:
            return
        now = self.clock()
        for kind in kinds:
            line = legacy_line(kind, now, s["cpu"]["load1"], s["cpu"]["psi_some_avg10"],
                               s["memory"]["avail_mb"], s["swap"]["used_mb"], busy, top3)  # fmt: skip
            for path in paths:
                try:
                    with open(path, "a") as f:
                        f.write(line + "\n")
                except OSError as e:
                    log.warning("cannot append to alert log %s: %s", path, e.strerror)

    def emit_event(self, kind: str, payload: dict | None = None) -> dict:
        """Hook of the memory guard: `pause`, `abort`, `low_memory`, `resume` or `reap`."""
        ev = self.watch.emit_event(kind, payload)
        self._publish([ev])
        return ev

    def emit_resolved(self, key: str, title: str) -> dict:
        """A `resolved` event for a key the stray guard raised."""
        ev = self.watch.resolved(key, title)
        self._publish([ev])
        return ev

    def emit_info(self, key: str, title: str, message: str) -> dict:
        ev = self.watch.info(key, title, message)
        self._publish([ev])
        return ev

    def notify_owner_change(self, ctx: dict, what: str) -> None:
        """Tell the phone about an owner-level change made on the host (REST changes are not echoed)."""
        reason = ctx.get("reason") or (ctx.get("request") or {}).get("reason")
        self._publish([self.watch.owner_change(ctx, what, reason)])

    def light_status(self) -> dict:
        """The light lane: running count, caps and the hard limits `loadctl run --kind light` applies."""
        c = self.config.light
        return {
            "running": sum(1 for j in self.jobs.jobs.values() if j.kind == "light"),
            "max": self.limits.effective()["max_light"],
            "per_project_max": c.max_light_per_project,
            "cpu_quota_pct": c.cpu_quota_pct,
            "memory_max_mb": c.memory_max_mb,
            "max_runtime_s": c.max_runtime_s,
        }

    def image_hold(self) -> bool:
        """An owner hold or the drain profile: no new image job starts and no start time is planned."""
        return bool(self.limits.state.hold) or bool(self.limits.effective()["hold_heavy_slot"])

    def image_cfg(self):
        """`[image]` with the owner's runtime override on top: what the train runs on."""
        return self.image.effective(self.config.image)

    def image_status(self, limit: int = VIEW_MAX) -> dict:
        """The image train: the window, and the queue with planned starts (bounded to `limit` entries)."""
        expected = lambda p: self.jobs.stats.expected_s(p, "image")  # noqa: E731
        return self.image.status(
            self.image_cfg(), self.clock(), expected, self.image_hold(), self.config.keys.url, limit
        )

    def _image_alerts(self) -> dict:
        """Alerts of the image train for the watch: a running image job past `max_runtime_min` (never a kill)
        and container build activity outside the window that is no image job (observe only)."""
        cfg, now, found = self.image_cfg(), self.clock(), {}
        jobs = [j for j in self.jobs.jobs.values() if j.kind == "image"]
        for j in jobs:
            if now - j.since > cfg.max_runtime_min * 60:
                msg = (f"image job {j.label} has run {int((now - j.since) // 60)} min "
                       f"(alert after {cfg.max_runtime_min} min); it is not stopped")  # fmt: skip
                found[f"image_overrun:{j.project}"] = (
                    "warning",
                    f"Image build overrunning: {j.project}",
                    msg,
                    [],
                )
        if not window_state(cfg, now)[0]:
            builds = find_builds(self.fs, self.sampler.proc_ticks, [j.cgroup for j in jobs])
            if builds:
                seen = ", ".join(f"{b['what']} (pid {b['pid']})" for b in builds[:3])
                msg = f"container build outside the image window {cfg.window} {cfg.timezone}: {seen}"
                found["image_out_of_window"] = (
                    "warning",
                    "Container build outside the image window",
                    msg,
                    [],
                )
        return found

    def limits_summary(self) -> dict:
        snap = self.limits.snapshot()
        r = snap["restriction"]
        return {
            "profile": snap["profile"],
            "ceiling": snap["ceiling"]["profile"] if snap["ceiling"] else None,
            "ceiling_info": snap["ceiling"],
            "profile_info": snap["profile_info"],
            "overrides": [
                {k: o[k] for k in ("param", "value", "principal", "reason", "set_at", "expires_at")}
                for o in snap["overrides"]
            ],
            "restricted": r["active"],
            "restricted_by": r["by"],
            "freeze": snap["freeze"],
            "effective": snap["effective"],
        }

    def _expire(self) -> None:
        if self.limits.tick():
            self._after_change({})

    def _after_change(self, ctx: dict) -> None:
        """Audit the engine's events, persist the state and rewrite the env shim if values changed."""
        now = self.clock()
        for ev in self.limits.drain_events():
            self.audit.append({"ts": iso(now), **ctx, **ev})
        self.slots.set_profile_hold(self.limits.effective()["hold_heavy_slot"])
        self._governor_step(None)  # a lowered max_cores clamps the cap now, not at the next sample
        for ev in [
            *self.slots.drain_events(),
            *self.jobs.drain_events(),
        ]:  # the holder's pid and request win; the caller's is kept
            req = {**ev["request"], "caller_pid": ctx.get("pid")}
            self.audit.append(
                {"ts": iso(now), **ctx, **{k: v for k, v in ev.items() if v is not None}, "request": req}
            )
        self._save_state()
        self.sync_env()

    def sync_env(self, force: bool = False) -> None:
        if not self.env_file:
            return
        snap = self.limits.snapshot()
        ceiling = snap["ceiling"]["profile"] if snap["ceiling"] else None
        key = repr((sorted(snap["effective"].items()), snap["profile"], ceiling))
        if key == self._env_key and not force:
            return
        try:
            write_env(self.env_file, render_env(snap["effective"], snap["profile"], ceiling, self.clock()))
            self._env_key = key
        except OSError as e:
            log.error("cannot write %s: %s", self.env_file, e)

    def _link_compat(self) -> None:
        if not self.compat_link:
            return
        try:
            if os.path.islink(self.compat_link) or not os.path.exists(self.compat_link):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.compat_link)
                os.symlink(self.status_path, self.compat_link)
        except OSError as e:
            log.warning("cannot link %s: %s", self.compat_link, e)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                return
            try:
                req = decode(line)
            except ValueError as e:
                reply = {"ok": False, "error": "bad_request", "message": str(e)}
            else:
                try:
                    reply = self.dispatch(req, self._peer_pid(writer))
                except Exception:  # a bad request must not drop the connection or the daemon
                    log.exception("dispatch failed")
                    reply = {"ok": False, "error": "internal", "message": "internal error"}
            writer.write(encode(reply))
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    @staticmethod
    def _peer_pid(writer: asyncio.StreamWriter) -> int | None:
        try:
            sock = writer.get_extra_info("socket")
            return struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            )[0]
        except (OSError, AttributeError, struct.error):
            return None

    def dispatch(self, req: dict, peer_pid: int | None = None) -> dict:
        if req.get("v") != VERSION:
            return {"ok": False, "error": "bad_version", "message": f"expected v={VERSION}"}
        op = req.get("op")
        if op == "status":
            if self.status is None:
                return {"ok": False, "error": STARTING, "message": "no sample yet"}
            slots = self.slots.summary()
            heavy = (
                {**self.status["heavy_slot"], "stuck": slots["heavy"]["stuck"]}
                if "heavy_slot" in self.status
                else {}
            )
            return {
                "ok": True,
                "status": {
                    **self.status,
                    "host": {**self.status["host"], "mode": self.config.host.mode},
                    **({"heavy_slot": heavy} if heavy else {}),
                    "limits": self.limits_summary(),
                    "slots": slots,
                    "jobs": self.jobs.summary(),
                    "light": self.light_status(),
                    "image": self.image_status(),
                    "job_stats": self.jobs.stats.summary(),
                    "projects": self.projects.summary(),
                    "rest": self.rest.status(),
                    "ui": self.ui.status(),
                    "alerts": self.watch.summary(),
                    "ha_push": self.ha.status(),
                    "governor": self.governor.status(),
                    "memguard": self.memguard.status(),
                    "headroom": self.headroom_status(),
                    "backstop": self.backstop.status(),
                    "strays": self.strays.status(),
                    "capabilities": list(memguard_ops.CAPABILITIES),
                },
            }
        if op == "fleet":
            return {"ok": True, "fleet": self.fleet_view()}
        if op == "job_endings":
            return job_ops.endings(self, req.get("args"))
        if op in ("slot_acquire", "slot_release", "slot_status", "hold", "cancel_holder"):
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return slot_ops.handle(self, op, args or {}, peer_pid)
        if op in IMAGE_OPS:
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return image_ops.handle(self, op, args or {}, peer_pid)
        if op in ("job_start", "job_end"):
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return job_ops.handle(self, op, args or {}, peer_pid)
        if op in ("freeze", "unfreeze", "reap"):
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return memguard_ops.handle(self, op, args or {}, peer_pid)
        if op == "stray_kill":
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return stray_ops.handle(self, args or {}, peer_pid)
        if op in ("set", "ceiling_set", "ceiling_clear", "reset"):
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return self._mutate(op, args or {}, peer_pid)
        if op == "limits":
            self._expire()
            return {"ok": True, "limits": self.limits.snapshot()}
        if op == "headroom":
            return self._headroom(req.get("args"))
        if op == "history":
            n = (req.get("args") or {}).get("n", 40)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return {"ok": False, "error": "invalid_args", "message": "n must be an integer >= 0"}
            return {"ok": True, "columns": list(COLUMNS), "rows": self.history.tail(min(n, SOCKET_MAX))}
        if op == "log":
            n = (req.get("args") or {}).get("n", 20)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                return {"ok": False, "error": "invalid_args", "message": "n must be an integer >= 0"}
            return {"ok": True, "lines": self.audit.tail(min(n, 1000))}
        return {"ok": False, "error": "unknown_op", "message": f"unknown op {op!r}"}

    def fleet_view(self) -> dict:
        """This host from its own status plus the last result of every `[fleet]` host (read-only)."""
        local = self.dispatch({"v": VERSION, "op": "status"})
        return build_view(self.host_id, local.get("status") if local.get("ok") else None,
                          self.config.fleet, self.fleet.results(), self.clock())  # fmt: skip

    def headroom_status(self) -> dict:
        """Decisions whose io PSI was over its limit, with the io gate on or off (trial counter)."""
        return {"io_would_block_count": self._io_blocks, "io_would_block_last": self._io_block_last}

    def _headroom(self, args) -> dict:
        """Is there room for one more heavy job of `kind` for `project`? Uses the latest sample."""
        args = {} if args is None else args
        kind = args.get("kind") if isinstance(args, dict) else None
        project = args.get("project") if isinstance(args, dict) else None
        if kind not in KINDS or not (project is None or isinstance(project, str)):
            return {
                "ok": False,
                "error": "bad_request",
                "message": f"args: kind ({', '.join(KINDS)}) required; project must be a string",
            }
        if self.status is None:
            return {"ok": False, "error": STARTING, "message": "no sample yet"}
        age = self.clock() - calendar.timegm(time.strptime(self.status["at"], "%Y-%m-%dT%H:%M:%SZ"))
        if age > 3 * self.tick_s:  # the tick loop stalled: never decide on old numbers
            return {"ok": False, "error": "not_ready", "message": f"stale sample ({age:.0f} s old)"}
        self._expire()
        snap = self.limits.snapshot()
        default = asdict(self.config.profiles[self.config.default_profile])
        factor = profile_factor(snap["effective"], default)
        th = thresholds(self.config.headroom, kind, project, self.fs.cpu_count)
        over = args.get("overrides")
        if over is not None:
            bad = self._bad_overrides(over)
            if bad:
                return {"ok": False, "error": "bad_request", "message": bad}
            th = replace(th, **over)  # replaces the configured value; the profile scaling still applies
        h = self.config.headroom
        paused = paused_reasons(self.fs, kind, snap["hold"], snap["freeze"], self._pause_marker_file())
        if kind == "review":  # the review lane: memory floor and the parallel cap only
            running = sum(1 for j in self.jobs.jobs.values() if j.kind == "review")
            d = decide_review(
                self.status, th, running=running, max_reviewers=snap["effective"]["max_reviewers"],
                ram_floor_gb=snap["effective"]["ram_floor_gb"],
                guard_paused=bool(self.memguard.engine.paused),
                factor=factor, io_psi_gate=h.io_psi_gate, io_psi_max=h.io_psi_max, paused=paused,
            )  # fmt: skip
        elif kind == "light":  # the light lane: memory floor, pause and the two parallel caps only
            light = [j for j in self.jobs.jobs.values() if j.kind == "light"]
            name = project or DEFAULT_PROJECT
            d = decide_light(
                self.status, th, project=name, running=len(light),
                project_running=sum(1 for j in light if j.project == name),
                max_light=snap["effective"]["max_light"],
                max_per_project=self.config.light.max_light_per_project,
                ram_floor_gb=snap["effective"]["ram_floor_gb"],
                guard_paused=bool(self.memguard.engine.paused),
                factor=factor, io_psi_gate=h.io_psi_gate, io_psi_max=h.io_psi_max, paused=paused,
            )  # fmt: skip
        else:
            d = decide(
                self.status, th, factor=factor, io_psi_gate=h.io_psi_gate, io_psi_max=h.io_psi_max,
                paused=paused,
            )  # fmt: skip
        if d.would_block_io:
            self._io_blocks += 1
            self._io_block_last = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))
        return {
            "ok": True,
            "kind": kind,
            "project": project or DEFAULT_PROJECT,
            "decision": d.to_dict(),
            "thresholds": d.thresholds,
            "factor": d.factor,
            "timing": {"confirm_s": th.confirm_s, "poll_s": th.poll_s, "max_wait_s": th.max_wait_s},
            "sample_at": self.status["at"],
            "sample_at_ms": self.status.get("at_ms"),
        }

    @staticmethod
    def _bad_overrides(over) -> str | None:
        if not isinstance(over, dict):
            return "overrides must be an object"
        for k, v in over.items():
            if k not in HEADROOM_OVERRIDES:
                return f"overrides: unknown key {k!r} (use {', '.join(HEADROOM_OVERRIDES)})"
            if isinstance(v, bool) or not isinstance(v, int | float) or v < 0:
                return f"overrides.{k} must be a number >= 0"
        return None

    @staticmethod
    def _bad_types(args: dict) -> str | None:
        """Wrong JSON types are a bad_request (range and meaning errors are invalid_args)."""
        for k in ("profile", "reason", "project", "ttl", "until"):
            if args.get(k) is not None and not isinstance(args[k], str):
                return f"{k} must be a string"
        for k in PARAM_ARGS:
            v = args.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int | float)):
                return f"{k} must be a number"
        p = args.get("params")
        if p is not None and (not isinstance(p, list) or not all(isinstance(x, str) for x in p)):
            return "params must be a list of strings"
        if args.get("all") is not None and not isinstance(args["all"], bool):
            return "all must be a boolean"
        if "owner" in args and not isinstance(args["owner"], bool):
            return "owner must be a boolean"
        return None

    def rest_refused(self, client: str, peer: str, method: str, path: str) -> None:
        """Audit a write attempt with a read-only token (the listener refused it with 403 `read_only`)."""
        self.audit.append({
            "ts": iso(self.clock()), "op": "rest_write", "principal": "agent", "source": "rest",
            "peer": peer, "client": client, "result": "refused",
            "request": {"method": method, "path": path}, "reason": "read-only token on a write path",
        })  # fmt: skip

    def rest_tokens_refused(self, reason: str) -> None:
        """Audit a reload that refused the tokens file (the listener keeps its current tokens)."""
        self.audit.append({
            "ts": iso(self.clock()), "op": "rest_tokens", "principal": "system", "source": "reload",
            "result": "refused", "reason": reason,
        })  # fmt: skip

    def rest_call(self, op: str, args: dict, peer: str, client: str = "default") -> dict:
        """A REST request: the owner (the token and its scope were checked by the listener), source `rest`.
        Every audit line written while it runs carries the client's name."""
        self.audit.client = client
        try:
            return self._rest_call(op, args, peer)
        finally:
            self.audit.client = None

    def _rest_call(self, op: str, args: dict, peer: str) -> dict:
        if op == "cancel_holder":
            return slot_ops.cancel_holder(self, args, None, rest_peer=peer)
        if op == "hold":
            return slot_ops.rest_hold(self, args, peer)
        if op in ("freeze", "unfreeze", "reap"):
            return memguard_ops.rest(self, op, args, peer)
        if op == "stray_kill":
            return stray_ops.rest(self, args, peer)
        if op in ("image_now", "image_cancel"):
            return image_ops.rest(self, op, args, peer)
        return self._mutate(op, args, None, rest_peer=peer)

    def _mutate(self, op: str, args: dict, peer_pid: int | None, rest_peer: str | None = None) -> dict:
        bad = self._bad_types(args)
        if bad:
            return {"ok": False, "error": "bad_request", "message": bad}
        if op == "set" and any(k in args for k in image_ops.SET_KEYS):
            if any(args.get(k) is not None for k in (*PARAM_ARGS, "profile", "ttl", "until", "project")):
                return {"ok": False, "error": "invalid_args",
                        "message": "image settings cannot be combined with limit settings"}  # fmt: skip
            return image_ops.set_settings(self, args, peer_pid, rest_peer)
        self._expire()  # replies and decisions use exact expiry, not the last tick's
        ctx = {**caller_info(self.fs, peer_pid), "op": op, "project": args.get("project"), "request": args}
        principal = "agent"
        if rest_peer is not None:
            ctx.update(source="rest", peer=rest_peer)
            principal = "owner"
        elif op != "set" or args.get("owner") is True:  # ceilings are owner-only
            why = owner_denial(self.fs, peer_pid, self.config.principal)
            if why or args.get("owner") is not True:
                why = why or "owner: true is required for this operation"
                self.audit.append(
                    {"ts": iso(self.clock()), **ctx, "principal": "agent", "result": "refused", "reason": why}
                )
                return {"ok": False, "error": "owner_denied", "message": why}
            principal = "owner"
        reason = args.get("reason")
        if op != "ceiling_clear" and (not isinstance(reason, str) or not reason.strip()):
            return {"ok": False, "error": "invalid_args", "message": "reason is required"}
        ctx["reason"] = reason
        try:
            if args.get("ttl") is not None and args.get("until") is not None:
                raise ValueError("give ttl or until, not both")
            ttl = parse_ttl(args["ttl"]) if args.get("ttl") is not None else None
            until = parse_until(args["until"], self.clock()) if args.get("until") is not None else None
            if op == "set":
                changes = {k: args[k] for k in PARAM_ARGS if args.get(k) is not None}
                if not changes and not args.get("profile"):
                    raise ValueError("nothing to set")
                reply = self.limits.set(
                    principal, changes, reason, ttl, until, args.get("profile"), args.get("project")
                )
            elif op == "reset":
                reply = self.limits.reset(reason, args.get("params"), args.get("all") is True)
            elif op == "ceiling_set":
                reply = self.limits.ceiling_set(str(args.get("profile")), reason, ttl, until)
            else:
                reply = self.limits.ceiling_clear(reason=reason or "")
        except (ValueError, KeyError) as e:  # ConfigError is a ValueError
            return {"ok": False, "error": "invalid_args", "message": str(e)}
        self._after_change({**ctx, "principal": principal})
        if (
            principal == "owner"
            and rest_peer is None
            and reply.get("ok")
            and reply.get("cleared") is not False
        ):
            self.notify_owner_change(ctx, op)
        return reply

    async def run(self) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        self.sampler.prime()
        self._link_compat()
        self._expire()  # drop entries that expired while the daemon was down
        self._after_change({})  # audit/persist what loading or expiring changed
        self.sync_env(force=True)
        old_umask = os.umask(0o177)  # socket 0600
        try:
            server = await asyncio.start_unix_server(self.handle, path=self.socket_path)
        finally:
            os.umask(old_umask)
        rest_task = asyncio.create_task(self.rest.supervise()) if self.config.rest.enabled else None
        ui_task = asyncio.create_task(self.ui.supervise()) if self.config.ui.enabled else None
        ha_task = asyncio.create_task(self.ha.run()) if self.config.ha.enabled else None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        loop.add_signal_handler(signal.SIGHUP, self.request_reload)
        self.fleet.start()
        heartbeat = Heartbeat(watchdog_interval_s())
        sd_notify("READY=1")  # the first sample and tick come after this: nothing slow delays the start
        heartbeat.start()
        log.info("loadgovd %s ready (%s mode) on %s", __version__, self.mode(), self.socket_path)
        async with server:
            step = min(self.tick_s, FAST_TICK_S)
            next_full = time.monotonic() + min(1.0, self.tick_s)  # the first tick, as before
            while not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=step)
                if self._stop.is_set():
                    break
                try:
                    if self._reload:
                        self.reload_config()
                    if time.monotonic() >= next_full - 0.01:
                        next_full = max(next_full + self.tick_s, time.monotonic())
                        self.tick()
                        heartbeat.beat()
                    else:
                        self.fast_tick()  # a scope that emptied frees its slot within a second
                except Exception:  # keep the loop alive; the heartbeat stops if no tick succeeds
                    log.exception("tick failed")
        heartbeat.stop()
        self.fleet.stop()
        tasks = [t for t in (rest_task, ui_task, ha_task) if t]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        sd_notify("STOPPING=1")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="loadgovd", description=__doc__)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--run-dir", default="/run/loadgovd")
    ap.add_argument("--compat-link", default="/run/loadgovd.json", help="'' to disable")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="TOML file; missing = built-in defaults")
    ap.add_argument("--state-dir", default="/var/lib/loadgov")
    ap.add_argument("--log-dir", default="/var/log/loadgov")
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="'' to disable")
    ap.add_argument("--tokens-file", default=DEFAULT_TOKENS_FILE, help="env file with LOADGOV_REST_TOKEN")
    ap.add_argument("--tick", type=float, default=5.0)
    ap.add_argument("--proc-root", default="/proc")
    ap.add_argument("--cgroup-root", default="/sys/fs/cgroup")
    ap.add_argument("--lock-root", default="/run/lock")
    ap.add_argument("--disk", action="append", dest="disks")
    ap.add_argument(
        "--recover",
        action="store_true",
        help="thaw everything the paused registry holds (daemon stopped); needs --force",
    )
    ap.add_argument("--force", action="store_true", help="with --recover: really thaw (else only list)")
    ap.add_argument(
        "--check-config", metavar="PATH", help="validate a config file, print the error, exit 0/1"
    )
    a = ap.parse_args(argv)
    if a.check_config:
        try:
            cfg = load_config(a.check_config)
        except (ConfigError, OSError, ValueError) as e:  # ValueError: not UTF-8
            print(f"loadgovd: bad config {a.check_config}: {e}", file=sys.stderr)
            return 1
        if cfg.ui.enabled and not os.path.isfile(os.path.join(cfg.ui.dir, "index.html")):
            print(
                f"loadgovd: warning: [ui] is enabled but no build is at {cfg.ui.dir} "
                "(the UI answers 503 until it is installed, see ui/README.md)",
                file=sys.stderr,
            )
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    fs = HostFS(
        proc_root=a.proc_root,
        cgroup_root=a.cgroup_root,
        lock_root=a.lock_root,
        disks=tuple(a.disks or ["/"]),
    )
    if a.recover:  # the one thaw-all never depends on a valid config; the marker is best effort
        try:
            marker = load_config(a.config).memguard.pause_marker
        except Exception as e:  # any parse failure: thawing must still happen
            print(
                f"loadgovd: --recover: config {a.config} not usable ({type(e).__name__}: {e}); "
                "the pause marker is not removed",
                file=sys.stderr,
            )
            marker = None
        return recover.run(fs, a.state_dir, a.socket, a.force, marker=marker)
    try:
        config = load_config(a.config)
    except (ConfigError, OSError) as e:
        print(f"loadgovd: bad config {a.config}: {e}", file=sys.stderr)
        return 2
    d = Daemon(fs, a.socket, a.run_dir, a.compat_link or None, a.tick, config, a.state_dir, a.log_dir,
               a.env_file or None, tokens_file=a.tokens_file, config_path=a.config)  # fmt: skip
    asyncio.run(d.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
