"""Config flow and options flow."""

from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.loadgov.const import (
    CONF_NOTIFY,
    CONF_PRESENCE_ZONE,
    CONF_QUIET,
    CONF_REMOTE_CPU,
    CONF_REMOTE_CPU_MAX,
    CONF_REMOTE_LOAD,
    CONF_REMOTE_LOAD_MAX,
    CONF_TRIGGER_PRESENCE,
    CONF_TRIGGER_REMOTE,
    CONF_TV_PLAYERS,
    DOMAIN,
    OPTION_DEFAULTS,
)

from .conftest import BASE, HOST, PORT, TOKEN, mock_status

USER = {"host": HOST, "port": PORT, "token": TOKEN}


async def _start(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    return result


async def test_user_flow_success(hass, aioclient_mock: AiohttpClientMocker, status):
    mock_status(aioclient_mock, status)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == USER
    assert result["result"].unique_id == f"{HOST}:{PORT}"
    # validated through GET /v1/status with the bearer token
    method, url, _, headers = aioclient_mock.mock_calls[0]
    assert (method, str(url)) == ("GET", f"{BASE}/v1/status")
    assert headers["Authorization"] == f"Bearer {TOKEN}"


async def test_user_flow_invalid_auth(hass, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE}/v1/status", status=401, json={"ok": False, "error": "unauthorized"})
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE}/v1/status", exc=aiohttp.ClientConnectionError("down"))
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_timeout_is_cannot_connect(hass, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(f"{BASE}/v1/status", exc=TimeoutError())
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_already_configured(hass, aioclient_mock, mock_entry: MockConfigEntry, status):
    mock_entry.add_to_hass(hass)
    mock_status(aioclient_mock, status)
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(hass, aioclient_mock, mock_entry: MockConfigEntry, status):
    mock_entry.add_to_hass(hass)
    mock_status(aioclient_mock, status)
    result = await mock_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "n" * 43})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_entry.data["token"] == "n" * 43


async def test_options_flow_saves_selectors(hass, aioclient_mock, mock_entry: MockConfigEntry, status):
    mock_status(aioclient_mock, status)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    chosen = {
        CONF_PRESENCE_ZONE: "zone.work",
        CONF_TV_PLAYERS: ["media_player.a", "media_player.b"],
        CONF_QUIET: "switch.quiet_hours",
        CONF_REMOTE_LOAD: "sensor.l",
        CONF_REMOTE_CPU: "sensor.c",
        CONF_NOTIFY: ["notify.mobile_app_phone"],
        CONF_TRIGGER_PRESENCE: True,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], chosen)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options[CONF_PRESENCE_ZONE] == "zone.work"
    assert mock_entry.options[CONF_TV_PLAYERS] == ["media_player.a", "media_player.b"]
    assert mock_entry.options[CONF_NOTIFY] == ["notify.mobile_app_phone"]
    assert mock_entry.options[CONF_TRIGGER_PRESENCE] is True
    assert mock_entry.options[CONF_TRIGGER_REMOTE] is False  # each trigger has its own switch, off by default


async def test_options_flow_has_no_site_defaults_except_the_home_zone(
    hass, aioclient_mock, mock_entry, status
):
    mock_status(aioclient_mock, status)
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    suggested = {
        str(k): k.description["suggested_value"]
        for k in result["data_schema"].schema
        if k.description and "suggested_value" in k.description
    }
    assert suggested[CONF_PRESENCE_ZONE] == "zone.home"
    assert suggested[CONF_TV_PLAYERS] == []
    for key in (CONF_QUIET, CONF_REMOTE_LOAD, CONF_REMOTE_CPU, CONF_REMOTE_LOAD_MAX, CONF_REMOTE_CPU_MAX):
        assert suggested[key] is None and OPTION_DEFAULTS[key] is None, key


async def test_user_flow_has_no_default_host(hass):
    result = await _start(hass)
    host_key = next(k for k in result["data_schema"].schema if str(k) == "host")
    assert host_key.default is vol.UNDEFINED


async def test_user_flow_source_not_allowed_is_not_invalid_auth(hass, aioclient_mock: AiohttpClientMocker):
    aioclient_mock.get(
        f"{BASE}/v1/status",
        status=403,
        json={"ok": False, "error": "forbidden", "message": "source address is not allowed"},
    )
    result = await hass.config_entries.flow.async_configure((await _start(hass))["flow_id"], USER)
    assert result["errors"] == {"base": "source_not_allowed"}
