"""loadgov.set_profile / set_ceiling / clear_ceiling."""

from __future__ import annotations

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import BASE, posts
from .test_entities import setup


async def test_set_profile_with_until(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True, "accepted": True})
    await hass.services.async_call(
        "loadgov", "set_profile",
        {"profile": "silent", "until": "07:00 Europe/Berlin", "reason": "movie night"}, blocking=True,
    )  # fmt: skip
    assert posts(aioclient_mock) == [
        ("/v1/set", {"reason": "movie night", "profile": "silent", "until": "07:00 Europe/Berlin"})
    ]


async def test_set_ceiling_and_clear(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/ceiling", json={"ok": True, "accepted": True})
    await hass.services.async_call(
        "loadgov", "set_ceiling", {"profile": "silent", "ttl": "30m"}, blocking=True
    )
    await hass.services.async_call("loadgov", "clear_ceiling", {}, blocking=True)
    (p1, b1), (p2, b2) = posts(aioclient_mock)
    assert (p1, b1["profile"], b1["ttl"]) == ("/v1/ceiling", "silent", "30m")
    assert (p2, b2["clear"]) == ("/v1/ceiling", True)
    assert b1["reason"] and b2["reason"]


async def test_service_rejects_unknown_profile_and_ttl_with_until(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    with pytest.raises(Exception, match="profile|valid"):
        await hass.services.async_call("loadgov", "set_profile", {"profile": "turbo"}, blocking=True)
    with pytest.raises(Exception, match="until|ttl|two"):
        await hass.services.async_call(
            "loadgov", "set_profile", {"profile": "silent", "ttl": "1h", "until": "07:00"}, blocking=True
        )
    assert posts(aioclient_mock) == []


async def test_service_refusal_raises_the_daemons_reason(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/set", status=409, json={"refused": True, "reason": "TV playing"})
    with pytest.raises(HomeAssistantError, match="TV playing"):
        await hass.services.async_call("loadgov", "set_profile", {"profile": "boost"}, blocking=True)


async def test_service_without_loaded_entry(hass):
    from custom_components.loadgov import async_setup

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("loadgov", "clear_ceiling", {}, blocking=True)
