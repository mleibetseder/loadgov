"""Smart triggers: presence, TV quiet, quiet hours and remote host load, run inside the integration.

Entity ids come only from the options flow; each trigger runs only when its toggle is on. Every
restriction a trigger sends carries a TTL (or an `until`) and is re-armed while its condition holds, so a
Home Assistant restart in the middle of a condition heals itself: the daemon expires what is not
re-armed. The trigger memory (what a trigger set) is in RAM only; after a restart the triggers evaluate
once and never clear what they cannot prove they set.

Ownership comes from the daemon, not from memory: every reason a trigger sends starts with a fixed tag
(`HA trigger:tv:` and so on) and the status carries the reason of the active ceiling and profile
(`limits.ceiling_info`, `limits.profile_info`). A trigger only clears or replaces what carries its own tag, so
a restriction a human set is never undone, and after a Home Assistant restart a trigger still
recognises its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .api import LoadgovError
from .const import (
    CONF_PRESENCE_ZONE,
    CONF_QUIET,
    CONF_REMOTE_CPU,
    CONF_REMOTE_CPU_MAX,
    CONF_REMOTE_LOAD,
    CONF_REMOTE_LOAD_MAX,
    CONF_TRIGGER_PRESENCE,
    CONF_TRIGGER_QUIET,
    CONF_TRIGGER_REMOTE,
    CONF_TRIGGER_TV,
    CONF_TV_PLAYERS,
    OPTION_DEFAULTS,
)
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import dig

_LOGGER = logging.getLogger(__name__)

PRESENCE, TV, QUIET, REMOTE = "presence", "tv_quiet", "quiet_hours", "remote_load"
TRIGGERS = (PRESENCE, TV, QUIET, REMOTE)
TOGGLES = {
    PRESENCE: CONF_TRIGGER_PRESENCE,
    TV: CONF_TRIGGER_TV,
    QUIET: CONF_TRIGGER_QUIET,
    REMOTE: CONF_TRIGGER_REMOTE,
}

PRESENCE_EMPTY_S = 10 * 60
PRESENCE_TTL = "30m"
PRESENCE_REARM = timedelta(minutes=15)
TV_TTL = "15m"
TV_REARM = timedelta(minutes=10)
TV_STOPPED_S = 5 * 60
REMOTE_HOLD_S = 10 * 60
REMOTE_TTL = "15m"
REMOTE_REARM = timedelta(minutes=10)
QUIET_UNTIL = "07:00"
REMOTE_TARGETS = {"max_implementers": 1, "max_cores": 2.5}
CEILING = "silent"
TAG = {
    PRESENCE: "HA trigger:presence:",
    TV: "HA trigger:tv:",
    QUIET: "HA trigger:quiet_hours:",
    REMOTE: "HA trigger:remote_load:",
}


def _tagged(info: dict[str, Any] | None, trigger: str) -> bool:
    return isinstance(info, dict) and str(info.get("reason") or "").startswith(TAG[trigger])


def _number(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None


def _unchanged(event: Event[EventStateChangedData]) -> bool:
    """Attribute-only updates do not matter to any trigger."""
    old, new = event.data["old_state"], event.data["new_state"]
    return old is not None and new is not None and old.state == new.state


class Triggers:
    """One per config entry: `async_start` after the first coordinator refresh, `async_stop` on unload."""

    def __init__(
        self, hass: HomeAssistant, entry: LoadgovConfigEntry, coordinator: LoadgovCoordinator
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._opts: dict[str, Any] = {**OPTION_DEFAULTS, **entry.options}
        self._lock = asyncio.Lock()
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._listeners: list[Callable[[], None]] = []
        self._stopped = False
        self._info: dict[str, dict[str, Any]] = {
            t: {
                "enabled": bool(self._opts[TOGGLES[t]]),
                "active": False,
                "last_action": None,
                "last_error": None,
            }
            for t in TRIGGERS
        }
        self._presence_owned = False  # RAM only: we sent `boost` and it was accepted

    # --- public ---

    def info(self) -> dict[str, dict[str, Any]]:
        return {t: dict(v) for t, v in self._info.items()}

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            if update in self._listeners:
                self._listeners.remove(update)

        return remove

    @callback
    def async_start(self) -> None:
        """Subscribe the enabled triggers and evaluate each once (self-heal after a restart or reload)."""
        o = self._opts
        watch: dict[str, tuple[list[str], Callable[[Event[EventStateChangedData]], None]]] = {
            PRESENCE: ([o[CONF_PRESENCE_ZONE]], self._on_presence),
            TV: (list(o[CONF_TV_PLAYERS] or []), self._on_tv),
            QUIET: ([o[CONF_QUIET]], self._on_quiet),
            REMOTE: ([o[CONF_REMOTE_LOAD], o[CONF_REMOTE_CPU]], self._on_remote),
        }
        first = {
            PRESENCE: self._presence_eval,
            TV: lambda: self._tv_eval(startup=True),
            QUIET: self._quiet_eval,
            REMOTE: lambda: self._remote_eval(startup=True),
        }
        for name in TRIGGERS:
            if not self._info[name]["enabled"]:
                continue
            if entities := [e for e in watch[name][0] if e]:
                self._unsubs.append(async_track_state_change_event(self.hass, entities, watch[name][1]))
            self._spawn(first[name](), f"loadgov trigger {name} start")

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for key in list(self._timers):
            self._cancel(key)
        self._listeners.clear()

    # --- plumbing ---

    def _spawn(self, coro: Any, name: str) -> None:
        if self._stopped:
            coro.close()
            return
        self._entry.async_create_task(self.hass, self._guard(coro), name)

    async def _guard(self, coro: Any) -> None:
        """Serialise the trigger work; nothing escapes into the event loop."""
        try:
            async with self._lock:
                if self._stopped:
                    coro.close()
                    return
                await coro
        except Exception:
            _LOGGER.exception("loadgov trigger failed unexpectedly")

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    def _cancel(self, key: str) -> None:
        if unsub := self._timers.pop(key, None):
            unsub()

    def _later(self, key: str, seconds: float, make: Callable[[], Any]) -> None:
        self._cancel(key)
        if self._stopped:
            return

        @callback
        def fire(_now: datetime) -> None:
            self._timers.pop(key, None)
            self._spawn(make(), f"loadgov trigger {key}")

        self._timers[key] = async_call_later(self.hass, seconds, fire)

    def _every(self, key: str, interval: timedelta, make: Callable[[], Any]) -> None:
        self._cancel(key)
        if self._stopped:
            return

        @callback
        def fire(_now: datetime) -> None:
            self._spawn(make(), f"loadgov trigger {key}")

        self._timers[key] = async_track_time_interval(self.hass, fire, interval)

    def _set(self, trigger: str, **kw: Any) -> None:
        self._info[trigger].update(kw)
        self._notify()

    def _ok(self, trigger: str, action: str) -> None:
        stamp = dt_util.now().isoformat(timespec="seconds")
        self._set(trigger, last_action=f"{action} @ {stamp}", last_error=None)

    def _fail(self, trigger: str, what: str, err: Exception) -> None:
        """Log once per change of the error text; the next re-arm retries."""
        text = f"{what}: {err}"
        if self._info[trigger]["last_error"] != text:
            _LOGGER.warning("loadgov %s trigger: %s", trigger, text)
        self._set(trigger, last_error=text)

    async def _post(self, trigger: str, what: str, path: str, body: dict[str, Any]) -> bool:
        try:
            await self._coordinator.async_post(path, body)
        except HomeAssistantError as err:  # refused (409), 503, connection, auth: all land here
            self._fail(trigger, what, err)
            return False
        self._ok(trigger, what)
        return True

    async def _status(self, trigger: str) -> dict[str, Any] | None:
        try:
            return await self._coordinator.api.status()
        except LoadgovError as err:
            self._fail(trigger, "status unavailable", err)
            return None

    # --- ceilings shared by TV and quiet hours ---

    async def _ceiling_arm(self, trigger: str, reason: str, handover: bool = False, **when: str) -> None:
        status = await self._status(trigger)
        if status is None:
            return
        cur = dig(status, "limits.ceiling_info")
        if cur is not None and not _tagged(cur, TV) and not _tagged(cur, QUIET):
            # A human ceiling (or a foreign one) already applies: never overwrite it.
            self._set(trigger, last_action=f"human ceiling active ({cur.get('reason')}), left alone")
            return
        if trigger == TV and _tagged(cur, QUIET) and not handover:
            self._ok(trigger, "covered by the quiet-hours ceiling")  # the longer one stays
            return
        body = {"profile": CEILING, "reason": f"{TAG[trigger]} {reason}", **when}
        await self._post(trigger, f"ceiling {CEILING} ({reason})", "/v1/ceiling", body)

    async def _ceiling_release(self, trigger: str, reason: str) -> None:
        status = await self._status(trigger)
        if status is None:
            return
        cur = dig(status, "limits.ceiling_info")
        if cur is None:
            self._ok(trigger, "ceiling already gone, nothing to clear")
            return
        if not _tagged(cur, trigger):
            self._set(
                trigger, last_action=f"ceiling not set by this trigger ({cur.get('reason')}), left alone"
            )
            return
        if trigger == QUIET and self._info[TV]["enabled"] and self._tv_playing():
            await self._ceiling_arm(TV, "TV playing", handover=True, ttl=TV_TTL)  # hand over to a TTL ceiling
            return
        body = {"clear": True, "reason": f"{TAG[trigger]} {reason}"}
        await self._post(trigger, f"ceiling clear ({reason})", "/v1/ceiling", body)

    # --- 1. presence ---

    def _zone_count(self) -> int | None:
        number = _number(self.hass.states.get(self._opts[CONF_PRESENCE_ZONE]))
        return None if number is None else int(number)

    @callback
    def _on_presence(self, event: Event[EventStateChangedData]) -> None:
        if _unchanged(event):
            return
        was_empty = _number(event.data["old_state"]) == 0
        self._spawn(self._presence_eval(was_empty), "loadgov trigger presence")

    async def _presence_eval(self, was_empty: bool = False) -> None:
        count = self._zone_count()
        if count is None:
            return
        if count > 0:
            self._cancel("presence_debounce")
            self._cancel("presence_rearm")
            self._set(PRESENCE, active=False)
            if was_empty:
                await self._presence_back()
            return
        if "presence_debounce" not in self._timers and "presence_rearm" not in self._timers:
            self._later("presence_debounce", PRESENCE_EMPTY_S, self._presence_empty_long_enough)

    async def _presence_empty_long_enough(self) -> None:
        if self._zone_count() != 0:
            return
        self._every("presence_rearm", PRESENCE_REARM, self._presence_arm)
        await self._presence_arm()

    async def _presence_arm(self) -> None:
        if self._zone_count() != 0:
            return
        status = await self._status(PRESENCE)
        if status is None:
            return
        profile = dig(status, "limits.profile")
        ours = profile == "boost" and _tagged(dig(status, "limits.profile_info"), PRESENCE)
        if not ours and self._presence_owned:
            # Someone changed the profile after we set boost: stop re-arming until the condition changes.
            self._cancel("presence_rearm")
            self._presence_owned = False
            self._set(PRESENCE, active=False, last_action="stopped: profile changed by someone else")
            return
        pinfo = dig(status, "limits.profile_info")
        default_balanced = profile == "balanced" and pinfo is None  # never set by anybody
        if not ours and not default_balanced:
            self._set(PRESENCE, last_action=f"skipped, profile {profile} was chosen by someone else")
            return
        body = {"profile": "boost", "ttl": PRESENCE_TTL, "reason": f"{TAG[PRESENCE]} nobody home"}
        self._set(PRESENCE, active=True)
        self._presence_owned = await self._post(PRESENCE, "profile boost", "/v1/set", body)

    async def _presence_back(self) -> None:
        self._presence_owned = False
        status = await self._status(PRESENCE)
        if status is None:
            return
        mine = _tagged(dig(status, "limits.profile_info"), PRESENCE)
        if dig(status, "limits.profile") != "boost" or not mine:
            self._set(PRESENCE, last_action="someone home, profile not set by this trigger, left alone")
            return
        body = {"profile": "balanced", "reason": f"{TAG[PRESENCE]} someone home"}
        await self._post(PRESENCE, "profile balanced", "/v1/set", body)

    # --- 2. TV quiet ---

    def _tv_playing(self) -> bool:
        return any(
            (s := self.hass.states.get(e)) is not None and s.state == "playing"
            for e in self._opts[CONF_TV_PLAYERS] or []
        )

    @callback
    def _on_tv(self, event: Event[EventStateChangedData]) -> None:
        if not _unchanged(event):
            self._spawn(self._tv_eval(), "loadgov trigger tv")

    async def _tv_eval(self, startup: bool = False) -> None:
        if not self._tv_playing() and startup and not self._info[TV]["active"]:
            # After a restart a ceiling of ours may still be in force: clear it after the usual 5 minutes.
            status = await self._status(TV)
            if status is not None and _tagged(dig(status, "limits.ceiling_info"), TV):
                self._set(TV, active=True)
        if self._tv_playing():
            self._cancel("tv_stopped")
            if not self._info[TV]["active"] or "tv_rearm" not in self._timers:
                self._set(TV, active=True)
                self._every("tv_rearm", TV_REARM, self._tv_arm)
                await self._tv_arm()
        elif self._info[TV]["active"] and "tv_stopped" not in self._timers:
            self._later("tv_stopped", TV_STOPPED_S, self._tv_stopped_long_enough)

    async def _tv_arm(self) -> None:
        if self._tv_playing():
            await self._ceiling_arm(TV, "TV playing", ttl=TV_TTL)

    async def _tv_stopped_long_enough(self) -> None:
        if self._tv_playing():
            return
        self._cancel("tv_rearm")
        self._set(TV, active=False)
        await self._ceiling_release(TV, "TV stopped")

    # --- 3. quiet hours ---

    def _quiet_state(self) -> State | None:
        entity = self._opts[CONF_QUIET]
        return self.hass.states.get(entity) if entity else None

    def _quiet_on(self) -> bool:
        state = self._quiet_state()
        return state is not None and state.state == "on"

    @callback
    def _on_quiet(self, event: Event[EventStateChangedData]) -> None:
        if not _unchanged(event):
            self._spawn(self._quiet_eval(), "loadgov trigger quiet_hours")

    async def _quiet_eval(self) -> None:
        if self._quiet_on():
            self._set(QUIET, active=True)
            until = f"{QUIET_UNTIL} {self.hass.config.time_zone}"
            await self._ceiling_arm(QUIET, "quiet hours", until=until)
        elif (state := self._quiet_state()) and state.state == "off":
            # also at start: a quiet-hours ceiling that outlived the switch is cleared
            self._set(QUIET, active=False)
            await self._ceiling_release(QUIET, "quiet hours over")

    # --- 4. remote host load ---

    def _remote_value(self, sensor_key: str) -> float | None:
        entity = self._opts[sensor_key]
        return _number(self.hass.states.get(entity)) if entity else None

    def _remote_high(self) -> bool:
        """Load or CPU of the watched remote host is above its threshold; an unset threshold never trips."""
        load, cpu = self._remote_value(CONF_REMOTE_LOAD), self._remote_value(CONF_REMOTE_CPU)
        load_max, cpu_max = self._opts[CONF_REMOTE_LOAD_MAX], self._opts[CONF_REMOTE_CPU_MAX]
        return (load is not None and load_max is not None and load > float(load_max)) or (
            cpu is not None and cpu_max is not None and cpu > float(cpu_max)
        )

    @callback
    def _on_remote(self, event: Event[EventStateChangedData]) -> None:
        self._spawn(self._remote_eval(), "loadgov trigger remote_load")

    async def _remote_eval(self, startup: bool = False) -> None:
        if self._remote_high():
            if startup:
                status = await self._status(REMOTE)
                overrides = dig(status, "limits.overrides", []) if status else []
                if any(_tagged(o, REMOTE) for o in overrides or []):
                    await self._remote_held()  # our limits are still in force: re-arm now, no TTL gap
                    return
            if "remote_debounce" not in self._timers and "remote_rearm" not in self._timers:
                self._later("remote_debounce", REMOTE_HOLD_S, self._remote_held)
            return
        self._cancel("remote_debounce")
        self._cancel("remote_rearm")
        self._set(REMOTE, active=False)  # nothing to send: the TTL restores the limits

    async def _remote_held(self) -> None:
        if not self._remote_high():
            return
        self._every("remote_rearm", REMOTE_REARM, self._remote_arm)
        await self._remote_arm()

    async def _remote_arm(self) -> None:
        """Lower only what nobody set by hand: a param with an override that does not carry the remote tag
        (a slider, `loadctl set --owner`) is left alone, and nothing is sent that would loosen a value."""
        if not self._remote_high():
            return
        self._set(REMOTE, active=True)
        status = await self._status(REMOTE)
        if status is None:
            return
        overrides = dig(status, "limits.overrides", []) or []
        effective = dig(status, "limits.effective", {}) or {}
        body: dict[str, Any] = {}
        for param, target in REMOTE_TARGETS.items():
            mine = any(o.get("param") == param and _tagged(o, REMOTE) for o in overrides)
            by_hand = any(
                o.get("param") == param and o.get("principal") != "agent" and not _tagged(o, REMOTE)
                for o in overrides
            )  # an agent raise ranks below us: this trigger replaces it
            value = effective.get(param)
            if by_hand or not isinstance(value, int | float):
                continue
            if mine or value > target:  # re-arm our own, or lower a value that is above the target
                body[param] = target
        if not body:
            self._ok(REMOTE, "nothing to lower (set by hand or already at or below the target)")
            return
        body |= {"ttl": REMOTE_TTL, "reason": f"{TAG[REMOTE]} load or CPU high"}
        what = "limits " + " ".join(f"{k}={v}" for k, v in REMOTE_TARGETS.items() if k in body)
        await self._post(REMOTE, what, "/v1/set", body)
