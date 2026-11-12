"""Alert engine (pure, injectable clock): evaluated every 30 s on the daemon's samples.

Conditions become alerts with a stable `key`; an alert fires at most once per `dedupe_s` per key (a rise
in level fires at once), and a `resolved` event follows once when the condition clears. `emit_event` is
the hook the memory guard calls (`pause`, `abort`, `low_memory`, `resume`, `reap`). Nothing here
does I/O: the daemon pushes the returned events to Home Assistant and the legacy alert log.
"""

from __future__ import annotations

import time
from collections import deque

from loadgov.engines.levels import LIMITS
from loadgov.engines.limits import iso

DEDUPE_S = 600
KEEP_EVENTS = 20
CPU_MARGIN_PCT = 5  # alert above target_cpu_pct + 5 (legacy: > 70 with target 65)
CPU_CONSECUTIVE = 3
MEM_ALERT_MB = 3000
SWAP_GROWTH_MB = 768  # the legacy guard's swap-growth step
CALM_AVAIL_MB = 6000  # the legacy guard's high-water mark
CALM_RESET_EVALS = 20  # 10 minutes of calm (legacy guard: 120 samples at 5 s): then the baseline follows swap
CLEAR_EVALS = 2  # a condition must stay clear this long before `resolved` is sent
LEGACY_SWAP_MB = 2048
LEGACY_CPU_PCT = 70
RANK = {"info": 0, "warning": 1, "critical": 2}
HOOK_KINDS = {  # emit_event kind -> (level, title, actions)
    "pause": ("warning", "Worker paused by the memory guard", ["unfreeze"]),
    "abort": ("critical", "Compile aborted by the memory guard", []),
    "low_memory": ("critical", "Memory is low", []),
    "resume": ("info", "Paused workers resumed by the memory guard", []),
    "reap": ("warning", "Build daemons stopped by the memory guard", []),
    "stuck_paused": ("critical", "Orphaned paused work cannot be thawed", []),
    "backstop": ("critical", "Memory backstop killed a build job", []),
}
ACTIONS = {
    "cpu": ["profile=silent", "freeze"],
    "stuck_holder": ["cancel_holder"],
    "disk": ["prune_caches"],
}
ACTION_LABELS = {"unfreeze": "Resume paused workers"}  # for the Home Assistant side (documented)


def interval_top3(prev: dict, cur: dict, dt_s: float, clk_tck: int) -> list[dict]:
    """Top 3 processes by CPU share over the interval; dicts are pid -> (start, ticks, comm, rss_mb).

    A pid that is new, or whose start time changed (pid reuse), counts all its ticks, so a fresh rustc
    is named rather than a long-running service with a high lifetime average.
    """
    rows = []
    for pid, (start, ticks, comm, rss_mb) in cur.items():
        old = prev.get(pid)
        used = ticks - (old[1] if old and old[0] == start else 0)
        if used > 0 and dt_s > 0:
            pcpu = round(100.0 * used / clk_tck / dt_s, 1)
            rows.append({"pid": pid, "comm": comm, "pcpu": pcpu, "rss_mb": rss_mb})
    return sorted(rows, key=lambda r: r["pcpu"], reverse=True)[:3]


def legacy_line(kind: str, now: float, load1: float, cpu_some10: float, avail_mb: int, swap_mb: int,
                busy_pct: float, top3: list[dict]) -> str:  # fmt: skip
    """The legacy watcher alert line: `ALERT <kind> <HH:MM:SS> load=... busy=N% top: name(pcpu%,NMB) ...`."""
    top = "".join(f"{t['comm']}({t['pcpu']:.1f}%,{t['rss_mb']}MB) " for t in top3)
    return (
        f"ALERT {kind} {time.strftime('%H:%M:%S', time.gmtime(now))} load={load1:.2f} "
        f"cpu_some10={cpu_some10:.2f} mem_avail_mb={avail_mb} swap_used_mb={swap_mb} "
        f"busy={int(busy_pct)}% top: {top}"
    )


class LegacyTee:
    """The legacy host watcher's three conditions with its 10-minute dedupe, for the legacy alert log."""

    def __init__(self, clock=time.time, dedupe_s: int = DEDUPE_S):
        self.clock, self.dedupe_s = clock, dedupe_s
        self._hi = 0
        self._last: dict[str, float] = {}

    def evaluate(self, busy_pct: float, avail_mb: int, swap_mb: int) -> list[str]:
        now = self.clock()
        self._hi = self._hi + 1 if int(busy_pct) > LEGACY_CPU_PCT else 0
        due = {
            "cpu": self._hi >= CPU_CONSECUTIVE,
            "memory": avail_mb < MEM_ALERT_MB,
            "swap": swap_mb > LEGACY_SWAP_MB and avail_mb < CALM_AVAIL_MB,
        }
        out = []
        for kind, hit in due.items():
            if hit and now - self._last.get(kind, -1e18) >= self.dedupe_s:
                self._last[kind] = now
                out.append(kind)
        return out


class Watch:
    def __init__(self, clock=time.time, dedupe_s: int = DEDUPE_S):
        self.clock, self.dedupe_s = clock, dedupe_s
        self.active: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=KEEP_EVENTS)
        self._last_sent: dict[str, float] = {}
        self._sent_level: dict[str, int] = {}
        self._cpu_hi = 0
        self._swap_base: int | None = None
        self._calm = 0
        self._ctx = {"avail_mb": 0, "swap_mb": 0, "profile": None, "ceiling": None, "top3": []}

    # ---- evaluation ------------------------------------------------------------------------------

    def evaluate(self, s: dict, busy_pct: float, target_cpu_pct: float, profile: str,
                 ceiling: str | None, top3: list[dict]) -> list[dict]:  # fmt: skip
        """One 30 s evaluation. `s` is the status sample; `busy_pct` the CPU share over the interval."""
        now = self.clock()
        avail, swap = s["memory"]["avail_mb"], s["swap"]["used_mb"]
        self._ctx = {"avail_mb": avail, "swap_mb": swap, "profile": profile, "ceiling": ceiling,
                     "top3": top3}  # fmt: skip
        found: dict[str, tuple] = {}

        limit = target_cpu_pct + CPU_MARGIN_PCT
        self._cpu_hi = self._cpu_hi + 1 if busy_pct > limit else 0
        if self._cpu_hi >= CPU_CONSECUTIVE:
            msg = (
                f"CPU {busy_pct:g}% busy, above {limit:g}% (target {target_cpu_pct:g}%) "
                f"for {self._cpu_hi} checks"
            )
            found["cpu"] = ("warning", "CPU busy", msg, ACTIONS["cpu"])

        if avail < MEM_ALERT_MB:
            msg = f"{avail} MB available (alert below {MEM_ALERT_MB} MB)"
            found["memory"] = ("critical", "Memory low", msg, [])

        growth = self._swap_growth(swap, avail)
        if growth > SWAP_GROWTH_MB and avail < CALM_AVAIL_MB:
            lvl = "critical" if avail < MEM_ALERT_MB else "warning"
            msg = f"Swap grew {growth} MB over the calm baseline (now {swap} MB) with {avail} MB available"
            found["swap"] = (lvl, "Swap growing", msg, [])

        for name, sl in s.get("slots", {}).items():
            stale = sl.get("stuck") or (name == "heavy" and s.get("heavy_slot", {}).get("stuck"))
            if sl.get("state") == "held" and stale:  # an owner hold is not a stuck job (HA reminds)
                who = sl.get("holder") or "an unnamed holder"
                msg = f"The {name} slot has been held since {sl.get('held_since')} by {who}"
                found[f"stuck_holder:{name}"] = ("warning", f"{name} slot held too long", msg,
                                                 ACTIONS["stuck_holder"])  # fmt: skip

        free = s["disk"]["free_gb"]
        busy_gb, limit_gb = LIMITS["disk_free_gb"]
        if free < busy_gb:
            msg = f"{free:g} GB free (BUSY below {busy_gb} GB, LIMIT below {limit_gb} GB)"
            found["disk"] = ("critical" if free < limit_gb else "warning", "Disk space low", msg,
                             ACTIONS["disk"])  # fmt: skip
        return self._reconcile(now, found)

    def _swap_growth(self, swap: int, avail: int) -> int:
        """Swap above the calm baseline. The baseline is the lowest swap seen while MemAvailable is
        calm; it only moves down until 20 consecutive calm evaluations (10 minutes) have passed, then it
        follows the current swap for as long as the calm lasts (the legacy guard's rule)."""
        if self._swap_base is None:
            self._swap_base = swap
        if avail >= CALM_AVAIL_MB:
            self._calm += 1
            if self._calm >= CALM_RESET_EVALS:
                self._swap_base = swap
            else:
                self._swap_base = min(self._swap_base, swap)
        else:
            self._calm = 0
        return swap - self._swap_base

    def _reconcile(self, now: float, found: dict) -> list[dict]:
        out = []
        for key, (level, title, message, actions) in found.items():
            rec = self.active.get(key)
            if rec is None:
                rec = self.active[key] = {"key": key, "since": iso(now), "sent": False}
            rec.update(level=level, title=title, message=message, clear=0)
            # Within the dedupe window only a level above the highest one already sent gets through.
            due = now - self._last_sent.get(key, -1e18) >= self.dedupe_s
            if due or RANK[level] > self._sent_level.get(key, -1):
                if due:
                    self._sent_level.pop(key, None)
                rec["sent"], self._last_sent[key] = True, now
                self._sent_level[key] = max(RANK[level], self._sent_level.get(key, -1))
                ev = {
                    "type": "alert",
                    "key": key,
                    "level": level,
                    "title": title,
                    "message": message,
                    **self._ctx_fields(),
                    "actions": actions,
                    "at": iso(now),
                }
                out.append(self._record(ev))
        for key in [k for k in self.active if k not in found]:
            rec = self.active[key]
            rec["clear"] += 1
            if rec["clear"] < CLEAR_EVALS:
                continue  # may be a blip: still active, nothing sent
            del self.active[key]
            if rec["sent"]:  # the phone was told: tell it again at once if the condition returns
                self._last_sent.pop(key, None)
                self._sent_level.pop(key, None)
                out.append(self._record({"type": "resolved", "key": key, "title": rec["title"],
                                         "at": iso(now)}))  # fmt: skip
        return out

    def _ctx_fields(self) -> dict:
        c = self._ctx
        return {"top3": c["top3"], "avail_mb": c["avail_mb"], "swap_mb": c["swap_mb"],
                "profile": c["profile"], "ceiling": c["ceiling"]}  # fmt: skip

    def _record(self, ev: dict) -> dict:
        self.events.append(ev)
        return ev

    # ---- events from elsewhere -------------------------------------------------------------------

    def emit_event(self, kind: str, payload: dict | None = None) -> dict:
        """The memory guard's hook (`pause`, `abort`, `low_memory`, `resume`, `reap`); `payload` may carry
        `title`, `message`, `actions`, `top3`, `avail_mb`, `swap_mb`. Not deduped: the guard limits itself."""
        if kind not in HOOK_KINDS:
            raise ValueError(f"unknown event kind {kind!r} (one of {', '.join(HOOK_KINDS)})")
        payload = payload or {}
        level, title, actions = HOOK_KINDS[kind]
        ctx = self._ctx_fields()
        for k in ctx:
            if k in payload:
                ctx[k] = payload[k]
        ev = {"type": "alert", "key": kind, "level": level, "title": payload.get("title", title),
              "message": payload.get("message", title), **ctx,
              "actions": payload.get("actions", actions), "at": iso(self.clock())}  # fmt: skip
        ev.update({k: payload[k] for k in ("scope", "memory_peak_mb") if k in payload})  # BACKSTOP
        return self._record(ev)

    def owner_change(self, who: dict, what: str, reason: str | None) -> dict:
        """An owner-level change made on the host (not over REST), for the phone."""
        who = {k: who.get(k) for k in ("pid", "cmdline", "cgroup")}
        ev = {"type": "info", "key": "owner_change", "level": "info", "title": "Owner change on the host",
              "message": f"{what} by pid {who['pid']} ({who['cmdline']}): {reason}",
              "who": who, "what": what, "reason": reason, "at": iso(self.clock())}  # fmt: skip
        return self._record(ev)

    def summary(self) -> dict:
        keys = ("key", "level", "since", "title", "message")
        return {"active": [{k: r[k] for k in keys} for r in self.active.values()],
                "events": list(self.events)}  # fmt: skip
