"""Home Assistant integration for loadgovd, the host-wide load governor of the build host."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import LoadgovApi
from .const import DOMAIN, PROFILE_LABELS
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .triggers import Triggers

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

ATTR_ENTRY = "config_entry_id"
_BASE = {vol.Optional(ATTR_ENTRY): cv.string, vol.Optional("reason"): cv.string}
SET_PROFILE_SCHEMA = vol.Schema(
    {
        **_BASE,
        vol.Required("profile"): vol.In(list(PROFILE_LABELS)),
        vol.Exclusive("until", "when"): cv.string,
        vol.Exclusive("ttl", "when"): cv.string,
    }
)
SET_CEILING_SCHEMA = SET_PROFILE_SCHEMA
CLEAR_CEILING_SCHEMA = vol.Schema(_BASE)


def _coordinator(hass: HomeAssistant, call: ServiceCall) -> LoadgovCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if wanted := call.data.get(ATTR_ENTRY):
        entries = [e for e in entries if e.entry_id == wanted]
    if not entries:
        raise ServiceValidationError("no loaded loadgov config entry (is the daemon set up?)")
    return entries[0].runtime_data


def _body(call: ServiceCall) -> dict:
    return {k: call.data[k] for k in ("profile", "until", "ttl", "reason") if k in call.data}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def set_profile(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_post("/v1/set", _body(call))

    async def set_ceiling(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_post("/v1/ceiling", _body(call))

    async def clear_ceiling(call: ServiceCall) -> None:
        await _coordinator(hass, call).async_post("/v1/ceiling", {**_body(call), "clear": True})

    hass.services.async_register(DOMAIN, "set_profile", set_profile, SET_PROFILE_SCHEMA)
    hass.services.async_register(DOMAIN, "set_ceiling", set_ceiling, SET_CEILING_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_ceiling", clear_ceiling, CLEAR_CEILING_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LoadgovConfigEntry) -> bool:
    api = LoadgovApi(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN]
    )
    coordinator = LoadgovCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.triggers = Triggers(hass, entry, coordinator)
    entry.async_on_unload(coordinator.triggers.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.triggers.async_start()  # after the first refresh: every enabled trigger evaluates once
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LoadgovConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
