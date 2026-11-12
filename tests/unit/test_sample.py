import calendar
import os
from pathlib import Path

import pytest

from loadgov.engines.governor import Governor
from loadgov.engines.levels import level
from loadgov.engines.slots import SlotManager
from loadgov.protocol import validate_status
from loadgov.sample import Sampler, busy_pct, parse_cpu_max, parse_holder, parse_pressure

NOW = float(calendar.timegm((2026, 11, 10, 18, 40, 0)))


def take(host, now=NOW, mirrors=("loop-a",)):
    s = Sampler(host.fs, mirrors)
    s.prime(host.prev_stat)
    out = s.sample(now=now)
    out["daemon"] = {"version": "t", "pid": 1, "started_at": "t", "mode": "observe", "tick_s": 5.0}
    out["limits"] = {  # added by the daemon, like "daemon"
        "profile": "balanced",
        "ceiling": None,
        "restricted": False,
        "restricted_by": None,
        "freeze": None,
        "ceiling_info": None,
        "profile_info": None,
        "overrides": [],
        "effective": {"max_cores": 3.5},
    }
    out["capabilities"] = ["freeze", "reap"]
    out["headroom"] = {"io_would_block_count": 0, "io_would_block_last": None}
    out["memguard"] = {
        "enforce": False, "reaper_enforce": False, "low_mb": 3072, "resume_mb": 6144, "crit_mb": 1500,
        "baseline_swap_mb": 0, "paused": [], "last_decision": None, "last_reason": None, "last_at": None,
        "cooldown_s": 0.0, "foreign_stopped": [],
    }  # fmt: skip
    out["backstop"] = {"enabled": False, "applied": False, "memory_max_mb": None, "memory_swap_max_mb": None,
                       "oom_kills": 0, "last_event": None, "note": None}  # fmt: skip
    out["rest"] = {"enabled": False, "state": "disabled", "error": None, "bind": "192.0.2.10", "port": 7420}
    out["slots"] = SlotManager(host.fs).summary()  # added by the daemon too
    out["jobs"] = []  # likewise
    out["alerts"] = {"active": [], "events": []}
    out["governor"] = Governor(host.fs, lambda: now).status()  # likewise
    out["ha_push"] = {
        "enabled": False,
        "state": "disabled",
        "queued": 0,
        "dropped": 0,
        "failed": 0,
        "last_ok": None,
        "last_error": None,
    }
    return out


def test_busy_fixture_reports_82_pct_and_cpu_busy(fake_host):
    s = take(fake_host("busy"))
    assert s["cpu"]["busy_pct"] == 82.0
    assert s["levels"]["cpu"] == "BUSY"  # 82 % alone is OK; psi60 45 >= 40 makes it BUSY
    assert s["level"] == "BUSY"


@pytest.mark.parametrize("name", ["calm", "busy", "swap-surge", "low-mem"])
def test_every_fixture_matches_the_status_schema(fake_host, name):
    assert validate_status(take(fake_host(name))) == []


def test_calm_is_ok(fake_host):
    s = take(fake_host("calm"))
    assert s["level"] == "OK"
    assert s["heavy_slot"]["held"] is False


def test_low_mem_is_ram_limit_and_reports_stopped_procs_in_governed_cgroups(fake_host):
    s = take(fake_host("low-mem"))
    assert s["memory"]["avail_mb"] == 2900
    assert s["levels"]["ram"] == "LIMIT"
    assert [p["pid"] for p in s["stopped"]] == [201]


def test_swap_surge_reports_used_pct_and_level(fake_host):
    s = take(fake_host("swap-surge"))
    assert s["swap"]["used_mb"] == 7600
    assert s["levels"]["swap"] == "BUSY"  # 92.8 %


def test_top3_jvm_count_and_heavy_slot_from_proc_locks(fake_host):
    s = take(fake_host("busy"))
    assert [p["comm"] for p in s["top"]] == ["rustc", "node", "java"]
    assert s["top"][0]["rss_mb"] == 6800
    assert s["jvm_daemons"] == 1
    hs = s["heavy_slot"]
    assert hs["held"] is True and hs["holder_pid"] == 104 and hs["lock_pids"] == []
    assert hs["holder"].startswith("proj-a gate job-229")
    assert hs["age_s"] == 2400 and hs["stuck"] is False
    assert take(fake_host("busy"), now=NOW + 3600)["heavy_slot"]["stuck"] is True


def test_legacy_lock_with_dead_flock_helper_is_held_via_holder_fd(fake_host):
    """Real kernel behaviour: `flock -n 9` from bash leaves no /proc/locks entry once the helper exits."""
    host = fake_host("busy")
    assert "FLOCK" not in Path(f"{host.proc}/locks").read_text()
    hs = take(host)["heavy_slot"]
    assert hs["held"] is True and hs["holder_pid"] == 104


def test_waiter_with_lock_open_but_not_named_does_not_make_the_slot_held(fake_host):
    host = fake_host("busy")
    with open(f"{host.lock}/host-heavy-slot.holder", "w") as f:  # holder 104 gone, line names a dead pid
        f.write("proj-a gate job-228 1 pid=4242 since=2026-11-10T17:00:00Z\n")
    hs = take(host)["heavy_slot"]
    assert hs["held"] is False and hs["holder_pid"] is None
    assert hs["last_holder"].startswith("proj-a gate job-228")


def test_reused_holder_pid_without_the_lock_open_is_not_a_holder(fake_host):
    host = fake_host("busy")
    with open(f"{host.lock}/host-heavy-slot.holder", "w") as f:  # pid 102 is alive (node) but has no lock fd
        f.write("proj-a gate job-228 1 pid=102 since=2026-11-10T17:00:00Z\n")
    assert take(host)["heavy_slot"]["held"] is False


def test_live_kernel_locker_is_held_even_without_a_holder_line(fake_host):
    hs = take(fake_host("calm", flock_pid=777))["heavy_slot"]
    assert hs["held"] is True and hs["lock_pids"] == [777]  # blocked waiter 999 ignored
    assert hs["holder"] is None and hs["holder_pid"] is None


def test_holder_file_without_flock_is_reported_as_last_holder_only(fake_host):
    host = fake_host("calm")
    with open(f"{host.lock}/host-heavy-slot.holder", "w") as f:
        f.write("proj-a gate job-230 1 pid=1914626 since=2026-11-10T20:12:50Z\n")
    hs = take(host)["heavy_slot"]
    assert hs["held"] is False and hs["holder"] is None
    assert hs["last_holder"].startswith("proj-a gate job-230")


def test_cgroup_caps(fake_host):
    s = take(fake_host("calm"))
    assert s["cgroups"]["loop-a"] == {"cpu_max": "150000 100000", "cores": 1.5}
    assert s["cgroups"]["build-work.slice"] is None


def test_no_mirror_configured_watches_only_the_build_work_slice(fake_host):
    s = take(fake_host("calm"), mirrors=())
    assert list(s["cgroups"]) == ["build-work.slice"]


def test_build_work_slice_is_read_at_its_nested_systemd_path(fake_host):
    host = fake_host("calm")
    nested = os.path.join(host.cgroup, "build.slice", "build-work.slice")
    os.makedirs(nested)
    with open(os.path.join(nested, "cpu.max"), "w") as f:
        f.write("350000 100000\n")
    assert take(host)["cgroups"]["build-work.slice"] == {"cpu_max": "350000 100000", "cores": 3.5}


def test_stopped_processes_under_the_nested_slice_are_listed(fake_host):
    from conftest import FakeProc

    procs = [
        FakeProc(301, "cargo", "T", cgroup="/build.slice/build-work.slice/build-work-proj-a.slice/u.scope"),
        FakeProc(302, "cargo", "T", cgroup="/build-work.slice/old.scope"),  # not a real systemd path
    ]
    assert [p["pid"] for p in take(fake_host("calm", procs=procs))["stopped"]] == [301]
    assert parse_cpu_max("max 100000")["cores"] is None


def test_parsers():
    assert busy_pct((100, 50), (100, 50)) == 0.0
    assert parse_pressure("some avg10=1.5 avg60=2.0 avg300=0 total=9")["some_avg60"] == 2.0
    assert parse_holder("proj-b gate job-51 1 pid=42 since=2026-10-25T10:00:00Z")["who"] == (
        "proj-b gate job-51 1"
    )
    assert parse_holder("garbage") is None


@pytest.mark.parametrize(
    ("value", "busy", "limit", "expected"),
    [
        (84, 85, 97, "OK"),
        (85, 85, 97, "BUSY"),
        (97, 85, 97, "LIMIT"),
        (6144, 6144, 3584, "BUSY"),
        (3584, 6144, 3584, "LIMIT"),
    ],
)
def test_level_matches_hwmon(value, busy, limit, expected):
    assert level(value, busy, limit) == expected
