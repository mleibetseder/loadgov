"""Memory backstop: config, apply, rollback, detection and the job hook, on a fake cgroup tree (no host)."""

import json
import os
from types import SimpleNamespace

import pytest

from loadgov.config import BackstopConfig, ConfigError, parse_config
from loadgov.daemon.backstop_ops import MIB, BackstopRuntime
from loadgov.host import cgroup
from loadgov.host.procfs import HostFS

SLICE = "build.slice/build-work.slice"
SCOPE = f"/{SLICE}/build-work-proj-a.slice/loadgov-impl-x-1.scope"
UNIT = "loadgov-impl-x-1.scope"


# ---- config ---------------------------------------------------------------------------------------


def test_backstop_defaults_off_without_values():
    c = parse_config("").backstop
    assert c == BackstopConfig(False, None, None)


@pytest.mark.parametrize(
    "body, msg",
    [
        ("enabled = true", "needs both"),
        ("enabled = true\nmemory_max_mb = 8192", "needs both"),
        ("enabled = true\nmemory_swap_max_mb = 1024", "needs both"),
        ("memory_max_mb = 4095", ">= 4096"),
        ("memory_max_mb = 8192.5", ">= 4096"),
        ("memory_swap_max_mb = -1", ">= 0"),
        ("enabled = 1", "boolean"),
        ("oops = 1", "allowed keys"),
    ],
)
def test_backstop_config_errors(body, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(f"[backstop]\n{body}\n")


def test_backstop_config_accepts_a_full_table_and_values_without_enabled():
    c = parse_config("[backstop]\nenabled = true\nmemory_max_mb = 12288\nmemory_swap_max_mb = 0\n").backstop
    assert c == BackstopConfig(True, 12288, 0)
    assert parse_config("[backstop]\nmemory_max_mb = 8192\n").backstop.enabled is False


def test_backstop_cap_margin_defaults_to_512_and_is_checked():
    assert parse_config("[backstop]\nmemory_max_mb = 8192\n").backstop.cap_margin_mb == 512
    assert parse_config("[backstop]\ncap_margin_mb = 1024\n").backstop.cap_margin_mb == 1024
    for bad in ("-1", "1.5", "true", "70000"):
        with pytest.raises(ConfigError, match="cap_margin_mb"):
            parse_config(f"[backstop]\ncap_margin_mb = {bad}\n")


# ---- rig ------------------------------------------------------------------------------------------


class FakeSystemd:
    """Records systemctl argv and acts on the fake cgroup tree like systemd: `set-property` writes the
    sizes to the slice's files, `start` creates the slice directory. `rc` != 0 fails every call; `lazy`
    returns 0 but leaves the files alone (a systemd that did not realize the property)."""

    def __init__(self, slice_dir, root=None):
        self.slice_dir, self.root, self.calls, self.rc, self.lazy = slice_dir, root, [], 0, False

    def _dir(self, unit):
        if self.root is None or unit == self.slice_dir.name:
            return self.slice_dir
        from loadgov.cli.run import slice_cgroup

        return self.root / slice_cgroup(unit)  # a project slice, nested by its dashes

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.rc or self.lazy:
            return self.rc
        if argv[1] == "start":
            self.slice_dir.mkdir(parents=True, exist_ok=True)
            for name in ("memory.max", "memory.swap.max"):
                (self.slice_dir / name).write_text("max\n")
            (self.slice_dir / "memory.current").write_text("0\n")
        elif argv[1] == "set-property":
            d = self._dir(argv[3])
            files = {
                "MemoryMax": "memory.max",
                "MemorySwapMax": "memory.swap.max",
                "MemoryHigh": "memory.high",
            }
            for prop in argv[4:]:
                key, val = prop.split("=")
                out = "max" if val == "infinity" else str(int(val.rstrip("M")) * MIB)
                if (d / files[key]).exists():  # an inactive slice: systemd only stores the property
                    (d / files[key]).write_text(out + "\n")
        return 0

    def verbs(self):
        return [c[1] for c in self.calls]


class Rig:
    def __init__(self, tmp_path, cfg, runner=True):
        self.root = tmp_path / "cg"
        self.state = tmp_path / "state"
        self.state.mkdir()
        (tmp_path / "proc" / "self").mkdir(parents=True)
        self.fs = HostFS(cgroup_root=str(self.root), proc_root=str(tmp_path / "proc"))
        self.events, self.audits, self.jobs = [], [], {}
        self.slice = self.root / SLICE
        self.slice.mkdir(parents=True)
        self.put(self.slice, "memory.max", "max")
        self.put(self.slice, "memory.swap.max", "max")
        self.put(self.slice, "memory.current", 1000 * MIB)
        self.put(self.slice, "memory.peak", 1500 * MIB)
        self.put(self.slice, "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0")
        self.systemd = FakeSystemd(self.slice, self.root)
        self.t = [1_762_800_000.0]
        self.rt = self.runtime(cfg, self.systemd if runner else None)

    def runtime(self, cfg, runner, **kw):
        return BackstopRuntime(
            self.fs, cfg, lambda: self.t[0], str(self.state),
            lambda kind, payload: self.events.append((kind, payload)), self.audits.append, lambda: self.jobs,
            runner=runner, **kw,
        )  # fmt: skip

    @staticmethod
    def put(d, name, value):
        (d / name).write_text(f"{value}\n")

    def read(self, d, name):
        return (d / name).read_text().strip()

    def scope(self, peak_mb=100, kills=0):
        d = self.root / SCOPE.lstrip("/")
        d.mkdir(parents=True, exist_ok=True)
        self.put(d, "memory.oom.group", 0)
        self.put(d, "memory.peak", peak_mb * MIB)
        self.put(d, "memory.events", f"oom_kill {kills}")
        self.jobs[UNIT] = SimpleNamespace(cgroup=SCOPE)
        return d

    def kill_slice_counter(self, kills=1, group=1):
        self.put(self.slice, "memory.events", f"oom_kill {kills}\noom_group_kill {group}")


ON = BackstopConfig(True, 8192, 1024)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, ON)


# ---- apply ----------------------------------------------------------------------------------------


def test_disabled_writes_nothing_and_reads_no_marker(tmp_path):
    r = Rig(tmp_path, BackstopConfig())
    r.scope()
    r.rt.tick()
    r.rt.on_job(UNIT, SCOPE)
    assert r.read(r.slice, "memory.max") == "max" and r.read(r.slice, "memory.swap.max") == "max"
    assert r.read(r.root / SCOPE.lstrip("/"), "memory.oom.group") == "0"
    assert not os.path.exists(r.rt.marker) and r.audits == [] and r.events == []
    assert r.rt.status()["enabled"] is False and r.rt.status()["applied"] is False


def test_enabled_writes_exact_bytes_marker_first_and_audits_once(rig):
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)
    assert rig.read(rig.slice, "memory.swap.max") == str(1024 * MIB)
    assert json.load(open(rig.rt.marker)) == {"slice": SLICE, "capped_mb": None, "projects": {}}
    assert rig.rt.status()["applied"] is True and rig.rt.status()["note"] is None
    rig.rt.tick()
    assert [a["result"] for a in rig.audits] == ["applied"]  # not again while nothing changes


def test_a_changed_value_is_reasserted_and_max_is_never_written_while_enabled(rig):
    rig.rt.tick()
    rig.put(rig.slice, "memory.max", "max")  # somebody lifted it by hand
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)


def test_usage_above_the_target_caps_at_usage_plus_margin_then_steps_down_with_one_alert(rig):
    rig.put(rig.slice, "memory.current", 9000 * MIB)
    rig.rt.tick()
    assert rig.systemd.calls[-1] == ["systemctl", "set-property", "--runtime", "build-work.slice",
                                     "MemoryMax=9512M", "MemorySwapMax=1024M"]  # fmt: skip
    assert rig.read(rig.slice, "memory.max") == str(9512 * MIB)  # never at or below the usage, never max
    st = rig.rt.status()
    assert st["applied"] is False and st["capped_mb"] == 9512 and st["note"].startswith("capped at 9512 MB")
    assert [k for k, _ in rig.events] == ["backstop_cap"]
    assert [a["result"] for a in rig.audits] == ["capped"]
    rig.put(rig.slice, "memory.current", 9300 * MIB)  # usage rose: the cap never rises
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(9512 * MIB)
    rig.put(rig.slice, "memory.current", 8500 * MIB)  # usage fell: the cap follows it down
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(9012 * MIB) and rig.rt.capped_mb == 9012
    rig.put(rig.slice, "memory.current", 7000 * MIB)  # usage + margin below the target: the target
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)
    st = rig.rt.status()
    assert st["applied"] is True and st["capped_mb"] is None and st["note"] is None and st["via"] == "systemd"
    assert [k for k, _ in rig.events] == ["backstop_cap"]  # one alert for the whole episode
    assert rig.audits[-1]["result"] == "applied"


def test_a_reset_to_max_while_usage_sits_at_the_limit_is_capped_not_deferred(rig):
    """The incident: systemd wrote `max` over the limit while usage sat at it; the old code deferred."""
    rig.rt.tick()
    rig.put(rig.slice, "memory.current", 8192 * MIB)
    rig.put(rig.slice, "memory.max", "max")  # re-realization by systemd
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str((8192 + 512) * MIB)
    assert rig.events[-1][0] == "backstop_cap"


def test_systemctl_runs_at_start_and_only_when_the_files_differ(rig):
    rig.put(rig.slice, "memory.max", str(8192 * MIB))  # already right (e.g. a static unit property)
    rig.put(rig.slice, "memory.swap.max", str(1024 * MIB))
    rig.rt.tick()
    assert rig.systemd.verbs() == ["set-property"]  # once at start, so systemd owns the values
    for _ in range(3):
        rig.rt.tick()
    assert rig.systemd.verbs() == ["set-property"]  # not every tick
    rig.put(rig.slice, "memory.max", "max")
    rig.rt.tick()
    assert rig.systemd.verbs() == ["set-property", "set-property"]


def test_disabled_never_runs_systemctl(tmp_path):
    r = Rig(tmp_path, BackstopConfig())
    os.unlink(r.slice / "memory.max")  # even a missing slice is not started
    for _ in range(3):
        r.rt.tick()
    assert r.systemd.calls == []


def test_an_inactive_slice_is_started_first_then_limited(tmp_path):
    r = Rig(tmp_path, ON)
    for f in r.slice.iterdir():
        f.unlink()
    r.slice.rmdir()  # after boot: no cgroup directory until the first job
    r.rt.tick()
    assert r.systemd.calls == [["systemctl", "start", "build-work.slice"]]  # one systemctl call per tick
    assert r.rt.status()["applied"] is False and "limits follow on the next tick" in r.rt.note
    r.rt.tick()
    assert r.systemd.verbs() == ["start", "set-property"]
    assert r.read(r.slice, "memory.max") == str(8192 * MIB) and r.rt.status()["applied"] is True


def test_a_slice_that_will_not_start_is_retried_slowly_and_reported(tmp_path):
    r = Rig(tmp_path, ON)
    for f in r.slice.iterdir():
        f.unlink()
    r.slice.rmdir()
    r.systemd.rc = 1
    r.rt.tick()
    r.rt.tick()
    assert r.systemd.verbs() == ["start"]  # not again within RETRY_S
    assert r.rt.applied is False and "no memory limit files" in r.rt.note


def test_set_property_failure_falls_back_to_the_file_write_and_says_so(rig):
    rig.systemd.rc = 1
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)
    st = rig.rt.status()
    assert st["applied"] is True and st["via"] == "file"
    assert st["note"].startswith("systemctl set-property exit 1: wrote the cgroup files directly")


def test_files_are_verified_after_set_property(rig):
    rig.systemd.lazy = True  # exit 0 but the files still say max
    rig.rt.tick()
    st = rig.rt.status()
    assert st["via"] == "file" and st["note"].startswith("the files differ after systemctl set-property")
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)


def test_the_real_runner_is_refused_on_an_injected_cgroup_root(tmp_path):
    from loadgov.daemon import slot_ops
    from loadgov.daemon.backstop_ops import slice_unit, trusted_runner

    r = Rig(tmp_path, ON, runner=False)
    assert trusted_runner(r.fs, slot_ops.run_argv) is None  # fail closed: no test reaches the host
    assert r.runtime(ON, slot_ops.run_argv).runner is None
    fake = FakeSystemd(r.slice)
    assert trusted_runner(r.fs, fake) is fake
    assert slice_unit("build.slice/build-work.slice") == "build-work.slice"
    with pytest.raises(ValueError):
        slice_unit("build.slice/bad unit")


def test_without_a_runner_the_files_are_written_directly_and_reported(tmp_path):
    r = Rig(tmp_path, ON, runner=False)
    r.rt.tick()
    assert r.read(r.slice, "memory.max") == str(8192 * MIB)
    assert r.rt.status()["via"] == "file" and r.rt.status()["note"].startswith("no systemctl runner")


def test_a_missing_slice_or_unwritable_marker_writes_nothing(tmp_path):
    r = Rig(tmp_path, ON, runner=False)  # no systemctl to start the slice with
    os.unlink(r.slice / "memory.max")
    r.rt.tick()
    assert r.rt.applied is False and "no memory limit files" in r.rt.note
    (tmp_path / "b").mkdir()
    r2 = Rig(tmp_path / "b", ON)
    (tmp_path / "b" / "state").rmdir()
    (tmp_path / "b" / "state").write_text("not a directory")  # the marker cannot be written
    r2.rt.tick()
    assert r2.read(r2.slice, "memory.max") == "max" and r2.rt.note.startswith("refused: cannot persist")
    assert r2.audits[-1]["result"] == "refused"


def test_cgroup_helpers_refuse_bad_names_and_values(rig):
    with pytest.raises(ValueError):
        cgroup.write_memory_limit(rig.fs, SLICE, "cpu.max", 5)
    with pytest.raises(ValueError):
        cgroup.write_memory_limit(rig.fs, SLICE, "memory.max", 0)
    with pytest.raises(ValueError):
        cgroup.clear_memory_limit(rig.fs, SLICE, "memory.min")
    with pytest.raises(FileNotFoundError):  # never creates a file
        cgroup.write_oom_group(rig.fs, "/nowhere/x.scope")


# ---- rollback -------------------------------------------------------------------------------------


def test_a_failed_set_property_infinity_keeps_the_marker_and_is_retried_until_it_succeeds(rig):
    from loadgov.daemon.backstop_ops import RETRY_S

    rig.rt.tick()
    off = rig.runtime(BackstopConfig(), rig.systemd)  # a restart without the table
    rig.systemd.rc = 1
    rig.audits.clear()
    off.tick()
    infinity = ["systemctl", "set-property", "--runtime", "build-work.slice", "MemoryMax=infinity",
                "MemorySwapMax=infinity"]  # fmt: skip
    assert rig.systemd.calls[-1] == infinity
    assert os.path.exists(off.marker)  # the runtime property is still there: nothing may forget it
    assert off.note.startswith("rollback incomplete: systemctl set-property build-work.slice")
    assert [a["result"] for a in rig.audits] == ["failed"]
    assert rig.read(rig.slice, "memory.max") == "max"  # the files are freed at once anyway
    n = len(rig.systemd.calls)
    rig.t[0] += RETRY_S - 1
    off.tick()
    assert len(rig.systemd.calls) == n  # rate-limited
    rig.t[0] += 1
    off.tick()
    assert len(rig.systemd.calls) == n + 1 and rig.systemd.calls[-1] == infinity  # the eligible retry
    assert os.path.exists(off.marker)  # still failing
    rig.systemd.rc = 0
    rig.t[0] += RETRY_S
    off.tick()
    assert rig.systemd.calls[-1] == infinity and not os.path.exists(off.marker)
    assert off.note is None and rig.audits[-1]["result"] == "rollback"


def test_a_restart_while_capped_keeps_the_cap_and_never_raises_it(rig):
    rig.put(rig.slice, "memory.current", 9000 * MIB)
    rig.rt.tick()
    assert rig.rt.capped_mb == 9512 and json.load(open(rig.rt.marker))["capped_mb"] == 9512
    rig.put(rig.slice, "memory.current", 9800 * MIB)  # usage rose; then the daemon restarts
    again = rig.runtime(ON, rig.systemd)
    again.tick()
    assert again.capped_mb == 9512 and rig.read(rig.slice, "memory.max") == str(9512 * MIB)
    assert [k for k, _ in rig.events] == ["backstop_cap"]  # the episode goes on: no second alert


def test_the_cap_steps_down_only_by_a_quarter_margin_or_to_the_target(rig):
    rig.put(rig.slice, "memory.current", 9000 * MIB)
    rig.rt.tick()  # cap 9512
    n = len(rig.systemd.calls)
    rig.put(rig.slice, "memory.current", 8900 * MIB)  # 9412 would be only 100 MB lower (< 512 / 4)
    rig.rt.tick()
    assert rig.rt.capped_mb == 9512 and len(rig.systemd.calls) == n
    rig.put(rig.slice, "memory.current", 8850 * MIB)  # 9362: 150 MB lower
    rig.rt.tick()
    assert rig.rt.capped_mb == 9362 and len(rig.systemd.calls) == n + 1
    rig.put(rig.slice, "memory.current", 7650 * MIB)  # 8162 is below the target: the target, any step
    rig.rt.tick()
    assert rig.rt.capped_mb is None and rig.read(rig.slice, "memory.max") == str(8192 * MIB)


def test_rollback_writes_max_back_once_and_removes_the_marker(rig, tmp_path):
    rig.rt.tick()
    off = BackstopRuntime(
        rig.fs, BackstopConfig(), lambda: 1.0, str(rig.state), lambda *a: None, rig.audits.append, lambda: {},
        runner=rig.systemd,
    )  # a restart without the table  # fmt: skip
    rig.put(rig.slice, "memory.max", str(8192 * MIB))
    rig.systemd.lazy = True  # only the file write may bring `max` back here
    off.tick()
    assert rig.systemd.calls[-1] == ["systemctl", "set-property", "--runtime", "build-work.slice",
                                     "MemoryMax=infinity", "MemorySwapMax=infinity"]  # fmt: skip
    assert rig.read(rig.slice, "memory.max") == "max" and rig.read(rig.slice, "memory.swap.max") == "max"
    assert not os.path.exists(off.marker)
    assert rig.audits[-1]["result"] == "rollback"
    n = len(rig.audits)
    off.tick()  # marker gone: nothing more happens
    assert len(rig.audits) == n


def test_rollback_without_the_slice_just_drops_the_marker(rig):
    rig.rt.tick()
    for name in ("memory.max", "memory.swap.max"):
        os.unlink(rig.slice / name)
    off = BackstopRuntime(rig.fs, BackstopConfig(), lambda: 1.0, str(rig.state), lambda *a: None,
                          rig.audits.append, lambda: {})  # fmt: skip
    off.tick()
    assert not os.path.exists(off.marker)


# ---- memory.oom.group -----------------------------------------------------------------------------


def test_job_start_hook_sets_oom_group_on_the_scope_only_when_enabled(rig):
    d = rig.scope()
    rig.rt.on_job(UNIT, SCOPE)
    assert rig.read(d, "memory.oom.group") == "1"
    assert rig.read(rig.slice, "memory.max") == "max"  # the hook touches the scope, nothing else


def test_readopted_jobs_get_oom_group_on_the_next_tick(rig):
    d = rig.scope()
    rig.rt.tick()
    assert rig.read(d, "memory.oom.group") == "1"


# ---- detection ------------------------------------------------------------------------------------


def test_first_reading_is_the_baseline_then_an_increase_emits_one_backstop_event(rig):
    rig.kill_slice_counter(5, 2)  # kills that happened before the daemon started
    rig.scope(peak_mb=7000)
    rig.rt.tick()
    assert rig.events == [] and rig.rt.status()["oom_kills"] == 0
    rig.rt.tick()
    assert rig.events == []
    rig.kill_slice_counter(6, 3)
    shutil_rm = rig.root / SCOPE.lstrip("/")
    for f in shutil_rm.iterdir():
        f.unlink()
    shutil_rm.rmdir()
    del rig.jobs[UNIT]  # the killed scope is gone before the tick looks
    rig.rt.tick()
    assert len(rig.events) == 1
    kind, payload = rig.events[0]
    assert (
        kind == "backstop" and payload["scope"] == UNIT and payload["memory_peak_mb"] == 1500
    )  # the slice peak
    assert "OOM kill" in payload["message"]
    st = rig.rt.status()
    assert st["oom_kills"] == 1 and UNIT in st["last_event"] and "peak 1500 MB" in st["last_event"]
    assert rig.audits[-1]["op"] == "backstop" and rig.audits[-1]["result"] == "event"
    rig.rt.tick()
    assert len(rig.events) == 1  # the same counter does not fire twice


def test_a_live_scope_whose_own_counter_rose_is_named(rig):
    d = rig.scope(peak_mb=300)
    rig.rt.tick()
    rig.put(d, "memory.events", "oom_kill 1")
    rig.put(d, "memory.peak", 900 * MIB)
    rig.kill_slice_counter(1, 1)
    rig.rt.tick()
    assert rig.events[0][1]["scope"] == UNIT and rig.events[0][1]["memory_peak_mb"] == 900


def test_a_kill_with_no_scope_still_alerts_with_the_slice_peak(rig):
    rig.rt.tick()
    rig.kill_slice_counter(1, 0)
    rig.rt.tick()
    kind, payload = rig.events[0]
    assert payload["scope"].startswith("unknown") and payload["memory_peak_mb"] == 1500


def test_counters_that_restart_are_a_new_baseline_not_an_event(rig):
    rig.kill_slice_counter(9, 9)
    rig.rt.tick()
    rig.kill_slice_counter(0, 0)
    rig.rt.tick()
    assert rig.events == []


def test_disabled_runtime_does_not_detect(tmp_path):
    r = Rig(tmp_path, BackstopConfig())
    r.rt.tick()
    r.kill_slice_counter(3, 3)
    r.rt.tick()
    assert r.events == []


def test_max_and_swap_fail_counters_track_deltas_since_baseline_and_render_in_status(rig, fake_host):
    from test_loadctl import status as base_status

    from loadgov.cli import loadctl

    rig.put(rig.slice, "memory.events", "max 10\noom_kill 0\noom_group_kill 0")
    rig.put(rig.slice, "memory.swap.events", "max 20\nfail 20")
    rig.rt.tick()  # first tick takes the baseline
    assert rig.rt.status()["max_events"] == 0 and rig.rt.status()["swap_fail_events"] == 0
    st = base_status(fake_host("calm"))
    st["backstop"] = rig.rt.status()
    assert "max hit" not in loadctl.format_status(st) and "swap fail" not in loadctl.format_status(st)
    rig.put(rig.slice, "memory.events", "max 17\noom_kill 0\noom_group_kill 0")
    rig.put(rig.slice, "memory.swap.events", "max 32\nfail 32")
    rig.rt.tick()
    assert rig.rt.status()["max_events"] == 7 and rig.rt.status()["swap_fail_events"] == 12
    st["backstop"] = rig.rt.status()
    line = [ln for ln in loadctl.format_status(st).splitlines() if ln.startswith("  backstop ")][0]
    assert "7 max hit(s), 12 swap fail(s)" in line
    rig.put(rig.slice, "memory.events", "max 2\noom_kill 0\noom_group_kill 0")  # counter reset: new baseline
    rig.put(rig.slice, "memory.swap.events", "max 3\nfail 3")
    rig.rt.tick()
    assert rig.rt.status()["max_events"] == 7 and rig.rt.status()["swap_fail_events"] == 12


# ---- through the daemon ---------------------------------------------------------------------------


def test_daemon_status_carries_backstop_and_job_start_sets_the_group(fake_host, tmp_path):
    from loadgov.config import Config
    from loadgov.daemon.main import Daemon
    from loadgov.protocol import VERSION, validate_status

    host = fake_host("calm")
    scope = f"{SLICE}/build-work-proj-a.slice/{UNIT}"
    sl = os.path.join(host.cgroup, SLICE)
    os.makedirs(os.path.join(host.cgroup, scope))
    for name, val in (("memory.max", "max"), ("memory.swap.max", "max"), ("memory.current", "0"),
                      ("memory.events", "oom_kill 0")):  # fmt: skip
        with open(os.path.join(sl, name), "w") as f:
            f.write(val + "\n")
    with open(os.path.join(host.cgroup, scope, "memory.oom.group"), "w") as f:
        f.write("0\n")
    d = Daemon(host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
               compat_link=None, run_dir=str(tmp_path / "run"), config=Config(backstop=ON))  # fmt: skip
    os.makedirs(d.run_dir)
    d.sampler.prime(host.prev_stat)
    s = d.tick()
    assert validate_status(s) == []
    assert s["backstop"]["enabled"] is True and s["backstop"]["applied"] is True, s["backstop"]["note"]
    assert open(os.path.join(sl, "memory.max")).read().strip() == str(8192 * MIB)
    assert d.dispatch({"v": VERSION, "op": "status"})["status"]["backstop"]["memory_max_mb"] == 8192
    req = {"kind": "impl", "project": "proj-a", "label": "x", "unit": UNIT, "cgroup": "/" + scope,
           "pid": os.getpid(), "owner_pid": os.getpid()}  # fmt: skip
    r = d.dispatch({"v": VERSION, "op": "job_start", "args": req}, os.getpid())
    assert r["ok"] is True, r
    assert open(os.path.join(host.cgroup, scope, "memory.oom.group")).read().strip() == "1"
