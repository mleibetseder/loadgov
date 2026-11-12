"""Smart triggers: presence, TV quiet, quiet hours, remote host load (REST mocked, time stepped)."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.loadgov.const import DOMAIN

from .conftest import BASE, HOST, PORT, TOKEN, posts

ZONE, TV, TV2, QUIET, LOAD, CPU = (
    "zone.home",
    "media_player.living_room_tv",
    "media_player.other_tv",
    "input_boolean.quiet_hours",
    "sensor.remote_load",
    "sensor.remote_cpu",
)
OPTS = {
    "presence_zone": ZONE,
    "tv_media_players": [TV, TV2],
    "quiet_hours_switch": QUIET,
    "remote_load_sensor": LOAD,
    "remote_cpu_sensor": CPU,
    "remote_load_threshold": 6.0,
    "remote_cpu_threshold": 80.0,
}
ALL_ON = {
    "trigger_presence": True,
    "trigger_tv_quiet": True,
    "trigger_quiet_hours": True,
    "trigger_remote_load": True,
}
TV_CEILING = (
    "/v1/ceiling",
    {"profile": "silent", "ttl": "15m", "reason": "HA trigger:tv: TV playing"},
)
REMOTE_LIMITS = (
    "/v1/set",
    {
        "max_implementers": 1,
        "max_cores": 2.5,
        "ttl": "15m",
        "reason": "HA trigger:remote_load: load or CPU high",
    },
)


def only(name: str) -> dict:
    return {**OPTS, f"trigger_{name}": True}


def mock_daemon(mocker, status, post_kwargs=None):
    """(Re)register the mocks; the recorded calls survive, so a test can change the status mid-way."""
    mocker._mocks.clear()
    mocker.get(f"{BASE}/v1/status", json={"ok": True, "status": status})
    for path in ("/v1/set", "/v1/ceiling"):
        mocker.post(f"{BASE}{path}", **(post_kwargs or {"json": {"ok": True, "accepted": True}}))


def with_ceiling(status, reason, profile="silent"):
    status["limits"]["ceiling"] = profile
    status["limits"]["ceiling_info"] = {
        "profile": profile,
        "reason": reason,
        "principal": "owner",
        "set_at": "2026-11-10T10:00:00Z",
        "expires_at": None,
    }


def with_profile(status, profile, reason):
    status["limits"]["profile"] = profile
    status["limits"]["profile_info"] = {
        "profile": profile,
        "reason": reason,
        "principal": "owner",
        "set_at": "2026-11-10T10:00:00Z",
        "expires_at": None,
    }


def with_override(status, param, value, reason, principal="owner"):
    status["limits"]["overrides"].append(
        {
            "param": param,
            "value": value,
            "principal": principal,
            "reason": reason,
            "set_at": "2026-11-10T10:00:00Z",
            "expires_at": None,
        }
    )
    status["limits"]["effective"][param] = value


TV_TAGGED, BED_TAGGED, HUMAN = (
    "HA trigger:tv: TV playing",
    "HA trigger:quiet_hours: quiet hours",
    "movie night",
)


async def start(hass, mocker, status, options, states=None):
    for entity, state in (states or {}).items():
        hass.states.async_set(entity, state)
    mock_daemon(mocker, status)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Build Host Load Governor",
        unique_id=f"{HOST}:{PORT}",
        data={"host": HOST, "port": PORT, "token": TOKEN},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def start_empty(hass, mocker, status):
    return await start(hass, mocker, status, {"trigger_remote_load": True, "trigger_quiet_hours": True})


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def change(hass, entity, state):
    hass.states.async_set(entity, state)
    await hass.async_block_till_done()


def info(hass, name):
    return hass.states.get("sensor.host_triggers").attributes[name]


# --- disabled ---


async def test_disabled_triggers_never_call_the_api(hass, aioclient_mock, freezer, status):
    states = {ZONE: "1", TV: "idle", QUIET: "off", LOAD: "1", CPU: "1"}
    await start(hass, aioclient_mock, status, OPTS, states)
    for entity, state in ((ZONE, "0"), (TV, "playing"), (QUIET, "on"), (LOAD, "20"), (CPU, "99")):
        await change(hass, entity, state)
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []
    assert hass.states.get("sensor.host_triggers").state == "0"
    assert info(hass, "presence")["enabled"] is False


async def test_only_the_enabled_trigger_runs(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {ZONE: "1", TV: "idle", QUIET: "off"})
    await change(hass, TV, "playing")
    await change(hass, ZONE, "0")
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []


# --- presence ---


async def test_presence_empty_for_10_minutes_boosts_and_rearms(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "1"})
    await change(hass, ZONE, "0")
    await advance(hass, freezer, 9 * 60)
    assert posts(aioclient_mock) == []  # 10-minute debounce
    await advance(hass, freezer, 60)
    boost = ("/v1/set", {"profile": "boost", "ttl": "30m", "reason": "HA trigger:presence: nobody home"})
    assert posts(aioclient_mock) == [boost]
    assert hass.states.get("sensor.host_triggers").state == "1"
    with_profile(status, "boost", "HA trigger:presence: nobody home")
    mock_daemon(aioclient_mock, status)
    await advance(hass, freezer, 14 * 60)
    assert len(posts(aioclient_mock)) == 1
    await advance(hass, freezer, 60)  # 15 minutes after the first arm
    assert posts(aioclient_mock) == [boost, boost]
    await advance(hass, freezer, 15 * 60)
    assert posts(aioclient_mock) == [boost, boost, boost]


async def test_presence_back_within_the_debounce_sends_nothing(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "1"})
    await change(hass, ZONE, "0")
    await advance(hass, freezer, 5 * 60)
    await change(hass, ZONE, "1")
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []


async def test_presence_someone_arrives_returns_to_balanced_without_ttl(
    hass, aioclient_mock, freezer, status
):
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)  # the start-up evaluation armed the debounce
    with_profile(status, "boost", "HA trigger:presence: nobody home")
    mock_daemon(aioclient_mock, status)
    await change(hass, ZONE, "2")
    assert posts(aioclient_mock)[-1] == (
        "/v1/set",
        {"profile": "balanced", "reason": "HA trigger:presence: someone home"},
    )
    n = len(posts(aioclient_mock))
    await advance(hass, freezer, 3600)  # re-arming stopped
    assert len(posts(aioclient_mock)) == n


async def test_presence_does_not_fight_a_manual_profile(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)
    assert len(posts(aioclient_mock)) == 1
    with_profile(status, "silent", HUMAN)  # the owner chose silent meanwhile
    mock_daemon(aioclient_mock, status)
    await advance(hass, freezer, 15 * 60)
    await advance(hass, freezer, 15 * 60)
    assert len(posts(aioclient_mock)) == 1  # stopped re-arming
    assert "someone else" in info(hass, "presence")["last_action"]
    await change(hass, ZONE, "1")  # arrival: the profile is not boost, so there is nothing to undo
    assert len(posts(aioclient_mock)) == 1


async def test_presence_arrival_after_a_human_boost_sends_nothing(hass, aioclient_mock, freezer, status):
    with_profile(status, "boost", HUMAN)
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == []  # a boost it did not set is not overridden
    await change(hass, ZONE, "1")
    assert posts(aioclient_mock) == []


async def test_presence_after_a_restart_recognises_its_own_boost(hass, aioclient_mock, freezer, status):
    with_profile(status, "boost", "HA trigger:presence: nobody home")
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)  # re-armed, the boost is ours
    assert len(posts(aioclient_mock)) == 1
    await change(hass, ZONE, "1")
    assert posts(aioclient_mock)[-1][1]["profile"] == "balanced"


async def test_presence_does_not_boost_over_a_human_balanced(hass, aioclient_mock, freezer, status):
    with_profile(status, "balanced", HUMAN)  # chosen by hand, e.g. before a restart
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == []
    assert "someone else" in info(hass, "presence")["last_action"]


async def test_presence_skips_when_the_profile_was_chosen_by_the_owner(hass, aioclient_mock, freezer, status):
    with_profile(status, "silent", HUMAN)
    await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == []
    assert "skipped" in info(hass, "presence")["last_action"]


# --- TV quiet ---


async def test_tv_playing_sets_ceiling_rearms_and_clears_after_5_minutes(
    hass, aioclient_mock, freezer, status
):
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "idle", TV2: "off"})
    await change(hass, TV2, "playing")  # any of the chosen players
    assert posts(aioclient_mock) == [TV_CEILING]
    await advance(hass, freezer, 10 * 60)
    assert posts(aioclient_mock) == [TV_CEILING, TV_CEILING]  # re-armed every 10 minutes
    await advance(hass, freezer, 10 * 60)
    assert len(posts(aioclient_mock)) == 3
    with_ceiling(status, TV_TAGGED)
    mock_daemon(aioclient_mock, status)
    await change(hass, TV2, "paused")
    await advance(hass, freezer, 4 * 60)
    assert len(posts(aioclient_mock)) == 3  # 5-minute debounce
    await advance(hass, freezer, 60)
    assert posts(aioclient_mock)[-1] == (
        "/v1/ceiling",
        {"clear": True, "reason": "HA trigger:tv: TV stopped"},
    )
    n = len(posts(aioclient_mock))
    await advance(hass, freezer, 3600)
    assert len(posts(aioclient_mock)) == n


async def test_tv_playing_again_cancels_the_clear(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "playing"})
    assert len(posts(aioclient_mock)) == 1  # start-up self-heal
    with_ceiling(status, TV_TAGGED)
    mock_daemon(aioclient_mock, status)
    await change(hass, TV, "paused")
    await advance(hass, freezer, 4 * 60)
    await change(hass, TV, "playing")
    await advance(hass, freezer, 10 * 60)
    assert not any("clear" in body for _, body in posts(aioclient_mock))


async def test_tv_does_not_clear_a_ceiling_it_does_not_own(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "playing"})
    status["limits"]["ceiling"] = None  # the TTL expired or someone cleared it
    mock_daemon(aioclient_mock, status)
    await change(hass, TV, "idle")
    await advance(hass, freezer, 5 * 60)
    assert len(posts(aioclient_mock)) == 1  # no clear
    assert "nothing to clear" in info(hass, "tv_quiet")["last_action"]


async def test_tv_idle_at_start_never_clears_a_human_ceiling(hass, aioclient_mock, freezer, status):
    with_ceiling(status, HUMAN)
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "idle"})
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []


async def test_tv_after_a_restart_recognises_and_clears_its_own_ceiling(
    hass, aioclient_mock, freezer, status
):
    with_ceiling(status, TV_TAGGED)  # set before the restart; the RAM memory is gone
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "idle"})
    await advance(hass, freezer, 4 * 60)
    assert posts(aioclient_mock) == []
    await advance(hass, freezer, 60)
    assert posts(aioclient_mock) == [("/v1/ceiling", {"clear": True, "reason": "HA trigger:tv: TV stopped"})]


async def test_tv_stop_does_not_clear_a_human_silent_ceiling(hass, aioclient_mock, freezer, status):
    with_ceiling(status, HUMAN)
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "idle"})
    await change(hass, TV, "playing")  # no ceiling_set over the human one
    assert posts(aioclient_mock) == []
    assert "human ceiling active" in info(hass, "tv_quiet")["last_action"]
    await change(hass, TV, "idle")
    await advance(hass, freezer, 5 * 60)
    assert posts(aioclient_mock) == []
    assert "left alone" in info(hass, "tv_quiet")["last_action"]


# --- quiet hours ---


async def test_quiet_on_sets_ceiling_until_seven_and_off_clears(hass, aioclient_mock, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "off"})
    await change(hass, QUIET, "on")
    until = f"07:00 {hass.config.time_zone}"
    assert posts(aioclient_mock) == [
        (
            "/v1/ceiling",
            {"profile": "silent", "until": until, "reason": "HA trigger:quiet_hours: quiet hours"},
        )
    ]
    with_ceiling(status, BED_TAGGED)
    mock_daemon(aioclient_mock, status)
    await change(hass, QUIET, "off")
    assert posts(aioclient_mock)[-1] == (
        "/v1/ceiling",
        {"clear": True, "reason": "HA trigger:quiet_hours: quiet hours over"},
    )


async def test_quiet_off_leaves_a_foreign_ceiling_alone(hass, aioclient_mock, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "off"})
    await change(hass, QUIET, "on")
    with_ceiling(status, HUMAN, "balanced")  # someone replaced our ceiling
    mock_daemon(aioclient_mock, status)
    await change(hass, QUIET, "off")
    assert len(posts(aioclient_mock)) == 1


async def test_quiet_never_touches_a_human_ceiling(hass, aioclient_mock, status):
    with_ceiling(status, HUMAN)
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "off"})
    await change(hass, QUIET, "on")
    assert posts(aioclient_mock) == []
    assert "human ceiling active" in info(hass, "quiet_hours")["last_action"]
    await change(hass, QUIET, "off")
    assert posts(aioclient_mock) == []


async def test_quiet_after_a_restart_clears_its_own_ceiling_when_the_switch_is_off(
    hass, aioclient_mock, status
):
    with_ceiling(status, BED_TAGGED)
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "off"})
    assert posts(aioclient_mock) == [
        ("/v1/ceiling", {"clear": True, "reason": "HA trigger:quiet_hours: quiet hours over"})
    ]


async def test_quiet_reapplied_on_start_when_already_on(hass, aioclient_mock, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "on"})
    assert len(posts(aioclient_mock)) == 1
    assert posts(aioclient_mock)[0][1]["until"].startswith("07:00 ")


async def test_quiet_off_at_start_sends_nothing(hass, aioclient_mock, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "off"})
    assert posts(aioclient_mock) == []


async def test_tv_and_quiet_hours_share_the_ceiling(hass, aioclient_mock, freezer, status):
    options = {**OPTS, "trigger_tv_quiet": True, "trigger_quiet_hours": True}
    await start(hass, aioclient_mock, status, options, {QUIET: "on", TV: "playing"})
    assert len(posts(aioclient_mock)) == 2  # TV ttl first, then the longer quiet hours ceiling replaces it
    assert "until" in posts(aioclient_mock)[-1][1]
    with_ceiling(status, BED_TAGGED)
    mock_daemon(aioclient_mock, status)
    await advance(hass, freezer, 10 * 60)
    assert len(posts(aioclient_mock)) == 2  # the TV re-arm does not shorten the quiet hours ceiling
    await change(hass, TV, "idle")
    await advance(hass, freezer, 5 * 60)
    assert not any("clear" in body for _, body in posts(aioclient_mock))  # quiet hours still wants it
    await change(hass, TV, "playing")
    await change(hass, QUIET, "off")  # the TV is playing: the ceiling goes back to a TTL
    assert posts(aioclient_mock)[-1] == TV_CEILING


# --- remote host load ---


async def test_remote_load_high_for_10_minutes_limits_and_rearms(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "3", CPU: "10"})
    await change(hass, LOAD, "9.1")
    await advance(hass, freezer, 9 * 60)
    assert posts(aioclient_mock) == []
    await advance(hass, freezer, 60)
    assert posts(aioclient_mock) == [REMOTE_LIMITS]
    await advance(hass, freezer, 10 * 60)
    assert posts(aioclient_mock) == [REMOTE_LIMITS, REMOTE_LIMITS]
    await change(hass, LOAD, "2")  # cleared: nothing is sent, the TTL restores the limits
    await advance(hass, freezer, 3600)
    assert len(posts(aioclient_mock)) == 2
    assert info(hass, "remote_load")["active"] is False


async def test_remote_cpu_high_triggers_and_a_dip_resets_the_debounce(hass, aioclient_mock, freezer, status):
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "3", CPU: "10"})
    await change(hass, CPU, "90")
    await advance(hass, freezer, 6 * 60)
    await change(hass, CPU, "50")
    await advance(hass, freezer, 60)
    await change(hass, CPU, "90")
    await advance(hass, freezer, 9 * 60)
    assert posts(aioclient_mock) == []
    await advance(hass, freezer, 60)
    assert len(posts(aioclient_mock)) == 1


async def test_remote_thresholds_come_from_the_options(hass, aioclient_mock, freezer, status):
    options = {**only("remote_load"), "remote_load_threshold": 4.0, "remote_cpu_threshold": 30.0}
    await start(hass, aioclient_mock, status, options, {LOAD: "5", CPU: "10"})  # 5 > 4.0 (but not > 6.0)
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == [REMOTE_LIMITS]


async def test_remote_thresholds_are_strict_and_unavailable_is_ignored(hass, aioclient_mock, freezer, status):
    await start(
        hass, aioclient_mock, status, only("remote_load"), {LOAD: "6.0", CPU: "80"}
    )  # not strictly above
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []
    await change(hass, LOAD, "unavailable")
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []


async def test_remote_without_thresholds_or_sensors_does_nothing(hass, aioclient_mock, freezer, status):
    bare = {"trigger_remote_load": True, "remote_load_sensor": LOAD, "remote_cpu_sensor": CPU}
    await start(hass, aioclient_mock, status, bare, {LOAD: "99", CPU: "99"})  # no threshold set: never high
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []
    await start_empty(hass, aioclient_mock, status)  # no entities at all: nothing to watch, nothing to do
    await advance(hass, freezer, 3600)
    assert posts(aioclient_mock) == []


async def test_remote_never_replaces_what_a_human_set(hass, aioclient_mock, freezer, status):
    with_override(status, "max_cores", 1.0, "slider")  # tighter than the target
    with_override(status, "max_implementers", 0, "slider")
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    await advance(hass, freezer, 600)
    await advance(hass, freezer, 10 * 60)
    await advance(hass, freezer, 3600)  # well past the TTL: the human values were never touched
    assert posts(aioclient_mock) == []
    assert "set by hand" in info(hass, "remote_load")["last_action"]


async def test_remote_leaves_a_looser_human_value_alone_and_lowers_the_rest(
    hass, aioclient_mock, freezer, status
):
    with_override(status, "max_cores", 3.0, "slider")  # a human value above the target stays
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == [
        (
            "/v1/set",
            {"max_implementers": 1, "ttl": "15m", "reason": "HA trigger:remote_load: load or CPU high"},
        )
    ]


async def test_remote_replaces_an_agent_raise(hass, aioclient_mock, freezer, status):
    with_override(status, "max_cores", 4.0, "need more", principal="agent")
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == [
        REMOTE_LIMITS
    ]  # sent as owner, the daemon cancels the looser agent raise


async def test_remote_human_wins_over_an_agent_on_the_same_param(hass, aioclient_mock, freezer, status):
    with_override(status, "max_cores", 4.0, "need more", principal="agent")
    with_override(status, "max_cores", 3.0, "slider")
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == [
        (
            "/v1/set",
            {"max_implementers": 1, "ttl": "15m", "reason": "HA trigger:remote_load: load or CPU high"},
        )
    ]


async def test_remote_after_a_restart_rearms_at_once_when_its_limits_are_in_force(
    hass, aioclient_mock, freezer, status
):
    with_override(status, "max_cores", 2.5, "HA trigger:remote_load: load or CPU high")
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    assert len(posts(aioclient_mock)) == 1  # no 10-minute gap before the TTL runs out


async def test_remote_after_a_restart_without_its_limits_still_waits_10_minutes(
    hass, aioclient_mock, freezer, status
):
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    assert posts(aioclient_mock) == []
    await advance(hass, freezer, 600)
    assert len(posts(aioclient_mock)) == 1


async def test_remote_skips_values_already_at_or_below_the_target(hass, aioclient_mock, freezer, status):
    status["limits"]["effective"].update(max_cores=2.0, max_implementers=1)  # e.g. a ceiling or a profile
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == []


async def test_remote_rearms_its_own_override(hass, aioclient_mock, freezer, status):
    with_override(status, "max_cores", 2.5, "HA trigger:remote_load: load or CPU high")
    with_override(status, "max_implementers", 1, "HA trigger:remote_load: load or CPU high")
    await start(hass, aioclient_mock, status, only("remote_load"), {LOAD: "9.5", CPU: "10"})
    assert posts(aioclient_mock) == [
        REMOTE_LIMITS
    ]  # at the target already, still re-armed before the TTL ends
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == [REMOTE_LIMITS, REMOTE_LIMITS]


# --- failures ---


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        ({"status": 409, "json": {"refused": True, "reason": "owner ceiling"}}, "owner ceiling"),
        ({"status": 503, "json": {"reason": "no sample yet"}}, "no sample yet"),
        ({"exc": aiohttp.ClientError("boom")}, "cannot reach loadgovd"),
    ],
)
async def test_daemon_failures_are_logged_once_and_retried(
    hass, aioclient_mock, freezer, status, caplog, kwargs, text
):
    await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "idle"})
    mock_daemon(aioclient_mock, status, kwargs)
    caplog.set_level(logging.WARNING)
    await change(hass, TV, "playing")
    assert text in info(hass, "tv_quiet")["last_error"]
    await advance(hass, freezer, 10 * 60)
    await advance(hass, freezer, 10 * 60)
    assert len(posts(aioclient_mock)) == 3  # retried on every re-arm
    assert sum("loadgov tv_quiet trigger" in r.message for r in caplog.records) == 1
    mock_daemon(aioclient_mock, status)
    await advance(hass, freezer, 10 * 60)
    assert info(hass, "tv_quiet")["last_error"] is None
    assert "ceiling silent" in info(hass, "tv_quiet")["last_action"]


async def test_status_failure_does_not_raise(hass, aioclient_mock, freezer, status):
    entry = await start(hass, aioclient_mock, status, only("presence"), {ZONE: "0"})
    aioclient_mock._mocks.clear()
    aioclient_mock.get(f"{BASE}/v1/status", status=503, json={"reason": "no sample yet"})
    await advance(hass, freezer, 600)
    assert posts(aioclient_mock) == []
    # the coordinator failed too, so the sensor is unavailable: read the trigger state directly
    assert "status unavailable" in entry.runtime_data.triggers.info()["presence"]["last_error"]


# --- lifecycle ---


async def test_unload_cancels_every_timer_and_subscription(hass, aioclient_mock, freezer, status):
    states = {ZONE: "1", TV: "idle", QUIET: "off", LOAD: "1", CPU: "1"}
    entry = await start(hass, aioclient_mock, status, {**OPTS, **ALL_ON}, states)
    await change(hass, ZONE, "0")
    await change(hass, LOAD, "20")
    await change(hass, TV, "playing")
    n = len(posts(aioclient_mock))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await change(hass, QUIET, "on")
    await change(hass, TV, "idle")
    await advance(hass, freezer, 3600)
    assert len(posts(aioclient_mock)) == n


async def test_nothing_registers_a_timer_after_unload(hass, aioclient_mock, status):
    entry = await start(hass, aioclient_mock, status, only("presence"), {ZONE: "1"})
    triggers = entry.runtime_data.triggers
    assert await hass.config_entries.async_unload(entry.entry_id)
    triggers._every("late", timedelta(minutes=1), lambda: None)
    triggers._later("late2", 60, lambda: None)
    assert triggers._timers == {}


async def test_reload_applies_a_new_entity_without_restart(hass, aioclient_mock, status):
    states = {TV: "idle", "media_player.new_tv": "idle"}
    entry = await start(hass, aioclient_mock, status, only("tv_quiet"), states)
    options = {**only("tv_quiet"), "tv_media_players": ["media_player.new_tv"]}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await change(hass, TV, "playing")  # the old player is no longer watched
    assert posts(aioclient_mock) == []
    await change(hass, "media_player.new_tv", "playing")
    assert len(posts(aioclient_mock)) == 1


async def test_reload_self_heals_a_condition_that_still_holds(hass, aioclient_mock, status):
    entry = await start(hass, aioclient_mock, status, only("tv_quiet"), {TV: "playing"})
    assert len(posts(aioclient_mock)) == 1
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(posts(aioclient_mock)) == 2


async def test_diagnostic_sensor_lists_every_trigger_without_secrets(hass, aioclient_mock, status):
    await start(hass, aioclient_mock, status, only("quiet_hours"), {QUIET: "on"})
    sensor = hass.states.get("sensor.host_triggers")
    assert sensor.state == "1"
    for name in ("presence", "tv_quiet", "quiet_hours", "remote_load"):
        assert set(sensor.attributes[name]) == {"enabled", "active", "last_action", "last_error"}
    assert sensor.attributes["quiet_hours"]["enabled"] is True
    assert "ceiling silent" in sensor.attributes["quiet_hours"]["last_action"]
    assert TOKEN not in str(sensor.attributes)
