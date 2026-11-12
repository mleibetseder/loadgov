"""Config flow (host, port, token) and options flow (trigger entities)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import LoadgovApi, LoadgovAuthError, LoadgovError, LoadgovSourceError
from .const import (
    CONF_NOTIFY,
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
    DEFAULT_PORT,
    DOMAIN,
    NAME,
    OPTION_DEFAULTS,
)
from .coordinator import LoadgovConfigEntry


class LoadgovConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, host: str, port: int, token: str) -> str | None:
        """None when GET /v1/status works, else the error key."""
        api = LoadgovApi(async_get_clientsession(self.hass), host, port, token)
        try:
            await api.status()
        except LoadgovSourceError:
            return "source_not_allowed"
        except LoadgovAuthError:
            return "invalid_auth"
        except LoadgovError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            # The host:port pair is the only identity the daemon offers (it has no device id).
            await self.async_set_unique_id(f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            error = await self._validate(user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_TOKEN])
            if error is None:
                return self.async_create_entry(title=NAME, data=user_input)
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, vol.UNDEFINED)): str,
                vol.Required(CONF_PORT, default=(user_input or {}).get(CONF_PORT, DEFAULT_PORT)): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            error = await self._validate(entry.data[CONF_HOST], entry.data[CONF_PORT], user_input[CONF_TOKEN])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )
            errors["base"] = error
        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: LoadgovConfigEntry) -> LoadgovOptionsFlow:
        return LoadgovOptionsFlow()


class LoadgovOptionsFlow(OptionsFlowWithReload):
    """Choose the entities the smart triggers watch and switch each trigger on or off.

    The choices live in `entry.options`; triggers.py reads them (saving reloads the entry)."""

    def _notify_options(self) -> list[str]:
        names = self.hass.services.async_services_for_domain("notify")
        return sorted(f"notify.{n}" for n in names if n != "send_message")

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = {**OPTION_DEFAULTS, **self.config_entry.options}

        def ent(domain: str | list[str], multiple: bool = False) -> selector.EntitySelector:
            return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=multiple))

        def key(name: str, optional: bool = True) -> vol.Marker:
            marker = vol.Optional if optional else vol.Required
            return marker(name, description={"suggested_value": opts[name]})

        notify_choices = sorted({*self._notify_options(), *opts[CONF_NOTIFY]})
        schema = vol.Schema(
            {
                key(CONF_PRESENCE_ZONE): ent("zone"),
                key(CONF_TV_PLAYERS): ent("media_player", multiple=True),
                key(CONF_QUIET): ent(["input_boolean", "switch"]),
                key(CONF_REMOTE_LOAD): ent("sensor"),
                key(CONF_REMOTE_CPU): ent("sensor"),
                key(CONF_REMOTE_LOAD_MAX): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1000, step=0.1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                key(CONF_REMOTE_CPU_MAX): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100, step=0.1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                key(CONF_NOTIFY): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=notify_choices, multiple=True, custom_value=True)
                ),
                vol.Required(CONF_TRIGGER_PRESENCE, default=opts[CONF_TRIGGER_PRESENCE]): bool,
                vol.Required(CONF_TRIGGER_TV, default=opts[CONF_TRIGGER_TV]): bool,
                vol.Required(CONF_TRIGGER_QUIET, default=opts[CONF_TRIGGER_QUIET]): bool,
                vol.Required(CONF_TRIGGER_REMOTE, default=opts[CONF_TRIGGER_REMOTE]): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
