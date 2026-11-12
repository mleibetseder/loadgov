import json
import time
from dataclasses import replace
from datetime import datetime

import pytest

from loadgov.config import BUILTIN_PROFILES, RESTRICTIVE, ConfigError, loosens, parse_config, tighter
from loadgov.daemon.state import StateStore
from loadgov.engines.limits import AGENT_TTL_DEFAULT, Limits, LimitState
from loadgov.engines.timers import parse_ttl, parse_until


class Clock:
    def __init__(self, t=1_762_800_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(profile="balanced"):
    clock = Clock()
    return Limits(dict(BUILTIN_PROFILES), LimitState(profile), clock), clock


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def test_agent_raise_refused_under_owner_ceiling():
    lim, _ = make()
    lim.ceiling_set("silent", "TV playing")
    before = lim.effective()
    r = lim.set("agent", {"max_cores": 4.0}, "need more")
    assert (r["ok"], r["refused"], r["restrictedBy"], r["reason"]) == (
        False,
        True,
        "owner",
        "TV playing",
    )
    assert lim.effective() == before and before["max_cores"] == 2.0
    assert lim.drain_events()[-1]["result"] == "refused"


def test_agent_raise_without_restriction_is_accepted_with_ttl_and_resets():
    lim, clock = make()
    r = lim.set("agent", {"max_cores": 4.0}, "build")
    assert r["ok"] and r["ttl_s"] == AGENT_TTL_DEFAULT == 7200 and not r["ttl_capped"]
    assert lim.effective()["max_cores"] == 4.0
    clock.t += 7199
    assert not lim.tick() and lim.effective()["max_cores"] == 4.0
    clock.t += 2
    assert lim.tick() and lim.effective()["max_cores"] == 3.5
    assert lim.drain_events()[-1]["result"] == "expired"


def test_agent_ttl_is_capped_at_12h():
    lim, _ = make()
    r = lim.set("agent", {"max_cores": 4.0}, "x", ttl_s=48 * 3600)
    assert r["ttl_s"] == 12 * 3600 and r["ttl_capped"]


def test_owner_ceiling_cancels_running_agent_raise():
    lim, _ = make()
    lim.set("agent", {"max_cores": 4.0}, "build")
    assert lim.effective()["max_cores"] == 4.0
    lim.drain_events()
    lim.ceiling_set("silent", "movie")
    assert lim.effective()["max_cores"] == 2.0 and lim.state.overrides == []
    assert [e["result"] for e in lim.drain_events()] == ["cancelled", "accepted"]


def test_agent_lower_under_ceiling_accepted_and_expires():
    lim, clock = make()
    lim.ceiling_set("silent", "movie")
    r = lim.set("agent", {"max_cores": 1.5}, "quieter")
    assert r["ok"] and lim.effective()["max_cores"] == 1.5
    clock.t += AGENT_TTL_DEFAULT
    lim.tick()
    assert lim.effective()["max_cores"] == 2.0  # back to the ceiling, not above it


def test_owner_override_below_profile_is_a_restriction():
    lim, _ = make()
    assert lim.restriction() is None
    lim.set("owner", {"load_max": 2.0}, "fan too loud")
    assert lim.restriction()["reason"] == "fan too loud"
    r = lim.set("agent", {"max_cores": 4.0}, "build")
    assert r["refused"] and r["restrictedBy"] == "owner"


def test_owner_override_at_or_above_profile_is_no_restriction():
    lim, _ = make()
    lim.set("owner", {"max_cores": 4.0}, "more")
    assert lim.restriction() is None


def test_freeze_and_hold_flags_restrict():
    lim, _ = make()
    lim.set_flag("freeze", "phone button")
    assert lim.set("agent", {"max_cores": 4.0}, "x")["refused"]
    lim.set_flag("freeze", None)
    lim.set_flag("hold", "night mode")
    assert lim.set("agent", {"max_cores": 4.0}, "x")["reason"] == "night mode"


def test_ttl_1s_expires_and_restores_previous_value():
    lim, clock = make()
    lim.set("owner", {"max_cores": 2.5}, "short", ttl_s=parse_ttl("1s"))
    assert lim.effective()["max_cores"] == 2.5
    clock.t += 1
    assert lim.tick()
    assert lim.effective()["max_cores"] == 3.5


def test_owner_profile_with_ttl_restores_previous_profile():
    lim, clock = make()
    lim.set("owner", {}, "tonight", ttl_s=60, profile="silent")
    assert lim.state.profile == "silent"
    clock.t += 60
    lim.tick()
    assert lim.state.profile == "balanced"


def test_agent_profile_silent_is_a_lower_and_boost_a_raise():
    lim, _ = make()
    assert lim.set("agent", {}, "quiet", profile="silent")["effective"]["max_cores"] == 2.0
    lim.ceiling_set("silent", "c")
    assert lim.set("agent", {}, "loud", profile="boost")["refused"]


def test_load_hold_stays_5_under_silent():
    lim, _ = make("silent")
    e = lim.effective()
    assert e["load_max"] == 2.0 and e["load_hold"] == 5.0


def test_state_round_trip_drops_expired(tmp_path):
    lim, clock = make()
    lim.ceiling_set("silent", "movie")
    lim.set("owner", {"max_cores": 1.5}, "perm")
    lim.set("agent", {"load_max": 1.0}, "short", ttl_s=10)
    store = StateStore(str(tmp_path))
    store.save(lim.state)
    assert not (tmp_path / "state.json.tmp").exists()
    clock.t += 60  # the daemon was down for a minute
    again = Limits(dict(BUILTIN_PROFILES), store.load("balanced", set(BUILTIN_PROFILES)), clock)
    assert again.state.ceiling.profile == "silent"
    assert len(again.state.overrides) == 2
    assert again.tick()
    assert [o.param for o in again.state.overrides] == ["max_cores"]
    assert again.effective()["max_cores"] == 1.5 and again.effective()["load_max"] == 2.0


def test_state_unreadable_or_unknown_profile_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text("{nope")
    assert StateStore(str(tmp_path)).load("balanced", {"balanced"}).profile == "balanced"
    (tmp_path / "state.json").write_text(json.dumps({"profile": "gone"}))
    assert StateStore(str(tmp_path)).load("balanced", {"balanced"}).profile == "balanced"


def test_until_resolves_next_0700_berlin():
    # CEST (UTC+2): before 07:00 local -> same day, after -> tomorrow
    assert parse_until("07:00 Europe/Berlin", ts("2026-06-10T03:00:00+00:00")) == ts(
        "2026-06-10T05:00:00+00:00"
    )
    assert parse_until("07:00 Europe/Berlin", ts("2026-06-10T08:00:00+00:00")) == ts(
        "2026-06-11T05:00:00+00:00"
    )


def test_until_across_dst_uses_the_offset_of_the_target_day():
    # DST ends 2026-10-25 in Berlin: 07:00 that day is CET (UTC+1) = 06:00 UTC, not 05:00
    assert parse_until("07:00 Europe/Berlin", ts("2026-10-24T12:00:00+00:00")) == ts(
        "2026-10-25T06:00:00+00:00"
    )
    # DST starts 2026-03-29: 07:00 is CEST = 05:00 UTC
    assert parse_until("07:00 Europe/Berlin", ts("2026-03-28T12:00:00+00:00")) == ts(
        "2026-03-29T05:00:00+00:00"
    )


@pytest.mark.parametrize("bad", ["", "5", "1d", "0s", "-5m", "2 hours"])
def test_ttl_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_ttl(bad)


def test_ttl_units():
    assert (parse_ttl("90s"), parse_ttl("15m"), parse_ttl("2h")) == (90, 900, 7200)


@pytest.mark.parametrize("bad", ["25:00", "07:00 Mars/Base", "soon"])
def test_until_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_until(bad, 0)


def test_config_defaults_and_overrides():
    c = parse_config("[profiles.silent]\nload_max = 2.5\nmax_implementers = 0\n")
    assert c.profiles["silent"].load_max == 2.5 and c.profiles["silent"].impl_max_quiet == 0
    assert c.profiles["silent"].load_hold == 5.0 and c.profiles["boost"].max_cores == 5.5
    assert c.profiles["drain"].hold_heavy_slot is True and c.default_profile == "balanced"


@pytest.mark.parametrize(
    "bad",
    [
        "[profiles.balanced]\nram_floor_gb = 7.0",
        "[profiles.balanced]\nmax_cores = 1.2",
        "[profiles.balanced]\ntarget_cpu_pct = 95",
        "[profiles.balanced]\nmax_implementers = 5",
        "[profiles.balanced]\nbogus = 1",
        "[profiles.nope]\nmax_cores = 2",
        'default_profile = "x"',
    ],
)
def test_config_validation_rejects(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_agent_ram_floor_lowering_is_a_loosening_refused_under_ceiling():
    lim, _ = make()
    lim.ceiling_set("silent", "movie")
    r = lim.set("agent", {"ram_floor_gb": 2.0}, "x")
    assert r["refused"] and r["restrictedBy"] == "owner"
    assert lim.effective()["ram_floor_gb"] == 3.0


def test_agent_ram_floor_raising_is_a_tightening_accepted_with_ttl():
    lim, clock = make()
    lim.ceiling_set("silent", "movie")
    r = lim.set("agent", {"ram_floor_gb": 5.0}, "careful")
    assert r["ok"] and r["ttl_s"] == AGENT_TTL_DEFAULT and lim.effective()["ram_floor_gb"] == 5.0
    clock.t += AGENT_TTL_DEFAULT
    lim.tick()
    assert lim.effective()["ram_floor_gb"] == 3.0


def test_owner_stricter_ram_floor_is_a_restriction():
    lim, _ = make()
    lim.set("owner", {"ram_floor_gb": 5.0}, "keep memory free")
    assert lim.restriction()["reason"] == "keep memory free"
    assert lim.set("agent", {"max_cores": 4.0}, "x")["refused"]


def test_owner_looser_ram_floor_is_no_restriction():
    lim, _ = make()
    lim.set("owner", {"ram_floor_gb": 2.0}, "tight on ram")
    assert lim.restriction() is None


def test_ceiling_never_lowers_the_ram_floor():
    profiles = dict(BUILTIN_PROFILES)
    profiles["balanced"] = replace(profiles["balanced"], ram_floor_gb=5.0)
    lim = Limits(profiles, LimitState("balanced"), Clock())
    lim.ceiling_set("silent", "movie")  # silent's floor is 3.0
    assert lim.effective()["ram_floor_gb"] == 5.0
    assert lim.effective()["max_cores"] == 2.0


def test_ceiling_cancels_agent_ram_floor_loosening():
    lim, _ = make()
    lim.set("agent", {"ram_floor_gb": 2.0}, "x")
    assert lim.effective()["ram_floor_gb"] == 2.0
    lim.ceiling_set("silent", "movie")
    assert lim.effective()["ram_floor_gb"] == 3.0


@pytest.mark.parametrize(
    ("param", "tight", "loose"),
    [
        ("max_cores", 2.0, 5.0),
        ("target_cpu_pct", 50, 85),
        ("load_max", 2.0, 5.0),
        ("max_implementers", 1, 3),
        ("ram_floor_gb", 5.0, 2.0),
    ],
)
def test_restrictive_direction_per_parameter(param, tight, loose):
    mid = getattr(BUILTIN_PROFILES["balanced"], param)
    assert tighter(param, tight, mid) and not tighter(param, loose, mid)
    assert loosens(param, loose, mid) and not loosens(param, tight, mid)
    lim, _ = make()
    lim.ceiling_set("silent", "c")
    ok = lim.set("agent", {param: tight}, "x")
    assert ok["ok"]
    assert lim.set("agent", {param: loose}, "x").get("refused") is True
    assert {"cargo_build_jobs", "vitest_max_workers", "gate_shard_budget"} <= set(RESTRICTIVE)
    assert "load_hold" not in RESTRICTIVE


A5 = ("agent", {"max_cores": 5.0})
SCENARIOS = {
    "a_owner_profile_silent_cancels_agent_5": (
        [A5, ("owner", {"profile": "silent"})],
        2.0,
        ("agent", {}, "boost"),
    ),
    "b_owner_4_is_a_restriction_when_agent_raised": (
        [A5, ("owner", {"max_cores": 4.0})],
        4.0,
        ("agent", {"max_cores": 5.5}, None),
    ),
    "c_owner_boost_then_balanced_then_load_max": (
        [
            ("owner", {"profile": "boost"}),
            A5,
            ("owner", {"profile": "balanced"}),
            ("owner", {"load_max": 2.0}),
        ],
        3.5,
        ("agent", {"max_cores": 5.0}, None),
    ),
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_owner_tightening_cancels_agent_loosenings_from_current_state(name):
    steps, final_cores, probe = SCENARIOS[name]
    lim, _ = make()
    for principal, kw in steps:
        kw = dict(kw)
        profile = kw.pop("profile", None)
        r = lim.set(principal, kw, "why", profile=profile)
        assert r["ok"], (name, r)
    assert lim.effective()["max_cores"] == final_cores
    assert lim.restriction() is not None
    principal, kw, profile = probe
    r = lim.set(principal, kw, "again", profile=profile)
    assert r.get("refused") is True
    assert lim.effective()["max_cores"] == final_cores


def test_expiry_of_an_owner_restriction_does_not_restore_a_cancelled_agent_loosening():
    lim, clock = make()
    lim.set(*A5, "build")
    lim.set("owner", {"max_cores": 4.0}, "quiet", ttl_s=60)
    clock.t += 61
    assert lim.tick()
    assert lim.effective()["max_cores"] == 3.5 and lim.restriction() is None


def test_temporary_owner_profile_keeps_an_earlier_profile_restriction():
    lim, clock = make()
    lim.set("owner", {}, "quiet", profile="silent")
    lim.set("owner", {}, "away", ttl_s=60, profile="boost")
    assert lim.restriction() is None  # boost is looser than silent
    clock.t += 61
    lim.tick()
    assert lim.state.profile == "silent" and lim.restriction() is not None


def test_boost_to_balanced_cancels_agent_above_baseline_but_is_no_restriction():
    lim, _ = make()
    lim.set("owner", {}, "away", profile="boost")
    assert lim.set(*A5, "build")["ok"]
    lim.drain_events()
    lim.set("owner", {}, "home", profile="balanced")
    assert [e["result"] for e in lim.drain_events()] == ["cancelled", "accepted"]
    assert lim.effective()["max_cores"] == 3.5 and lim.restriction() is None
    r = lim.set("agent", {"max_cores": 4.0}, "build")
    assert r["ok"] and r["ttl_s"] == AGENT_TTL_DEFAULT and lim.effective()["max_cores"] == 4.0


def test_balanced_to_silent_is_a_restriction_and_silent_to_balanced_clears_it():
    lim, _ = make()
    lim.set("owner", {}, "quiet", profile="silent")
    assert lim.restriction() is not None
    assert lim.set("agent", {"max_cores": 3.0}, "x")["refused"] is True
    lim.set("owner", {}, "normal", profile="balanced")
    assert lim.restriction() is None
    assert lim.set("agent", {"max_cores": 3.0}, "x")["ok"]


def test_silent_profile_to_boost_clears_the_restriction():
    lim, _ = make()
    lim.set("owner", {}, "quiet", profile="silent")
    lim.set("owner", {}, "away", profile="boost")
    assert lim.restriction() is None


def test_temporary_silent_expiring_back_to_balanced_clears_the_restriction():
    lim, clock = make()
    lim.set("owner", {}, "tonight", ttl_s=60, profile="silent")
    assert lim.restriction() is not None
    clock.t += 61
    lim.tick()
    assert lim.state.profile == "balanced" and lim.restriction() is None
    assert lim.set("agent", {"max_cores": 3.5 + 0.5}, "x")["ok"]


@pytest.mark.parametrize(
    ("param", "value", "silent_value"),
    [("target_cpu_pct", 90, 50), ("max_cores", 6.0, 2.0), ("load_max", 6.0, 2.0)],
)
def test_profile_expiry_cancels_agent_loosenings_above_the_restored_baseline(param, value, silent_value):
    lim, clock = make()
    lim.set("owner", {}, "quiet", profile="silent")
    lim.set("owner", {}, "away", ttl_s=3600, profile="boost")
    assert lim.restriction() is None
    assert lim.set("agent", {param: value}, "build")["ok"]
    lim.drain_events()
    clock.t += 3601
    assert lim.tick()
    assert lim.restriction() is not None
    assert lim.effective()[param] == silent_value
    assert [e["result"] for e in lim.drain_events()] == ["expired", "cancelled"]


def test_reset_removes_owner_overrides_and_ends_the_restriction():
    lim, _ = make()
    lim.set("owner", {"max_cores": 4.0, "load_max": 2.0}, "quiet")
    lim.set("owner", {"max_cores": 2.5}, "quieter")
    assert lim.set("agent", {"max_cores": 3.0}, "x")["refused"]
    r = lim.reset("done", ["max_cores"])
    assert r["ok"] and len(r["removed"]) == 1
    assert lim.restriction() is not None  # load_max 2.0 still restricts
    lim.reset("done", ["load_max"])
    assert lim.restriction() is None
    a = lim.set("agent", {"max_cores": 4.0}, "build")
    assert a["ok"] and a["ttl_s"] == AGENT_TTL_DEFAULT


def test_reset_all_also_drops_agent_overrides_and_validates():
    lim, _ = make()
    lim.set("agent", {"max_cores": 3.0}, "build")
    lim.set("owner", {"load_max": 2.0}, "quiet")
    assert len(lim.reset("clean", all_=True)["removed"]) == 2
    assert lim.effective() == make()[0].effective()
    for bad in ([], ["bogus"]):
        with pytest.raises(ValueError):
            lim.reset("x", bad)


def test_example_config_equals_the_builtin_defaults():
    from pathlib import Path

    from loadgov.config import Config, load_config

    example = Path(__file__).resolve().parents[2] / "packaging" / "config" / "loadgov.toml.example"
    assert load_config(str(example)) == Config()
    assert load_config("/nonexistent/loadgov.toml") == Config()


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-24T22:00:00+02:00", "2026-10-25T07:00:00+01:00"),  # evening before DST ends
        ("2026-03-28T22:00:00+01:00", "2026-03-29T07:00:00+02:00"),  # evening before DST starts
        ("2026-06-10T08:00:00+02:00", "2026-06-11T07:00:00+02:00"),
    ],
)
def test_zoneless_until_uses_the_host_zone_rules(monkeypatch, now, expected):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    try:
        assert parse_until("07:00", ts(now)) == ts(expected)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_snapshot_exposes_ceiling_and_profile_metadata():
    lim, clock = make()
    snap = lim.snapshot()
    assert snap["ceiling"] is None and snap["profile_info"] is None
    t0 = clock.t
    lim.ceiling_set("silent", "HA trigger:tv: playing", ttl_s=900)
    lim.set("owner", {}, "HA trigger:presence: nobody home", ttl_s=1800, profile="boost")
    snap = lim.snapshot()
    iso = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0 + dt))  # noqa: E731
    assert snap["ceiling"] == {
        "profile": "silent",
        "reason": "HA trigger:tv: playing",
        "principal": "owner",
        "set_at": iso(0),
        "expires_at": iso(900),
    }
    assert snap["profile_info"] == {
        "profile": "boost",
        "reason": "HA trigger:presence: nobody home",
        "principal": "owner",
        "set_at": iso(0),
        "expires_at": iso(1800),
    }
    clock.t += 1801  # the temporary profile expires: the restored one has no recorded reason
    lim.tick()
    snap = lim.snapshot()
    assert snap["profile"] == "balanced" and snap["profile_info"] is None and snap["ceiling"] is None


def test_profile_metadata_survives_a_restart(tmp_path):
    lim, _ = make()
    lim.set("owner", {}, "manual", profile="silent")
    store = StateStore(str(tmp_path))
    store.save(lim.state)
    again = store.load("balanced", set(BUILTIN_PROFILES))
    assert (again.profile_reason, again.profile_principal) == ("manual", "owner")
