"""Run-time image-train settings (window, timezone, beta interval, reset, source)."""

from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError

from .conftest import BASE, mock_status, posts
from .test_entities import setup

WINDOW = "text.host_image_window"
ZONE = "text.host_image_timezone"
BETA = "number.host_image_beta_interval"
RESET = "button.host_image_settings_reset"
SOURCE = "sensor.host_image_settings_source"
ALL = (WINDOW, ZONE, BETA, RESET, SOURCE)


def _with_image(status, **kw):
    image = {
        "window": "22:00-04:00",
        "timezone": "America/New_York",
        "beta_interval_h": 24,
        "source": "override",
        "queued": 0,
        "running": 0,
        "queue": [],
    }
    image.update(kw)
    status["image"] = image
    return status


async def test_entities_read_the_status(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    s = hass.states.get
    assert s(WINDOW).state == "22:00-04:00"
    assert s(ZONE).state == "America/New_York"
    assert float(s(BETA).state) == 24
    assert s(BETA).attributes["max"] == 336 and s(BETA).attributes["min"] == 0
    assert s(BETA).attributes["unit_of_measurement"] == "h"
    assert s(SOURCE).state == "override"
    assert s(RESET).state != STATE_UNAVAILABLE


async def test_source_config(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status, source="config"))
    assert hass.states.get(SOURCE).state == "config"


async def test_older_daemon_without_image_is_unavailable(hass, aioclient_mock, mock_entry, status):
    assert "image" not in status
    await setup(hass, mock_entry, aioclient_mock, status)
    for eid in ALL:
        assert hass.states.get(eid).state == STATE_UNAVAILABLE


async def test_older_daemon_without_source_degrades(hass, aioclient_mock, mock_entry, status):
    _with_image(status)
    del status["image"]["source"]
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get(SOURCE).state == STATE_UNKNOWN  # the train exists, the source is not reported
    for eid in (WINDOW, ZONE, BETA, RESET):  # the daemon would refuse the new fields: nothing to write to
        assert hass.states.get(eid).state == STATE_UNAVAILABLE
    await hass.services.async_call("button", "press", {"entity_id": RESET}, blocking=True)  # skipped by HA
    with pytest.raises(HomeAssistantError, match="no run-time image settings"):
        await mock_entry.runtime_data.async_set_image({"image_clear": True})  # the guard behind every write
    assert posts(aioclient_mock) == []


async def test_writes_send_the_right_payload(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True, "cleared": False})
    call = hass.services.async_call
    await call("text", "set_value", {"entity_id": WINDOW, "value": "23:30-05:15"}, blocking=True)
    await call("text", "set_value", {"entity_id": ZONE, "value": " Europe/Berlin "}, blocking=True)
    await call("number", "set_value", {"entity_id": BETA, "value": 0}, blocking=True)
    await call("number", "set_value", {"entity_id": BETA, "value": 336}, blocking=True)
    sent = posts(aioclient_mock)
    assert [p for p, _ in sent] == ["/v1/set"] * 4
    bodies = [b for _, b in sent]
    assert [{k: v for k, v in b.items() if k != "reason"} for b in bodies] == [
        {"image_window": "23:30-05:15"},
        {"image_timezone": "Europe/Berlin"},
        {"image_beta_interval_h": 0},
        {"image_beta_interval_h": 336},
    ]
    assert all(b["reason"] == "Home Assistant" for b in bodies)
    assert all(isinstance(b["image_beta_interval_h"], int) for b in bodies[2:])
    assert not any(k in b for b in bodies for k in ("max_cores", "profile", "owner"))  # never mixed with limits


async def test_write_refreshes_the_coordinator(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True})
    aioclient_mock.clear_requests()
    status2 = _with_image(dict(status), window="01:00-02:00", source="override")
    mock_status(aioclient_mock, status2)
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True})
    await hass.services.async_call(
        "text", "set_value", {"entity_id": WINDOW, "value": "01:00-02:00"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(WINDOW).state == "01:00-02:00"


@pytest.mark.parametrize("value", ["", "6:00-7:00", "24:00-05:00", "22:00-04:60", "22:00 - 04:00", "22:00-04:00x", "nonsense"])
async def test_malformed_window_is_rejected_before_sending(hass, aioclient_mock, mock_entry, status, value):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    with pytest.raises((HomeAssistantError, ValueError)):  # HA's own pattern check, or the entity's
        await hass.services.async_call("text", "set_value", {"entity_id": WINDOW, "value": value}, blocking=True)
    assert posts(aioclient_mock) == []


async def test_window_with_equal_start_and_end_is_rejected(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    with pytest.raises(HomeAssistantError, match="differ"):
        await hass.services.async_call(
            "text", "set_value", {"entity_id": WINDOW, "value": "06:00-06:00"}, blocking=True
        )
    assert posts(aioclient_mock) == []


async def test_beta_interval_out_of_range_is_not_sent(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    for value in (337, -1):
        with pytest.raises(Exception, match="range|336"):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": BETA, "value": value}, blocking=True
            )
    assert posts(aioclient_mock) == []


@pytest.mark.parametrize(
    ("entity", "domain", "service", "data", "http", "match"),
    [
        (WINDOW, "text", "set_value", {"value": "22:00-04:00"}, 400, "not the owner"),
        (ZONE, "text", "set_value", {"value": "Mars/Base"}, 400, "unknown timezone"),
        (BETA, "number", "set_value", {"value": 12}, 400, "interval"),
        (RESET, "button", "press", {}, 400, "not the owner"),
        (RESET, "button", "press", {}, 409, "refused"),  # 409 = a refused decision (rest.py status_for)
    ],
)
async def test_refused_write_raises_with_the_daemons_message(
    hass, aioclient_mock, mock_entry, status, entity, domain, service, data, http, match
):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    message = {
        "not the owner": "owner claim refused: not the owner",
        "unknown timezone": "image_timezone: unknown timezone 'Mars/Base'",
        "interval": "image_beta_interval_h must be an integer from 0 to 336",
        "refused": "refused",
    }[match]
    aioclient_mock.post(
        f"{BASE}/v1/set",
        status=http,
        json={"ok": False, "refused": True, "message": message}
        if http == 409
        else {"ok": False, "error": "owner_denied" if match == "not the owner" else "invalid_args", "message": message},
    )
    with pytest.raises(HomeAssistantError, match=match):
        await hass.services.async_call(domain, service, {"entity_id": entity, **data}, blocking=True)
    assert hass.states.get(WINDOW).state == "22:00-04:00"  # nothing crashed, the state is unchanged


async def test_reset_button_sends_image_clear(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, _with_image(status))
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True, "cleared": True})
    await hass.services.async_call("button", "press", {"entity_id": RESET}, blocking=True)
    ((path, body),) = posts(aioclient_mock)
    assert path == "/v1/set"
    assert body == {"reason": "Home Assistant", "image_clear": True}
