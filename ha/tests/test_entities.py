"""Setup, availability, entity states and writes."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
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
    assert float(s("number.host_max_light").state) == 4
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


def _stray(pid: int) -> dict:
    return {
        "pid": pid, "start_time": 1000 + pid, "comm": "node", "cmdline": "node x.js", "cwd": "/w/p",
        "cgroup": "/user.slice", "project": "p", "cpu_pct": 91.5, "hot_since": "2026-01-01T00:00:00+00:00",
        "age_s": 600, "orphan": True, "session": {"claude_pid": None, "session_id": "abc"},
    }  # fmt: skip


async def test_strays_sensor_state_and_attributes(hass, aioclient_mock, mock_entry, status):
    status["strays"] = {
        "enabled": True, "enforce": False, "check_s": 60, "last_check": None,
        "items": [_stray(p) for p in range(1, 13)],
    }  # fmt: skip
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_strays")
    assert st.state == "12"
    assert st.attributes["enforce"] is False
    assert st.attributes["friendly_name"].endswith("Stray processes")
    assert st.attributes["icon"] == "mdi:ghost"
    assert len(st.attributes["items"]) == 10
    assert st.attributes["items"][0] == {
        "pid": 1, "start_time": 1001, "comm": "node", "project": "p", "cwd": "/w/p",
        "cpu_pct": 91.5, "age_s": 600, "session_id": "abc",
    }  # fmt: skip


async def test_strays_sensor_unavailable_on_an_older_daemon(hass, aioclient_mock, mock_entry, status):
    assert "strays" not in status
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_strays").state == STATE_UNAVAILABLE


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

    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.host_max_light", "value": 6}, blocking=True
    )
    sent = [b["max_light"] for p, b in posts(aioclient_mock) if "max_light" in b]
    assert sent == [6] and isinstance(sent[0], int)

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


async def test_host_id_is_an_attribute_and_changes_no_entity_or_device_id(
    hass, aioclient_mock, mock_entry, status
):
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    from custom_components.loadgov.const import DOMAIN

    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_daemon_features").attributes["host_id"] == "build-a"
    ent = er.async_get(hass).async_get("sensor.host_daemon_features")
    assert ent.unique_id == f"{mock_entry.entry_id}_daemon_features"
    device = dr.async_get(hass).async_get(ent.device_id)
    assert device.identifiers == {(DOMAIN, mock_entry.entry_id)}  # the host id is not an identifier
    assert device.serial_number == "build-a"
    assert hass.states.get("select.host_load_profile") is not None  # entity ids keep their form


async def test_an_older_daemon_without_a_host_id_still_works(hass, aioclient_mock, mock_entry, status):
    del status["host"]
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_daemon_features").attributes["host_id"] is None


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


# --- queue view, running jobs, host load, per-project compute ---


def _waiter(pos, **kw):
    w = {
        "position": pos, "project": "web-app", "label": f"gate-{pos}", "kind": "gate", "key": None,
        "url": None, "pid": 100 + pos, "queued_since": "2026-11-10T09:00:00Z", "wait_s": 60 * pos,
        "expected_s": 300, "eta": "2026-11-10T10:05:00Z",
    }  # fmt: skip
    return {**w, **kw}


def _job(slot, **kw):
    j = {
        "kind": "gate", "project": "web-app", "label": "gate-run", "unit": "u.scope", "cgroup": "/x",
        "pid": 7, "slot": slot, "since": "2026-11-10T09:50:00Z", "key": "PROJ-41",
        "url": "https://tracker.example/issues/PROJ-41", "expected_s": 900,
        "eta": "2026-11-10T10:05:00Z", "overdue": False,
    }  # fmt: skip
    return {**j, **kw}


async def test_host_load_and_level_sensors(hass, aioclient_mock, mock_entry, status):
    status["cpu"] |= {"load1": 2.5, "load5": 1.75, "load1_per_core": 0.31}
    status["level"] = "BUSY"
    await setup(hass, mock_entry, aioclient_mock, status)
    s = hass.states.get
    assert s("sensor.host_load_1min").state == "2.5"
    assert s("sensor.host_load_5min").state == "1.75"
    assert s("sensor.host_load_per_core").state == "0.31"
    assert s("sensor.host_level").state == "BUSY"
    assert s("sensor.host_level").attributes["options"] == ["OK", "BUSY", "LIMIT"]


async def test_slot_next_head_waiter_and_bounds(hass, aioclient_mock, mock_entry, status):
    waiters = [_waiter(1, key="PROJ-42", url="https://tracker.example/issues/PROJ-42")]
    waiters += [_waiter(p) for p in range(2, 14)]
    status["slots"]["heavy"]["waiters"] = list(reversed(waiters))  # order in the list does not matter
    status["slots"]["compile"]["waiters"] = [_waiter(1, label="cargo-build")]
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_heavy_slot_next")
    assert st.state == "PROJ-42"
    assert st.attributes["count"] == 13
    assert st.attributes["project"] == "web-app"
    assert len(st.attributes["queue"]) == 10
    assert st.attributes["queue"][0] == {
        "position": 1, "project": "web-app", "label": "gate-1", "kind": "gate", "key": "PROJ-42",
        "url": "https://tracker.example/issues/PROJ-42", "wait_s": 60, "eta": "2026-11-10T10:05:00Z",
    }  # fmt: skip
    assert hass.states.get("sensor.host_compile_slot_next").state == "cargo-build"


async def test_slot_next_none_when_empty_and_unavailable_on_an_older_daemon(
    hass, aioclient_mock, mock_entry, status
):
    status["slots"]["heavy"]["waiters"] = []
    assert "waiters" not in status["slots"]["compile"]
    await setup(hass, mock_entry, aioclient_mock, status)
    empty = hass.states.get("sensor.host_heavy_slot_next")
    assert empty.state == "none"
    assert empty.attributes["count"] == 0
    assert empty.attributes["queue"] == []
    assert hass.states.get("sensor.host_compile_slot_next").state == STATE_UNAVAILABLE


async def test_holder_eta_timestamp_and_attributes(hass, aioclient_mock, mock_entry, status):
    status["jobs"] = [_job("heavy", overdue=True), _job(None, label="impl-a", kind="impl")]
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_heavy_slot_holder_eta")
    assert st.state == "2026-11-10T10:05:00+00:00"
    assert st.attributes["device_class"] == "timestamp"
    assert st.attributes["key"] == "PROJ-41"
    assert st.attributes["url"] == "https://tracker.example/issues/PROJ-41"
    assert (st.attributes["project"], st.attributes["label"], st.attributes["kind"]) == (
        "web-app", "gate-run", "gate",
    )  # fmt: skip
    assert st.attributes["expected_s"] == 900
    assert st.attributes["overdue"] is True
    # the compile slot is free: no job holds it
    assert hass.states.get("sensor.host_compile_slot_holder_eta").state == STATE_UNKNOWN


@pytest.mark.parametrize("eta", [None, "not a time", 42])
async def test_holder_eta_unknown_without_a_usable_estimate(hass, aioclient_mock, mock_entry, status, eta):
    status["jobs"] = [_job("compile", eta=eta, expected_s=None)]
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_compile_slot_holder_eta").state == STATE_UNKNOWN


async def test_running_jobs_count_attributes_and_bound(hass, aioclient_mock, mock_entry, status):
    status["jobs"] = [_job("heavy"), *(_job(None, label=f"impl-{n}", kind="impl") for n in range(11))]
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_running_jobs")
    assert st.state == "12"
    assert len(st.attributes["jobs"]) == 10
    assert st.attributes["jobs"][0] == {
        "kind": "gate", "project": "web-app", "label": "gate-run", "key": "PROJ-41",
        "url": "https://tracker.example/issues/PROJ-41", "slot": "heavy",
        "since": "2026-11-10T09:50:00Z", "eta": "2026-11-10T10:05:00Z", "overdue": False,
    }  # fmt: skip


async def test_running_jobs_zero_on_an_older_daemon(hass, aioclient_mock, mock_entry, status):
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_running_jobs")
    assert st.state == "0"
    assert st.attributes["jobs"] == []


async def test_list_attributes_are_kept_out_of_the_recorder(hass, aioclient_mock, mock_entry, status):
    status["slots"]["heavy"]["waiters"] = [_waiter(1)]
    await setup(hass, mock_entry, aioclient_mock, status)
    for entity_id, attr in (
        ("sensor.host_heavy_slot_next", "queue"),
        ("sensor.host_running_jobs", "jobs"),
    ):
        # the set the recorder reads: the entity class collects it from all bases and mixins
        entities = hass.data["entity_components"]["sensor"].entities
        entity = next(e for e in entities if e.entity_id == entity_id)
        assert attr in entity._Entity__combined_unrecorded_attributes


async def test_slot_next_state_is_cut_to_255_characters(hass, aioclient_mock, mock_entry, status):
    label = "x" * 300
    status["slots"]["heavy"]["waiters"] = [_waiter(1, label=label)]
    await setup(hass, mock_entry, aioclient_mock, status)
    state = hass.states.get("sensor.host_heavy_slot_next")
    assert len(state.state) == 255
    assert state.state == "x" * 254 + "…"
    assert state.attributes["queue"][0]["label"] == label


def _projects(*names):
    return {
        n: {"cpu_pct": 250.0, "cores": 2.5, "docker_cpu_pct": 50.0, "mem_mb": 2048, "jobs": 1,
            "waiting": 2, "slice": f"/build.slice/{n}.slice"}
        for n in names
    }  # fmt: skip


async def test_project_sensors_are_created_from_the_status(hass, aioclient_mock, mock_entry, status):
    status["projects"] = _projects("web-app", "ci")
    status["projects"]["ci"]["cores"] = 0.123
    status["projects"]["ci"]["mem_mb"] = None
    await setup(hass, mock_entry, aioclient_mock, status)
    cpu = hass.states.get("sensor.host_project_web_app_cpu")
    assert cpu.state == "2.5"
    assert cpu.attributes["unit_of_measurement"] == "cores"
    assert (cpu.attributes["cpu_pct"], cpu.attributes["docker_cpu_pct"]) == (250.0, 50.0)
    assert (cpu.attributes["jobs"], cpu.attributes["waiting"]) == (1, 2)
    mem = hass.states.get("sensor.host_project_web_app_memory")
    assert mem.state == "2048"
    assert mem.attributes["device_class"] == "data_size"
    assert mem.attributes["unit_of_measurement"] == "MB"
    assert hass.states.get("sensor.host_project_ci_cpu").state == "0.12"
    assert hass.states.get("sensor.host_project_ci_memory").state == STATE_UNKNOWN


async def _poll(hass, aioclient_mock, status, n):
    aioclient_mock.clear_requests()
    mock_status(aioclient_mock, status)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=n * (SCAN_INTERVAL_S + 1)))
    await hass.async_block_till_done()


async def test_project_sensors_appear_later_and_go_unavailable_when_the_project_disappears(
    hass, aioclient_mock, mock_entry, status
):
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_project_web_app_cpu") is None

    status["projects"] = _projects("web-app")
    await _poll(hass, aioclient_mock, status, 1)
    assert hass.states.get("sensor.host_project_web_app_cpu").state == "2.5"
    assert hass.states.get("sensor.host_project_web_app_memory").state == "2048"

    status["projects"] = {}
    await _poll(hass, aioclient_mock, status, 2)
    assert hass.states.get("sensor.host_project_web_app_cpu").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.host_project_web_app_memory").state == STATE_UNAVAILABLE

    status["projects"] = _projects("web-app")
    await _poll(hass, aioclient_mock, status, 3)
    assert hass.states.get("sensor.host_project_web_app_cpu").state == "2.5"


async def test_dashboard_queue_card_renders(hass, aioclient_mock, mock_entry, status):
    from pathlib import Path

    import yaml
    from homeassistant.helpers.template import Template

    status["jobs"] = [_job("heavy", overdue=True)]
    status["slots"]["heavy"]["waiters"] = [
        _waiter(1, key="PROJ-42", url="https://tracker.example/issues/PROJ-42"),
        _waiter(2),
    ]
    await setup(hass, mock_entry, aioclient_mock, status)
    board = yaml.safe_load((Path(__file__).parents[1] / "dashboard" / "loadgov.yaml").read_text())
    card = next(
        c
        for section in board["views"][0]["sections"]
        for c in section["cards"]
        if c.get("type") == "markdown"
    )
    text = Template(card["content"], hass).async_render()
    assert "[PROJ-41](https://tracker.example/issues/PROJ-41) (heavy slot)" in text
    assert "(overdue)" in text
    assert "1. [PROJ-42](https://tracker.example/issues/PROJ-42) (web-app, waiting 1 min" in text
    assert "2. gate-2 (web-app" in text
    assert "- empty" in text  # the compile queue


async def test_dashboard_card_survives_null_wait_and_bad_eta(hass, aioclient_mock, mock_entry, status):
    from pathlib import Path

    import yaml
    from homeassistant.helpers.template import Template

    status["jobs"] = [_job("heavy", eta="not-a-time")]
    status["slots"]["heavy"]["waiters"] = [_waiter(1, wait_s=None, eta="garbage")]
    await setup(hass, mock_entry, aioclient_mock, status)
    board = yaml.safe_load((Path(__file__).parents[1] / "dashboard" / "loadgov.yaml").read_text())
    card = next(
        c
        for section in board["views"][0]["sections"]
        for c in section["cards"]
        if c.get("type") == "markdown"
    )
    text = Template(card["content"], hass).async_render()
    assert "waiting 0 min, starts ~ ?)" in text
    assert "done ~ ?" in text


LIGHT = {
    "running": 2, "max": 4, "per_project_max": 2, "cpu_quota_pct": 100, "memory_max_mb": 1024,
    "max_runtime_s": 600,
}  # fmt: skip


async def test_light_lane_sensor_state_and_attributes(hass, aioclient_mock, mock_entry, status):
    status["light"] = dict(LIGHT)
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_light_lane")
    assert st.state == "2"
    assert st.attributes["friendly_name"].endswith("Host Light Lane")
    assert {k: st.attributes[k] for k in LIGHT if k != "running"} == {
        k: v for k, v in LIGHT.items() if k != "running"
    }


async def test_light_lane_sensor_unavailable_on_an_older_daemon(hass, aioclient_mock, mock_entry, status):
    assert "light" not in status
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_light_lane").state == STATE_UNAVAILABLE


async def test_light_jobs_show_in_running_jobs_and_leave_slot_sensors_alone(
    hass, aioclient_mock, mock_entry, status
):
    status["light"] = dict(LIGHT)
    status["jobs"] = [_job("heavy"), _job(None, kind="light", label="check", key=None, url=None, eta=None)]
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_running_jobs")
    assert st.state == "2"
    assert [j["kind"] for j in st.attributes["jobs"]] == ["gate", "light"]
    assert hass.states.get("sensor.host_heavy_slot_holder_eta").state == "2026-11-10T10:05:00+00:00"
    assert hass.states.get("sensor.host_compile_slot_holder_eta").state == STATE_UNKNOWN


async def test_disk_used_sensor_state_and_alert_attribute(hass, aioclient_mock, mock_entry, status):
    status["disk"] = {
        "used_pct": 83.5,
        "per_path": [{"path": "/", "free_gb": 40, "total_gb": 250, "used_pct": 83.5}],
    }
    status["levels"] = {"disk_alert": "BUSY"}
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_disk_used")
    assert st.state == "83.5"
    assert st.attributes["unit_of_measurement"] == "%"
    assert st.attributes["state_class"] == "measurement"
    assert st.attributes["disk_alert"] == "BUSY"


async def test_disk_used_sensor_unavailable_without_used_pct(hass, aioclient_mock, mock_entry, status):
    status["disk"] = {"per_path": []}
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_disk_used").state == STATE_UNAVAILABLE


def _image_job(n, **kw):
    j = {
        "id": n, "position": n, "project": "web-app", "label": f"web-{n}", "key": "PROJ-42",
        "url": "https://tracker.example/issues/PROJ-42", "channel": "release",
        "submitted": "2026-11-10T09:00:00Z", "planned_start": f"2026-11-11T0{n % 10}:00:00Z",
        "planned_finish": f"2026-11-11T0{n % 10}:30:00Z", "state": "queued",
    }  # fmt: skip
    return {**j, **kw}


def _image(queue, **kw):
    return {
        "queued": sum(j["state"] != "running" for j in queue),
        "running": sum(j["state"] == "running" for j in queue),
        "window": "00:00-06:00", "timezone": "Europe/Berlin", "in_window": False,
        "next_window_start": "2026-11-11T23:00:00Z", "queue": queue, **kw,
    }  # fmt: skip


async def test_image_train_state_and_attributes(hass, aioclient_mock, mock_entry, status):
    status["image"] = _image([_image_job(1, state="running"), _image_job(2), _image_job(3, state="now")])
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_image_train")
    assert st.state == "3"  # the running job and the two waiting ones
    assert (st.attributes["window"], st.attributes["timezone"]) == ("00:00-06:00", "Europe/Berlin")
    assert st.attributes["in_window"] is False
    assert st.attributes["next_window_start"] == "2026-11-11T23:00:00Z"
    assert [j["id"] for j in st.attributes["queue"]] == [1, 2, 3]
    assert st.attributes["queue"][1] == {
        "id": 2, "position": 2, "project": "web-app", "label": "web-2", "key": "PROJ-42",
        "url": "https://tracker.example/issues/PROJ-42", "channel": "release",
        "planned_start": "2026-11-11T02:00:00Z", "planned_finish": "2026-11-11T02:30:00Z",
        "state": "queued",
    }  # fmt: skip


async def test_image_train_queue_is_bounded_and_unrecorded(hass, aioclient_mock, mock_entry, status):
    status["image"] = _image([_image_job(n) for n in range(1, 14)])
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_image_train")
    assert st.state == "13"
    assert len(st.attributes["queue"]) == 10
    entities = hass.data["entity_components"]["sensor"].entities
    entity = next(e for e in entities if e.entity_id == "sensor.host_image_train")
    assert "queue" in entity._Entity__combined_unrecorded_attributes


async def test_image_train_counts_come_from_the_daemon_not_the_cut_list(
    hass, aioclient_mock, mock_entry, status
):
    status["image"] = _image([_image_job(n) for n in range(1, 21)], queued=34, running=1)
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_image_train")
    assert st.state == "35"
    assert len(st.attributes["queue"]) == 10


async def test_image_train_empty_queue(hass, aioclient_mock, mock_entry, status):
    status["image"] = _image([])
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_image_train")
    assert (st.state, st.attributes["queue"]) == ("0", [])


async def test_image_train_unavailable_on_an_older_daemon(hass, aioclient_mock, mock_entry, status):
    assert "image" not in status
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_image_train").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.host_image_train_next_start").state == STATE_UNKNOWN


async def test_image_train_next_start_is_the_head_job_else_the_window(
    hass, aioclient_mock, mock_entry, status
):
    status["image"] = _image([_image_job(1, state="running"), _image_job(2)])
    await setup(hass, mock_entry, aioclient_mock, status)
    st = hass.states.get("sensor.host_image_train_next_start")
    assert st.state == "2026-11-11T02:00:00+00:00"
    assert st.attributes["device_class"] == "timestamp"


async def test_image_train_next_start_skips_a_running_head_job(hass, aioclient_mock, mock_entry, status):
    status["image"] = _image([_image_job(1, state="running", planned_start="2026-11-10T22:00:00Z")])
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_image_train_next_start").state == "2026-11-11T23:00:00+00:00"


@pytest.mark.parametrize("planned", [None, "not a time", 42])
async def test_image_train_next_start_falls_back_to_the_next_window(
    hass, aioclient_mock, mock_entry, status, planned
):
    status["image"] = _image([_image_job(1, planned_start=planned)])
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_image_train_next_start").state == "2026-11-11T23:00:00+00:00"


async def test_image_train_next_start_unknown_without_any_time(hass, aioclient_mock, mock_entry, status):
    status["image"] = _image([], next_window_start="garbage")
    await setup(hass, mock_entry, aioclient_mock, status)
    assert hass.states.get("sensor.host_image_train_next_start").state == STATE_UNKNOWN
