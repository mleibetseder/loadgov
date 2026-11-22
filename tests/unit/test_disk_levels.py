"""Configurable disk levels: GB free or percent used, per watched path, worst path wins (fake statvfs)."""

import dataclasses
import os

import pytest

from loadgov.cli import loadctl
from loadgov.config import ConfigError, DiskThreshold, parse_config
from loadgov.engines.levels import DISK_BUSY, DISK_LIMIT, disk_check, evaluate
from loadgov.engines.watch import Watch
from loadgov.sample import Sampler

GB = 2**30
BLOCK = 4096
PCT = DiskThreshold
T0 = 1_762_800_000.0


def vfs(total_gb, free_gb):
    """A statvfs result for a filesystem of `total_gb` with `free_gb` free (no reserved blocks)."""
    blocks, free = int(total_gb * GB / BLOCK), int(free_gb * GB / BLOCK)
    return os.statvfs_result((BLOCK, BLOCK, blocks, free, free, 0, 0, 0, 0, 255))


def take(fake_host, sizes, busy=DISK_BUSY, limit=DISK_LIMIT):
    """Sample a calm host whose watched paths have the given (total_gb, free_gb)."""
    host = fake_host("calm")
    fs = dataclasses.replace(host.fs, disks=tuple(sizes), statvfs=lambda p: vfs(*sizes[p]))
    s = Sampler(fs)
    s.disk_busy, s.disk_limit = busy, limit
    s.prime(host.prev_stat)
    return s.sample(now=T0)


def cfg(body):
    return parse_config(f"[levels]\n{body}\n").levels


# ---- config ----------------------------------------------------------------------------------------


def test_defaults_are_todays_gb_thresholds():
    lv = parse_config("").levels
    assert (lv.disk_busy, lv.disk_limit) == (DiskThreshold(25.0), DiskThreshold(10.0))
    assert (lv.disk_busy, lv.disk_limit) == (DISK_BUSY, DISK_LIMIT)


def test_numbers_are_gb_free_and_strings_are_percent_used():
    lv = cfg('disk_busy = "80%"\ndisk_limit = 10')
    assert lv.disk_busy == DiskThreshold(80.0, True) and lv.disk_limit == DiskThreshold(10.0)
    assert cfg('disk_busy = " 82.5 % "').disk_busy == DiskThreshold(82.5, True)
    assert cfg("disk_busy = 30.5").disk_busy == DiskThreshold(30.5)


@pytest.mark.parametrize(
    "body",
    [
        'disk_busy = "0%"', 'disk_busy = "100%"', 'disk_busy = "120%"', 'disk_busy = "-5%"',
        'disk_busy = "80"', 'disk_busy = "abc"', "disk_busy = 0", "disk_busy = -3", "disk_busy = true",
        "disk_limit = [1]", "disk_unknown = 1",
        "disk_busy = 10\ndisk_limit = 10",  # not earlier
        "disk_busy = 5\ndisk_limit = 10",  # GB: busy needs more free space than limit
        'disk_busy = "95%"\ndisk_limit = "90%"',  # %: busy needs the lower percent
        'disk_busy = "90%"\ndisk_limit = "90%"',
    ],
)  # fmt: skip
def test_bad_levels_are_refused_at_config_load(body):
    with pytest.raises(ConfigError, match="levels"):
        parse_config(f"[levels]\n{body}\n")


def test_levels_table_must_be_a_table():
    with pytest.raises(ConfigError, match="levels"):
        parse_config("levels = 3\n")


def test_mixed_units_are_allowed_each_on_its_own():
    assert cfg('disk_busy = "50%"').disk_limit == DiskThreshold(10.0)
    assert cfg('disk_busy = 20\ndisk_limit = "97%"').disk_limit.pct


# ---- evaluation ------------------------------------------------------------------------------------


def test_gb_thresholds_keep_todays_boundaries():
    def d(free):
        return {"per_path": [{"path": "/", "free_gb": free, "total_gb": 100.0, "used_pct": 50.0}]}

    assert [disk_check(d(f))[0] for f in (26, 25, 11, 10, 9.9)] == ["OK", "BUSY", "BUSY", "LIMIT", "LIMIT"]
    # the alert uses "below": exactly at the threshold is still fine
    strict = [disk_check(d(f), inclusive=False)[0] for f in (25, 24.9, 10, 9.9)]
    assert strict == ["OK", "BUSY", "BUSY", "LIMIT"]


def test_percent_thresholds(fake_host):
    busy, limit = PCT(80.0, True), PCT(95.0, True)
    for total, free, want in ((100, 21, "OK"), (100, 20, "BUSY"), (100, 6, "BUSY"), (100, 5, "LIMIT")):
        s = take(fake_host, {"/": (total, free)}, busy, limit)
        assert s["levels"]["disk_alert"] == want, (free, s["disk"])
    s = take(fake_host, {"/": (100, 19)}, busy, limit)
    assert s["disk"]["used_pct"] == 81.0 and s["disk"]["free_gb"] == 19.0 and s["disk"]["total_gb"] == 100.0


def test_percent_busy_with_gb_limit_is_mixed(fake_host):
    busy, limit = PCT(80.0, True), PCT(10.0)
    # 1000 GB disk, 150 GB free: 85 % used (BUSY by percent) though far above 10 GB free
    assert take(fake_host, {"/": (1000, 150)}, busy, limit)["levels"]["disk_alert"] == "BUSY"
    assert take(fake_host, {"/": (1000, 9)}, busy, limit)["levels"]["disk_alert"] == "LIMIT"
    assert take(fake_host, {"/": (1000, 300)}, busy, limit)["levels"]["disk_alert"] == "OK"


def test_worst_path_wins_and_free_gb_stays_the_lowest_free(fake_host):
    busy, limit = PCT(80.0, True), PCT(95.0, True)
    sizes = {"/": (100, 50), "/data": (4000, 600), "/small": (20, 15)}
    s = take(fake_host, sizes, busy, limit)
    # /data is 85 % used; the lowest-free path (/small) is only 25 % used
    assert s["levels"]["disk_alert"] == "BUSY"
    assert s["disk"]["free_gb"] == 15.0 and s["disk"]["total_gb"] == 20.0
    assert s["disk"]["used_pct"] == 85.0  # of the worst path
    assert [p["path"] for p in s["disk"]["per_path"]] == ["/", "/data", "/small"]
    data = {"path": "/data", "free_gb": 600.0, "total_gb": 4000.0, "used_pct": 85.0}
    assert s["disk"]["per_path"][1] == data
    worse = take(fake_host, {**sizes, "/full": (100, 2)}, busy, limit)
    assert worse["levels"]["disk_alert"] == "LIMIT" and worse["disk"]["used_pct"] == 98.0


def test_no_threshold_crossed_reports_the_least_free_path(fake_host):
    s = take(fake_host, {"/": (100, 60), "/b": (100, 40)})
    assert (
        s["levels"]["disk_alert"] == "OK" and s["disk"]["free_gb"] == 40.0 and s["disk"]["used_pct"] == 60.0
    )


def test_a_zero_size_filesystem_never_trips_a_percent_threshold(fake_host):
    s = take(fake_host, {"/": (0, 0)}, PCT(80.0, True), PCT(95.0, True))
    assert s["levels"]["disk_alert"] == "OK" and s["disk"]["used_pct"] == 0.0


def test_evaluate_defaults_equal_the_old_function_of_free_gb():
    base = {
        "cpu": {"busy_pct": 0, "psi_effective": 0, "load1_per_core": 0},
        "memory": {"avail_mb": 20000}, "swap": {"used_pct": 0},
    }  # fmt: skip
    for free, want in ((48, "OK"), (20, "BUSY"), (9, "LIMIT")):
        assert evaluate({**base, "disk": {"free_gb": free}})[0]["disk"] == want


# ---- alerts ----------------------------------------------------------------------------------------


def alert_for(sample, busy, limit):
    w = Watch(lambda: T0)
    w.disk_busy, w.disk_limit = busy, limit
    s = {**sample, "slots": {}, "heavy_slot": {"stuck": False}}
    out = w.evaluate(s, 10, 65, "balanced", None, [])
    return [e for e in out if e["key"] == "disk"]


def test_alert_names_the_percent_threshold(fake_host):
    busy, limit = PCT(80.0, True), PCT(97.0, True)
    (e,) = alert_for(take(fake_host, {"/": (100, 19)}, busy, limit), busy, limit)
    assert e["level"] == "warning" and e["message"] == "disk / 81 % used >= 80 %"
    (e,) = alert_for(take(fake_host, {"/": (100, 2)}, busy, limit), busy, limit)
    assert e["level"] == "critical" and e["message"] == "disk / 98 % used >= 97 %"


def test_alert_names_the_gb_threshold(fake_host):
    (e,) = alert_for(take(fake_host, {"/": (100, 9.5)}), DISK_BUSY, DISK_LIMIT)
    assert e["level"] == "critical" and e["message"] == "disk / 9.5 GB free < 10 GB"
    (e,) = alert_for(take(fake_host, {"/": (100, 20)}), DISK_BUSY, DISK_LIMIT)
    assert e["level"] == "warning" and e["message"] == "disk / 20 GB free < 25 GB"
    assert alert_for(take(fake_host, {"/": (100, 25)}), DISK_BUSY, DISK_LIMIT) == []


def test_alert_names_the_worst_path_with_mixed_units(fake_host):
    busy, limit = PCT(80.0, True), PCT(10.0)
    s = take(fake_host, {"/": (100, 50), "/data": (1000, 150)}, busy, limit)
    (e,) = alert_for(s, busy, limit)
    assert e["level"] == "warning" and e["message"] == "disk /data 85 % used >= 80 %"


# ---- rendering (reload: test_reload.py; protocol fields: test_protocol_doc.py) -----------------


def test_status_line_shows_used_percent_next_to_free_gb(fake_host):
    s = take(fake_host, {"/": (100, 19)}, PCT(80.0, True), PCT(95.0, True))
    s["daemon"] = {
        "version": "t", "pid": 1, "started_at": "t", "mode": "observe", "tick_s": 5.0,
        "enforce": {"governor": False, "slots": False, "memguard": False, "reaper": False},
        "memory_protection_mb": 96,
    }  # fmt: skip
    assert loadctl.format_status(s).splitlines()[4] == "  disk  BUSY   19 GB free, 81 % used on /"
    del s["disk"]["used_pct"]  # a status from an older daemon
    assert loadctl.format_status(s).splitlines()[4] == "  disk  BUSY   19 GB free on /"


# ---- the configured thresholds never reach the overall level --------------------------------------


def test_configured_thresholds_leave_level_and_levels_disk_on_the_fixed_gb_rule(fake_host):
    busy, limit = PCT(80.0, True), PCT(90.0, True)
    s = take(fake_host, {"/": (1000, 50)}, busy, limit)  # 95 % used, 50 GB free
    assert s["levels"]["disk_alert"] == "LIMIT"
    assert s["levels"]["disk"] == "OK" and s["level"] == "OK"
    s = take(fake_host, {"/": (1000, 20)}, busy, limit)  # 20 GB free: BUSY by the fixed rule
    assert s["levels"]["disk"] == "BUSY" and s["level"] == "BUSY"
    s = take(fake_host, {"/": (1000, 5)}, busy, limit)
    assert s["levels"]["disk"] == "LIMIT" and s["level"] == "LIMIT"


def test_hwmon_five_lines_ignore_the_configured_thresholds_and_stay_as_before(fake_host):
    s = take(fake_host, {"/": (100, 19)}, PCT(80.0, True), PCT(95.0, True))  # BUSY by the fixed rule too
    assert loadctl.format_hwmon(s).splitlines()[4] == "  disk  BUSY   19 GB free on /"
    s = take(fake_host, {"/": (1000, 150)}, PCT(80.0, True), PCT(95.0, True))  # 85 % used, plenty free
    assert loadctl.format_hwmon(s).splitlines()[0].endswith("level OK")  # what `hwmon gate` reads


def test_defaults_make_disk_alert_equal_disk(fake_host):
    for free in (60, 25, 24, 10, 9):
        s = take(fake_host, {"/": (100, free)})
        assert s["levels"]["disk_alert"] == s["levels"]["disk"]


# ---- one unreadable path --------------------------------------------------------------------------


def test_an_unreadable_path_is_skipped_and_logged_once(fake_host, caplog):
    host = fake_host("calm")

    def statvfs(p):
        if p == "/gone":
            raise FileNotFoundError(p)
        return vfs(100, 40)

    fs = dataclasses.replace(host.fs, disks=("/", "/gone"), statvfs=statvfs)
    s = Sampler(fs)
    s.prime(host.prev_stat)
    first = s.sample(now=T0)
    s.sample(now=T0 + 5)
    assert first["disk"]["paths"] == ["/", "/gone"]  # the watched list stays as configured
    assert [p["path"] for p in first["disk"]["per_path"]] == ["/"] and first["disk"]["free_gb"] == 40.0
    assert len([r for r in caplog.records if "/gone" in r.getMessage()]) == 1


def test_all_paths_unreadable_still_fails_the_sample(fake_host):
    host = fake_host("calm")
    fs = dataclasses.replace(host.fs, statvfs=lambda p: (_ for _ in ()).throw(OSError("boom")))
    s = Sampler(fs)
    s.prime(host.prev_stat)
    with pytest.raises(OSError, match="no watched disk path"):
        s.sample(now=T0)
