"""Setup, availability, entity states and writes."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.loadgov.const import SCAN_INTERVAL_S

from .conftest import BASE, mock_status, posts


async def setup(hass, entry, mocker, status):
    mock_status(mocker, status)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_states(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    s = hass.states.get
    assert s("select.host_load_profile").state == "Balanced (Default)"
    assert s("select.host_load_profile").attributes["options"] == [
        "Silent (Fan Off)", "Balanced (Default)", "Boost (Away)", "Drain (Hold New Work)",
    ]  # fmt: skip
    assert float(s("number.host_max_cores").state) == 4.0
    assert float(s("number.host_ram_floor_gb").attributes["max"]) == 6.0
    assert float(s("number.host_max_implementers").state) == 2
    assert s("switch.host_load_gate_hold").state == STATE_OFF
    assert s("sensor.host_busy_pct").state == "42.5"
    assert s("sensor.host_cap_cores_build_work").state == "4.0"
    assert s("sensor.host_cap_cores_mirror").state == "unknown"  # unlimited
    assert s("sensor.host_effective_profile").state == "Balanced (Default)"
    assert s("sensor.host_paused_workers").state == "0"
    assert s("binary_sensor.host_workers_paused").state == STATE_OFF
    assert s("binary_sensor.host_restriction_active").state == STATE_OFF
    assert s("binary_sensor.host_hold_active").state == STATE_OFF


async def test_state_follows_restriction_hold_and_paused(hass, aioclient_mock, mock_entry, status):
    status["limits"].update(restricted=True, ceiling="silent")
    status["slots"]["heavy"]["hold"] = "Home Assistant"
    status["stopped"] = [{"pid": 7, "comm": "rustc", "cgroup": "/build-work.slice"}]
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("binary_sensor.host_restriction_active").state == STATE_ON
    assert hass.states.get("binary_sensor.host_hold_active").state == STATE_ON
    assert hass.states.get("switch.host_load_gate_hold").state == STATE_ON
    assert hass.states.get("binary_sensor.host_workers_paused").state == STATE_ON
    assert hass.states.get("sensor.host_paused_workers").state == "1"
    assert hass.states.get("sensor.host_owner_ceiling").state == "Silent (Fan Off)"


async def test_coordinator_failure_makes_entities_unavailable(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_busy_pct").state == "42.5"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/v1/status", exc=aiohttp.ClientConnectionError("down"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SCAN_INTERVAL_S + 1))
    await hass.async_block_till_done()
    for eid in ("sensor.host_busy_pct", "select.host_load_profile", "switch.host_load_gate_hold",
                "binary_sensor.host_hold_active", "number.host_max_cores"):  # fmt: skip
        assert hass.states.get(eid).state == STATE_UNAVAILABLE


async def test_setup_retries_when_daemon_is_down(hass, aioclient_mock, mock_entry):
    aioclient_mock.get(f"{BASE}/v1/status", status=503, json={"ok": False, "error": "not_ready"})
    mock_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_auth_failure_starts_reauth(hass, aioclient_mock, mock_entry):
    aioclient_mock.get(f"{BASE}/v1/status", status=401, json={"ok": False, "error": "unauthorized"})
    mock_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_setup_source_refusal_retries_without_reauth(hass, aioclient_mock, mock_entry):
    aioclient_mock.get(
        f"{BASE}/v1/status",
        status=403,
        json={"ok": False, "error": "forbidden", "message": "source address is not allowed"},
    )
    mock_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_entry.entry_id)
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


async def test_select_posts_set_profile(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True, "accepted": True, "principal": "owner"})
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.host_load_profile", "option": "Silent (Fan Off)"}, blocking=True,
    )  # fmt: skip
    ((path, body),) = posts(aioclient_mock)
    assert path == "/v1/set"
    assert body["profile"] == "silent" and body["reason"]
    assert "owner" not in body  # the principal is the REST token, not a claim in the body


async def test_number_set_and_refused_409_raises_with_reason(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/set", json={"ok": True, "accepted": True})
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.host_max_cores", "value": 2.5}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.host_max_implementers", "value": 3}, blocking=True
    )
    assert [b["max_cores"] for p, b in posts(aioclient_mock) if "max_cores" in b] == [2.5]
    assert [b["max_implementers"] for p, b in posts(aioclient_mock) if "max_implementers" in b] == [3]
    assert isinstance(posts(aioclient_mock)[1][1]["max_implementers"], int)

    aioclient_mock.clear_requests()
    mock_status(aioclient_mock, status)
    aioclient_mock.post(
        f"{BASE}/v1/set",
        status=409,
        json={"ok": False, "refused": True, "restrictedBy": "owner", "reason": "TV playing"},
    )
    with pytest.raises(HomeAssistantError, match="TV playing"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.host_load_max", "value": 5.0}, blocking=True
        )


async def test_number_rejects_out_of_range_ram_floor(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    with pytest.raises(Exception, match="range|6"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.host_ram_floor_gb", "value": 7.0}, blocking=True
        )
    assert posts(aioclient_mock) == []


async def test_hold_switch_on_off(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/hold", json={"ok": True, "hold": "on"})
    for service in ("turn_on", "turn_off"):
        await hass.services.async_call(
            "switch", service, {"entity_id": "switch.host_load_gate_hold"}, blocking=True
        )
    sent = posts(aioclient_mock)
    assert [(p, b["on"], b["slot"]) for p, b in sent] == [
        ("/v1/hold", True, "heavy"),
        ("/v1/hold", False, "heavy"),
    ]


async def test_503_on_write_asks_to_retry_later(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/hold", status=503, json={"ok": False, "error": "not_ready"})
    with pytest.raises(HomeAssistantError, match="try again later"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.host_load_gate_hold"}, blocking=True
        )


async def test_401_on_write_starts_reauth(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(f"{BASE}/v1/hold", status=401, json={"ok": False, "error": "unauthorized"})
    with pytest.raises(HomeAssistantError, match="token"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.host_load_gate_hold"}, blocking=True
        )
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())


FREEZE_ENTITIES = (
    "switch.host_instant_quiet_freeze",
    "button.host_kill_build_daemons",
    "button.host_resume_paused_workers",
)


async def test_freeze_reap_unfreeze_unavailable_and_never_call_the_api(
    hass, aioclient_mock, mock_entry, status
):
    await setup(hass, mock_entry, aioclient_mock, status)
    for eid in FREEZE_ENTITIES:
        state = hass.states.get(eid)
        assert state.state == STATE_UNAVAILABLE
    features = hass.states.get("sensor.host_daemon_features")
    assert features.state == "none"
    assert features.attributes["freeze"] == "requires a loadgovd with freeze support"
    for domain, service, eid in (
        ("switch", "turn_on", FREEZE_ENTITIES[0]),
        ("button", "press", FREEZE_ENTITIES[1]),
        ("button", "press", FREEZE_ENTITIES[2]),
    ):
        await hass.services.async_call(domain, service, {"entity_id": eid}, blocking=True)  # skipped by HA
    assert posts(aioclient_mock) == []
    # Even when an action is invoked directly, the integration refuses before it touches the API.
    coordinator = mock_entry.runtime_data
    with pytest.raises(HomeAssistantError, match="freeze support"):
        coordinator.require("freeze")
    with pytest.raises(HomeAssistantError, match="freeze support"):
        coordinator.require("reap")
    assert posts(aioclient_mock) == []


async def test_freeze_reap_light_up_when_the_daemon_reports_them(hass, aioclient_mock, mock_entry, status):
    status["capabilities"] = ["freeze", "reap"]
    await setup(hass, mock_entry, aioclient_mock, status)
    for eid in FREEZE_ENTITIES:
        assert hass.states.get(eid).state != STATE_UNAVAILABLE
    assert hass.states.get("sensor.host_daemon_features").state == "freeze, reap"
    aioclient_mock.post(f"{BASE}/v1/reap", json={"ok": True})
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.host_kill_build_daemons"}, blocking=True
    )
    assert [p for p, _ in posts(aioclient_mock)] == ["/v1/reap"]


async def test_hold_switch_shows_only_owner_holds(hass, aioclient_mock, mock_entry, status):
    status["slots"]["heavy"].update(hold="drain profile", hold_origin="profile")
    await setup(hass, mock_entry, aioclient_mock, status)
    sw = hass.states.get("switch.host_load_gate_hold")
    assert sw.state == STATE_OFF and sw.attributes["profile_hold"] is True
    assert hass.states.get("binary_sensor.host_hold_active").state == STATE_ON  # a hold is a hold


async def test_hold_switch_on_for_owner_hold_with_origin(hass, aioclient_mock, mock_entry, status):
    status["slots"]["heavy"].update(hold="Home Assistant", hold_origin="owner")
    await setup(hass, mock_entry, aioclient_mock, status)
    sw = hass.states.get("switch.host_load_gate_hold")
    assert sw.state == STATE_ON and sw.attributes["profile_hold"] is False


async def test_403_source_refusal_on_write_does_not_start_reauth(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    aioclient_mock.post(
        f"{BASE}/v1/hold",
        status=403,
        json={"ok": False, "error": "forbidden", "message": "source address is not allowed"},
    )
    with pytest.raises(HomeAssistantError, match="source address not allowed"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.host_load_gate_hold"}, blocking=True
        )
    await hass.async_block_till_done()
    assert not any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
