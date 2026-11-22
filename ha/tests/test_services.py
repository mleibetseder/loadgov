"""loadgov.set_profile / set_ceiling / clear_ceiling."""

from __future__ import annotations

import pytest
import voluptuous as vol
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


async def test_kill_stray_posts_the_body(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(
        f"{BASE}/v1/stray/kill", json={"ok": True, "killed": True, "signal": "SIGTERM", "message": "ok"}
    )
    await hass.services.async_call(
        "loadgov", "kill_stray", {"pid": 4242, "start_time": 99, "reason": "runaway"}, blocking=True
    )
    assert posts(aioclient_mock) == [("/v1/stray/kill", {"reason": "runaway", "pid": 4242, "start_time": 99})]


async def test_kill_stray_refusal_raises_the_daemons_message(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(
        f"{BASE}/v1/stray/kill",
        status=409,
        json={"ok": False, "refused": True, "message": "not a current stray"},
    )
    with pytest.raises(HomeAssistantError, match="not a current stray"):
        await hass.services.async_call(
            "loadgov", "kill_stray", {"pid": 4242, "start_time": 99}, blocking=True
        )


async def test_service_without_loaded_entry(hass):
    from custom_components.loadgov import async_setup

    await async_setup(hass, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("loadgov", "clear_ceiling", {}, blocking=True)


async def test_image_run_now_posts_the_body(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/image/now", json={"ok": True, "id": 3, "now": True})
    await hass.services.async_call(
        "loadgov", "image_run_now", {"id": 3, "reason": "release needed early"}, blocking=True
    )
    assert posts(aioclient_mock) == [("/v1/image/now", {"reason": "release needed early", "id": 3})]


@pytest.mark.parametrize(
    "data", [{"id": 3}, {"id": 3, "reason": ""}, {"reason": "r"}, {"id": 0, "reason": "r"}]
)
async def test_image_run_now_requires_id_and_reason(hass, aioclient_mock, mock_entry, status, data):
    await setup(hass, mock_entry, aioclient_mock, status)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call("loadgov", "image_run_now", data, blocking=True)
    assert posts(aioclient_mock) == []


@pytest.mark.parametrize(
    ("code", "reply"),
    [
        (409, {"ok": False, "refused": True, "message": "job 3 is running"}),
        (404, {"ok": False, "error": "not_found", "message": "no image job 3"}),
    ],
)
async def test_image_run_now_refusal_raises_the_daemons_message(
    hass, aioclient_mock, mock_entry, status, code, reply
):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/image/now", status=code, json=reply)
    with pytest.raises(HomeAssistantError, match=reply["message"]):
        await hass.services.async_call("loadgov", "image_run_now", {"id": 3, "reason": "r"}, blocking=True)
