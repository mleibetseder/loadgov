"""CPU pressure from throttling does not count while the CPU is idle (`cpu.psi_effective`)."""

import pytest
from conftest import parity_config

from loadgov.cli.loadctl import format_status
from loadgov.config import ConfigError, parse_config, thresholds
from loadgov.engines.headroom import decide
from loadgov.engines.levels import evaluate
from loadgov.sample import Sampler

THROTTLED = (62.0, 53.0)  # a throttled host: high PSI while most cores are idle


def take(host, gate=80.0):
    s = Sampler(host.fs, psi_busy_gate_pct=gate)
    s.prime(host.prev_stat)
    return s.sample(now=1_762_800_000.0)


def headroom(sample):
    th = thresholds(parity_config().headroom, "impl", None, sample["host"]["cores"])
    return decide(sample, th)


def psi_line(sample):
    text = format_status(
        {
            **sample,
            "cgroups": {},
            "heavy_slot": {"held": False},
            "daemon": {"version": "t", "pid": 1, "mode": "observe"},
        }
    )
    return next(x for x in text.splitlines() if x.lstrip().startswith("psi"))


def test_throttled_idle_host_is_ok_and_headroom_passes(fake_host):
    s = take(fake_host("calm", busy_pct=38.7, cpu_psi=THROTTLED))
    assert s["cpu"]["psi_some_avg60"] == 53.0 and s["cpu"]["psi_effective"] == 0.0  # raw kept
    assert s["levels"]["cpu"] == "OK" and s["level"] == "OK"
    d = headroom(s)
    assert d.passed and d.values["cpu_psi"] == 0.0


def test_same_pressure_on_a_busy_host_still_blocks(fake_host):
    s = take(fake_host("calm", busy_pct=90.0, cpu_psi=THROTTLED))
    assert s["cpu"]["psi_effective"] == 53.0
    assert s["levels"]["cpu"] == "BUSY"  # 53 >= 40 busy, < 70 limit
    d = headroom(s)
    assert not d.passed and any(r.startswith("cpu psi 53.0%") for r in d.reasons)


def test_gate_zero_keeps_the_old_behaviour(fake_host):
    s = take(fake_host("calm", busy_pct=5.0, cpu_psi=THROTTLED), gate=0)
    assert s["cpu"]["psi_effective"] == 53.0 and s["levels"]["cpu"] == "BUSY"
    assert not headroom(s).passed


def test_boundary_is_inclusive(fake_host):
    at = take(fake_host("calm", busy_pct=80.0, cpu_psi=THROTTLED))
    below = take(fake_host("calm", busy_pct=79.9, cpu_psi=THROTTLED))
    assert at["cpu"]["busy_pct"] == 80.0 and at["cpu"]["psi_effective"] == 53.0
    assert below["cpu"]["psi_effective"] == 0.0


def test_levels_read_the_effective_value_not_the_raw_one():
    s = {
        "cpu": {"busy_pct": 10.0, "psi_some_avg60": 99.0, "psi_effective": 0.0, "load1_per_core": 0.1},
        "memory": {"avail_mb": 20000}, "swap": {"used_pct": 0.0}, "disk": {"free_gb": 100},
    }  # fmt: skip
    assert evaluate(s)[1] == "OK"  # raw 99 ignored
    s["cpu"].update(psi_some_avg60=0.0, psi_effective=75.0, busy_pct=99.0)
    assert evaluate(s)[0]["cpu"] == "LIMIT"  # effective 75 decides


def test_status_line_shows_effective_next_to_raw(fake_host):
    idle = take(fake_host("calm", busy_pct=38.7, cpu_psi=THROTTLED))
    assert "cpu 62/53 (throttling only, counts 0)" in psi_line(idle)
    busy = take(fake_host("calm", busy_pct=90.0, cpu_psi=THROTTLED))
    assert "cpu 62/53  " in psi_line(busy) and "throttling" not in psi_line(busy)


def test_config_key():
    assert parse_config("").headroom.psi_busy_gate_pct == 80.0
    assert parse_config("[headroom]\npsi_busy_gate_pct = 0\n").headroom.psi_busy_gate_pct == 0.0
    for bad in ("-1", "101", "true", '"80"'):
        with pytest.raises(ConfigError):
            parse_config(f"[headroom]\npsi_busy_gate_pct = {bad}\n")
