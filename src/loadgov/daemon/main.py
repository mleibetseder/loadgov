"""loadgovd: sample the host every tick, publish status.json, decide limits, answer on the unix socket.

Nothing is enforced unless the matching `enforce` flag is on (AGENTS.md rule 2). The daemon writes
nothing outside its run, state and log directories and the compat env file without it.

`loadgovd --recover --force` is the one thaw-all: it releases everything the paused registry
holds; it runs with the daemon stopped. Stopping or killing the daemon never thaws anything.

Watchdog: `WATCHDOG=1` goes to systemd only after a tick that completed without an exception. A
persistently failing tick therefore stops the keep-alive and systemd restarts the daemon after
`WatchdogSec` (60 s) by design; one failing tick alone is logged and the loop carries on.
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
import time
from dataclasses import asdict, replace

from loadgov import __version__
from loadgov.config import (
    DEFAULT_CONFIG,
    DEFAULT_PROJECT,
    KINDS,
    Config,
    ConfigError,
    load_config,
    thresholds,
)
from loadgov.daemon import backstop_ops, job_ops, memguard_ops, recover, slot_ops
from loadgov.daemon.audit import AuditLog
from loadgov.daemon.compat import render_env, write_env
from loadgov.daemon.ha_push import HaPush
from loadgov.daemon.principal import caller_info, owner_denial
from loadgov.daemon.rest import DEFAULT_TOKENS_FILE, RestServer
from loadgov.daemon.state import StateStore
from loadgov.engines import slots as slots_engine
from loadgov.engines.governor import PRIMARY, Governor, event_line
from loadgov.engines.headroom import decide, paused_reasons, profile_factor
from loadgov.engines.history import COLUMNS, SOCKET_MAX, History, row_for
from loadgov.engines.jobs import JobTable
from loadgov.engines.limits import Limits, iso
from loadgov.engines.slots import SlotManager
from loadgov.engines.timers import parse_ttl, parse_until
from loadgov.engines.watch import LegacyTee, Watch, interval_top3, legacy_line
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS
from loadgov.protocol import DEFAULT_SOCKET, VERSION, decode, encode
from loadgov.sample import Sampler

DEFAULT_ENV_FILE = "/run/lock/load-governor.env"

log = logging.getLogger("loadgovd")
FAST_TICK_S = 1.0
HEADROOM_OVERRIDES = ("load1_max", "load5_max", "psi_max", "mem_min_gb", "swap_max_mb", "mem_psi_max")
WATCH_EVERY_S = 30  # alert evaluation interval
PARAM_ARGS = ("max_cores", "target_cpu_pct", "load_max", "max_implementers", "ram_floor_gb")


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
        memguard_options: dict | None = None,  # test-only MemGuardRuntime arguments (fake signals, docker)
        backstop_options: dict | None = None,  # test-only BackstopRuntime arguments (target slice)
    ):
        self.fs = fs
        self.clock = clock
        config = config or Config()
        self.config = config
        self.store, self.audit, self.env_file = StateStore(state_dir), AuditLog(log_dir), env_file
        self.limits = Limits(
            config.profiles,
            self.store.load(config.default_profile, set(config.profiles)),
            clock,
            config.default_profile,
        )
        slots_engine.set_compile_lock(config.compile_lock)
        self.slots = SlotManager(fs, clock, config.slots_enforce)
        self.slots.restore(self.store.load_slots())
        self.jobs = JobTable(fs, clock)
        self.governor = Governor(fs, clock, config.governor_enforce, config.compat.mirror_cgroups)
        self.runner = slot_ops.run_argv
        self.jobs.restore(self.store.load_jobs())
        self.memguard = memguard_ops.MemGuardRuntime(self, **(memguard_options or {}))
        self.memguard.restore(self.store.load_memguard())  # paused work stays paused: re-adopt
        self.backstop = backstop_ops.BackstopRuntime(
            fs, config.backstop, clock, state_dir, self.emit_event, self.audit.append,
            lambda: self.jobs.jobs, **(backstop_options or {}),
        )  # fmt: skip
        self.history = History(os.path.join(state_dir, "history.csv"), clock)
        if self.slots.owner_hold_reason() and not self.limits.state.hold:
            self.limits.set_flag("hold", self.slots.owner_hold_reason())  # the flag follows the saved hold
        self._env_key: str | None = None
        self.sampler = Sampler(fs, config.compat.mirror_cgroups)
        self.socket_path = socket_path
        self.run_dir = run_dir
        self.status_path = os.path.join(run_dir, "status.json")
        self.compat_link = compat_link
        self.tick_s = tick_s
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.status: dict | None = None
        self._stop = asyncio.Event()
        self.rest = RestServer(self, config.rest, tokens_file, fs, **(rest_options or {}))
        self.watch, self.tee = Watch(clock), LegacyTee(clock)
        self.ha = HaPush(config.ha, tokens_file, clock=clock)
        self._watch_every = max(1, round(WATCH_EVERY_S / tick_s))
        self._ticks, self._busy_acc = 0, []
        self._prev_procs: tuple[float, dict] | None = None
        self._io_blocks, self._io_block_last = 0, None  # headroom decisions with would_block_io (since start)

    def _pause_marker_file(self) -> str | None:
        marker = self.config.memguard.pause_marker
        return marker.file if marker else None

    def tick(self) -> dict:
        s = self.sampler.sample()
        self._expire()
        self.fast_tick()
        self._watch_step(s)
        self._governor_step(self.sampler.cpu_times)
        self._memguard_step(s)
        self._backstop_step()
        self._history_step(s)
        s["limits"] = self.limits_summary()
        s["rest"] = self.rest.status()
        s["slots"] = self.slots.summary()
        s["jobs"] = self.jobs.summary()
        s["alerts"] = self.watch.summary()
        s["ha_push"] = self.ha.status()
        s["governor"] = self.governor.status()
        s["memguard"] = self.memguard.status()
        s["headroom"] = self.headroom_status()
        s["backstop"] = self.backstop.status()
        s["capabilities"] = list(memguard_ops.CAPABILITIES)
        s["daemon"] = {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "mode": "observe",
            "tick_s": self.tick_s,
        }
        self.status = s
        atomic_write_json(self.status_path, s)
        return s

    def fast_tick(self) -> None:
        """Release slots and drop jobs whose holder or scope is gone (runs every second, see `run`)."""
        changed = self.jobs.tick()
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
                            self.memguard.engine.to_state())  # fmt: skip
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

    def notify_owner_change(self, ctx: dict, what: str) -> None:
        """Tell the phone about an owner-level change made on the host (REST changes are not echoed)."""
        reason = ctx.get("reason") or (ctx.get("request") or {}).get("reason")
        self._publish([self.watch.owner_change(ctx, what, reason)])

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
                return {"ok": False, "error": "not_ready", "message": "no sample yet"}
            return {
                "ok": True,
                "status": {
                    **self.status,
                    "limits": self.limits_summary(),
                    "slots": self.slots.summary(),
                    "jobs": self.jobs.summary(),
                    "rest": self.rest.status(),
                    "alerts": self.watch.summary(),
                    "ha_push": self.ha.status(),
                    "governor": self.governor.status(),
                    "memguard": self.memguard.status(),
                    "headroom": self.headroom_status(),
                    "backstop": self.backstop.status(),
                    "capabilities": list(memguard_ops.CAPABILITIES),
                },
            }
        if op in ("slot_acquire", "slot_release", "slot_status", "hold", "cancel_holder"):
            args = req.get("args")
            if args is not None and not isinstance(args, dict):
                return {"ok": False, "error": "bad_request", "message": "args must be an object"}
            return slot_ops.handle(self, op, args or {}, peer_pid)
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
            return {"ok": False, "error": "not_ready", "message": "no sample yet"}
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
        d = decide(
            self.status, th, factor=factor, io_psi_gate=h.io_psi_gate, io_psi_max=h.io_psi_max, paused=paused
        )
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

    def rest_call(self, op: str, args: dict, peer: str) -> dict:
        """A REST request: always the owner (the token was checked by the listener), source `rest`."""
        if op == "cancel_holder":
            return slot_ops.cancel_holder(self, args, None, rest_peer=peer)
        if op == "hold":
            return slot_ops.rest_hold(self, args, peer)
        if op in ("freeze", "unfreeze", "reap"):
            return memguard_ops.rest(self, op, args, peer)
        return self._mutate(op, args, None, rest_peer=peer)

    def _mutate(self, op: str, args: dict, peer_pid: int | None, rest_peer: str | None = None) -> dict:
        bad = self._bad_types(args)
        if bad:
            return {"ok": False, "error": "bad_request", "message": bad}
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
        await asyncio.sleep(min(1.0, self.tick_s))
        try:
            self.tick()
        except Exception:  # serve not_ready and retry next tick rather than restart-looping
            log.exception("first tick failed")
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
        ha_task = asyncio.create_task(self.ha.run()) if self.config.ha.enabled else None
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stop.set)
        sd_notify("READY=1")
        log.info("loadgovd %s ready (observe mode) on %s", __version__, self.socket_path)
        async with server:
            step = min(self.tick_s, FAST_TICK_S)
            next_full = time.monotonic() + self.tick_s
            while not self._stop.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=step)
                if self._stop.is_set():
                    break
                try:
                    if time.monotonic() >= next_full - 0.01:
                        next_full += self.tick_s
                        self.tick()
                        sd_notify("WATCHDOG=1")
                    else:
                        self.fast_tick()  # a scope that emptied frees its slot within a second
                except Exception:  # keep the loop alive; systemd's watchdog catches real stalls
                    log.exception("tick failed")
        tasks = [t for t in (rest_task, ha_task) if t]
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
    a = ap.parse_args(argv)
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
               a.env_file or None, tokens_file=a.tokens_file)  # fmt: skip
    asyncio.run(d.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
