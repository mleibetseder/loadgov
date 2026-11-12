"""CPU governor against a fake cgroup tree and a fake clock (never the real /sys)."""

import json
from dataclasses import replace

import pytest

from loadgov.config import ConfigError, parse_config
from loadgov.daemon.main import Daemon
from loadgov.engines.governor import PRIMARY, Governor
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS
from loadgov.protocol import validate_status

T0 = 1_762_800_000.0


class Clock:
    t = T0

    def __call__(self):
        return self.t


class Rig:
    """A Governor on a tmp cgroup tree, fed synthetic busy values at a fixed cadence."""

    def __init__(self, tmp_path, cap="200000 100000", mirror="200000 100000", enforce=True, sync=True):
        self.root = tmp_path / "cg"
        self.fs = HostFS(cgroup_root=str(self.root))
        if cap is not None:
            self.write(PRIMARY, cap)
        if mirror is not None:
            self.write("loop-a", mirror)
        self.clock = Clock()
        self.gov = Governor(self.fs, self.clock, enforce, ("loop-a",) if sync else (), monotonic=self.clock)
        self.total = self.idle = 0

    def write(self, name, text):
        p = self.root / name / "cpu.max"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")

    def raw(self, name=PRIMARY):
        return (self.root / name / "cpu.max").read_bytes()

    def step(self, busy, seconds=10, max_cores=3.5, target=65):
        """Advance the clock and the jiffies by `seconds` at `busy` %, then tick."""
        self.clock.t += seconds
        jiffies = seconds * 100
        self.total += jiffies
        self.idle += round(jiffies * (100 - busy) / 100)
        self.gov.tick((self.total, self.idle), max_cores, target)

    def prime(self, **kw):
        self.step(0, 0, **kw)  # the first call only records the baseline


def test_replay_82_83_75_steps_two_one_and_a_half_one(tmp_path):
    r = Rig(tmp_path)
    r.prime()
    caps = []
    for busy in (82, 83, 75):
        r.step(busy)
        caps.append(r.gov.cap)
    assert caps == [1.5, 1.0, 1.0]  # the third sample would go to 0.5: MIN 1.0 holds
    assert r.raw() == b"100000 100000\n"
    assert r.raw("loop-a") == b"100000 100000\n"  # mirrored
    events = r.gov.drain_events()
    assert [(e["before"], e["cap"], e["busy"]) for e in events] == [(2.0, 1.5, 82), (1.5, 1.0, 83)]
    assert events[0]["reason"] == "host above 65%" and events[0]["applied"] is True


def test_nine_calm_samples_raise_eight_do_not(tmp_path):
    r = Rig(tmp_path, cap="150000 100000", mirror=None)
    r.prime()
    for _ in range(8):
        r.step(54)  # < target - 10 = 55
    assert r.gov.cap == 1.5 and r.raw() == b"150000 100000\n"
    r.step(54)
    assert r.gov.cap == 2.0 and r.raw() == b"200000 100000\n"
    assert r.gov.status()["last_reason"] == "room again"


def test_an_in_between_sample_resets_the_calm_counter(tmp_path):
    r = Rig(tmp_path, cap="150000 100000", mirror=None)
    r.prime()
    for _ in range(8):
        r.step(50)
    r.step(60)  # between 55 and 65: counters reset
    for _ in range(8):
        r.step(50)
    assert r.gov.cap == 1.5
    r.step(50)
    assert r.gov.cap == 2.0


def test_the_cap_never_rises_above_max_cores(tmp_path):
    r = Rig(tmp_path, cap="200000 100000", mirror=None)
    r.prime()
    for _ in range(27):
        r.step(10, max_cores=2.5)
    assert r.gov.cap == 2.5


def test_lowered_max_cores_clamps_at_once_without_a_sample(tmp_path):
    r = Rig(tmp_path, cap="300000 100000", mirror="300000 100000")
    r.prime(max_cores=3.5)
    assert r.gov.cap == 3.0
    r.clock.t += 1  # nowhere near the 10 s cadence
    r.gov.tick((r.total, r.idle), 2.0, 65)
    assert r.gov.cap == 2.0 and r.raw() == b"200000 100000\n" and r.raw("loop-a") == b"200000 100000\n"
    assert r.gov.drain_events()[-1]["reason"] == "max_cores lowered to 2"
    r.gov.clamp(1.5)  # the daemon's immediate path after an HA or CLI change
    assert r.raw() == b"150000 100000\n"


def test_start_only_lowers_to_the_lower_of_both_targets(tmp_path):
    r = Rig(tmp_path, cap="350000 100000", mirror="150000 100000")
    r.prime(max_cores=3.5)
    assert r.gov.cap == 1.5
    assert r.raw() == b"150000 100000\n" and r.raw("loop-a") == b"150000 100000\n"
    r.step(90)
    assert r.raw() == b"100000 100000\n" and r.raw("loop-a") == b"100000 100000\n"  # never 3.0
    assert r.gov.drain_events()[0]["reason"] == "start"


def test_start_never_raises_a_lower_primary(tmp_path):
    r = Rig(tmp_path, cap="150000 100000", mirror="350000 100000")
    r.prime(max_cores=3.5)
    assert r.raw() == b"150000 100000\n" and r.raw("loop-a") == b"150000 100000\n"


def test_proj_a_loop_appearing_later_is_adopted_if_lower(tmp_path):
    r = Rig(tmp_path, cap="300000 100000", mirror=None)
    r.prime(max_cores=3.5)
    assert r.gov.cap == 3.0
    r.write("loop-a", "100000 100000")
    r.step(60)
    assert r.gov.cap == 1.0 and r.raw() == b"100000 100000\n"


def test_a_busy_sample_never_raises_an_adopted_sub_minimum_cap(tmp_path):
    r = Rig(tmp_path, cap="50000 100000", mirror=None)
    r.prime()
    r.step(90)
    assert r.gov.cap == 0.5 and r.raw() == b"50000 100000\n"
    for _ in range(9):
        r.step(10)  # calm: climbs normally, up to max_cores
    assert r.gov.cap == 1.0


def test_wall_clock_steps_do_not_stall_the_window(tmp_path):
    mono = Clock()
    mono.t = 0.0
    wall = Clock()
    r = Rig(tmp_path, mirror=None)
    r.gov = Governor(r.fs, wall, True, ("loop-a",), monotonic=mono)
    r.gov.tick((0, 0), 3.5, 65)
    wall.t -= 3600  # NTP / manual step backwards
    mono.t += 10
    r.gov.tick((1000, 100), 3.5, 65)  # 90 % busy over 10 monotonic seconds
    assert r.gov.cap == 1.5
    wall.t += 7200
    mono.t += 10
    r.gov.tick((2000, 200), 3.5, 65)
    assert r.gov.cap == 1.0


def test_raised_max_cores_does_not_jump(tmp_path):
    r = Rig(tmp_path, cap="200000 100000", mirror=None)
    r.prime(max_cores=2.0)
    r.step(60, max_cores=5.0)  # in between: nothing moves
    assert r.gov.cap == 2.0 and r.raw() == b"200000 100000\n"


def test_start_value_is_the_current_cap_not_max_cores(tmp_path):
    r = Rig(tmp_path, cap="120000 100000", mirror=None)
    r.prime(max_cores=3.5)
    assert r.gov.cap == 1.2 and r.raw() == b"120000 100000\n"


def test_unlimited_cgroup_starts_at_max_cores(tmp_path):
    r = Rig(tmp_path, cap="max 100000", mirror=None)
    r.prime(max_cores=3.5)
    assert r.gov.cap == 3.5 and r.raw() == b"350000 100000\n"


def test_two_five_second_ticks_make_one_ten_second_sample(tmp_path):
    r = Rig(tmp_path, cap="200000 100000", mirror=None)
    r.prime()
    r.step(100, 5)  # a 5 s value alone would be 100 % ...
    assert r.gov.cap == 2.0 and r.gov.status()["last_busy_pct"] is None
    r.step(40, 5)  # ... but the 10 s window is 70 % > 65
    assert r.gov.cap == 1.5 and r.gov.status()["last_busy_pct"] == 70


def test_observe_mode_never_touches_cpu_max_and_logs_would(tmp_path):
    r = Rig(tmp_path, enforce=False)
    before = (r.raw(), r.raw("loop-a"))
    r.prime()
    for busy in [82, 83, 75] + [60] * 30:
        r.step(busy)
    assert (r.raw(), r.raw("loop-a")) == before
    events = r.gov.drain_events()
    assert [e["cap"] for e in events] == [1.5, 1.0]  # only changes of the decision are logged
    assert all(not e["applied"] for e in events)
    assert r.gov.status()["cap_cores"] == 1.0 and r.gov.status()["enforce"] is False


def test_missing_proj_a_loop_is_skipped(tmp_path):
    r = Rig(tmp_path, mirror=None)
    r.prime()
    r.step(90)
    assert r.raw() == b"150000 100000\n"
    assert not (r.root / "loop-a").exists()
    t = r.gov.status()["targets"]
    assert t[0] == {"path": PRIMARY, "present": True, "cpu_max": "150000 100000"}
    assert t[1] == {"path": "loop-a", "present": False, "cpu_max": None}


def test_sync_off_leaves_proj_a_loop_alone(tmp_path):
    r = Rig(tmp_path, sync=False)
    r.prime()
    r.step(90)
    assert r.raw() == b"150000 100000\n" and r.raw("loop-a") == b"200000 100000\n"
    assert [t["path"] for t in r.gov.status()["targets"]] == [PRIMARY]


def test_missing_primary_cgroup_is_a_status_note_not_an_error(tmp_path):
    r = Rig(tmp_path, cap=None, mirror="200000 100000")
    r.prime()
    r.step(90)
    assert r.gov.cap is None and r.gov.drain_events() == []
    assert r.raw("loop-a") == b"200000 100000\n"
    assert r.gov.status()["targets"][0]["present"] is False
    r.write(PRIMARY, "200000 100000")  # appears later: governed from then on
    r.step(90)
    assert r.gov.cap == 1.5 and r.raw() == b"150000 100000\n"


def test_failed_write_keeps_the_cap_and_retries(tmp_path, monkeypatch):
    r = Rig(tmp_path, mirror=None)
    r.prime()

    def refuse(*_a):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cgroup, "write_cpu_max", refuse)
    r.step(90)
    assert r.gov.cap == 2.0 and r.gov.drain_events() == []
    monkeypatch.undo()
    r.step(90)  # the next sample decides again and succeeds
    assert r.gov.cap == 1.5 and r.raw() == b"150000 100000\n"


# ---- host/cgroup.py ------------------------------------------------------------------------------


def test_cgroup_cpu_max_parse_and_exact_write(tmp_path):
    fs = HostFS(cgroup_root=str(tmp_path))
    assert cgroup.read_cpu_max(fs, "a/b") is None
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert cgroup.read_cpu_max(fs, "a/b") is None  # no cpu.max file
    (tmp_path / "a" / "b" / "cpu.max").write_text("max 100000\n")
    assert cgroup.read_cpu_max(fs, "a/b") == cgroup.CpuMax(None, 100000)
    cgroup.write_cpu_max(fs, "a/b", 1.5)
    assert (tmp_path / "a" / "b" / "cpu.max").read_bytes() == b"150000 100000\n"
    assert cgroup.read_cpu_max(fs, "/a/b/").cores == 1.5


def test_cgroup_write_never_writes_max_creates_files_or_escapes_the_root(tmp_path):
    fs = HostFS(cgroup_root=str(tmp_path))
    with pytest.raises(OSError):
        cgroup.write_cpu_max(fs, "absent", 2.0)
    assert not (tmp_path / "absent").exists()
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "cpu.max").write_text("200000 100000\n")
    for bad in ("max", float("inf"), 0, -1):
        with pytest.raises((ValueError, TypeError, OverflowError)):
            cgroup.write_cpu_max(fs, "x", bad)  # type: ignore[arg-type]
    assert (tmp_path / "x" / "cpu.max").read_text() == "200000 100000\n"
    with pytest.raises(ValueError):
        cgroup.write_cpu_max(fs, "../x", 1.0)


# ---- config and daemon ---------------------------------------------------------------------------


def test_config_governor_and_compat_keys():
    c = parse_config(
        '[governor]\nenforce = true\n[compat]\nmirror_cgroups = ["loop-a", "loop-b/x"]\n'
        'governor_log_path = "/tmp/g.log"'
    )
    assert c.governor_enforce is True
    assert c.compat.mirror_cgroups == ("loop-a", "loop-b/x") and c.compat.governor_log_path == "/tmp/g.log"
    d = parse_config("")
    assert (
        d.governor_enforce is False and d.compat.mirror_cgroups == () and d.compat.governor_log_path is None
    )
    for bad in ("[governor]\nenforce = 1", "[governor]\nx = 1", "[compat]\nmirror_cgroups = 1",
                '[compat]\nmirror_cgroups = ["/abs"]', '[compat]\nmirror_cgroups = ["../x"]',
                '[compat]\ngovernor_log_path = "rel.log"'):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_config(bad)


def mk_daemon(fake_host, tmp_path, cfg_text, cap="200000 100000"):
    host = fake_host("calm", cpu_max={"loop-a": "200000 100000", PRIMARY: cap})
    clock = Clock()
    cfg = parse_config(cfg_text)
    cfg = replace(cfg, compat=replace(cfg.compat, mirror_cgroups=("loop-a",)))
    d = Daemon(host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
               config=cfg, clock=clock, run_dir=str(tmp_path / "run"))  # fmt: skip
    (tmp_path / "run").mkdir()
    d.sampler.prime(host.prev_stat)
    return d, host, clock


def audit_lines(d):
    return [json.loads(line) for line in open(d.audit.path)]


def test_daemon_observe_mode_status_audit_and_legacy_log(fake_host, tmp_path):
    glog = tmp_path / "governor.log"
    d, host, _ = mk_daemon(fake_host, tmp_path, f'[compat]\ngovernor_log_path = "{glog}"')
    before = open(f"{host.cgroup}/{PRIMARY}/cpu.max", "rb").read()
    d.limits.set("owner", {"max_cores": 1.5}, "test")  # below the cap: observe mode still must not write
    d._after_change({})
    g = d.tick()["governor"]
    assert validate_status(d.status) == []
    assert g["enforce"] is False and g["cap_cores"] == 1.5 and g["max_cores"] == 1.5 and g["min_cores"] == 1.0
    assert g["last_reason"] == "start" and g["targets"][0]["cpu_max"] == "200000 100000"
    assert open(f"{host.cgroup}/{PRIMARY}/cpu.max", "rb").read() == before
    line = [a for a in audit_lines(d) if a["op"] == "governor"][0]
    assert line["result"] == "would" and line["reason"] == "WOULD cap=1.5 cores busy=-% (start)"
    assert glog.read_text().split(" ", 1)[1] == line["reason"] + "\n"


def test_daemon_enforce_reapplies_a_lowered_max_cores_immediately(fake_host, tmp_path):
    d, host, _ = mk_daemon(fake_host, tmp_path, "[governor]\nenforce = true", cap="300000 100000")
    open(f"{host.cgroup}/loop-a/cpu.max", "w").write("300000 100000\n")
    d.tick()
    assert open(f"{host.cgroup}/{PRIMARY}/cpu.max").read() == "300000 100000\n"
    d.limits.set("owner", {"max_cores": 2.0}, "HA slider")
    d._after_change({})  # no tick, no sample
    for p in (PRIMARY, "loop-a"):
        assert open(f"{host.cgroup}/{p}/cpu.max").read() == "200000 100000\n"
    line = [a for a in audit_lines(d) if a["op"] == "governor"][0]
    assert (line["result"], line["before"], line["after"]) == ("applied", "3", "2")


def test_proj_a_loop_only_lowering_is_audited_and_updates_last_reason(tmp_path):
    r = Rig(tmp_path, cap="200000 100000", mirror=None)
    r.prime()
    r.gov.drain_events()
    r.write("loop-a", "300000 100000")  # recreated above the cap; the primary is already at it
    r.step(50)
    assert r.raw("loop-a") == b"200000 100000\n" and r.raw() == b"200000 100000\n"
    (ev,) = r.gov.drain_events()
    assert ev["reason"] == "target above cap" and ev["applied"] is True and ev["before"] == 3.0
    assert r.gov.status()["last_reason"] == "target above cap"
    assert r.gov.status()["last_change_at"] is not None


def test_parse_cpu_times_sums_every_field_like_the_script():
    from loadgov.sample import parse_cpu_times

    line = "cpu  100 5 50 800 20 3 2 7 40 10\ncpu0 1 1 1 1 1 1 1 1 1 1\n"
    # legacy governor: t = sum of fields 2..NF (guest 40 and guest_nice 10 included), idle = $5 + $6
    assert parse_cpu_times(line) == (100 + 5 + 50 + 800 + 20 + 3 + 2 + 7 + 40 + 10, 820)
    assert parse_cpu_times("cpu  1 2 3 4 5 6 7 8\n") == (36, 9)  # older kernels: eight fields


def test_the_old_boolean_key_is_gone():
    with pytest.raises(ConfigError):
        parse_config("[compat]\nsync_loop = true")


def test_several_mirrors_follow_the_cap(tmp_path):
    r = Rig(tmp_path, cap="200000 100000", mirror="200000 100000")
    r.write("loop-b", "200000 100000")
    r.gov.mirrors = ("loop-a", "loop-b")
    r.gov.clamp(1.5)
    assert (r.root / "loop-a" / "cpu.max").read_text().split()[0] == "150000"
    assert (r.root / "loop-b" / "cpu.max").read_text().split()[0] == "150000"
