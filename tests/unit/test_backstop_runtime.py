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


# ---- rig ------------------------------------------------------------------------------------------


class Rig:
    def __init__(self, tmp_path, cfg):
        self.root = tmp_path / "cg"
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.fs = HostFS(cgroup_root=str(self.root))
        self.events, self.audits, self.jobs = [], [], {}
        self.slice = self.root / SLICE
        self.slice.mkdir(parents=True)
        self.put(self.slice, "memory.max", "max")
        self.put(self.slice, "memory.swap.max", "max")
        self.put(self.slice, "memory.current", 1000 * MIB)
        self.put(self.slice, "memory.peak", 1500 * MIB)
        self.put(self.slice, "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0")
        self.rt = BackstopRuntime(
            self.fs, cfg, lambda: 1_762_800_000.0, str(self.state),
            lambda kind, payload: self.events.append((kind, payload)), self.audits.append, lambda: self.jobs,
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
    assert json.load(open(rig.rt.marker)) == {"slice": SLICE}
    assert rig.rt.status()["applied"] is True and rig.rt.status()["note"] is None
    rig.rt.tick()
    assert [a["result"] for a in rig.audits] == ["applied"]  # not again while nothing changes


def test_a_changed_value_is_reasserted_and_max_is_never_written_while_enabled(rig):
    rig.rt.tick()
    rig.put(rig.slice, "memory.max", "max")  # somebody lifted it by hand
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB)


def test_memory_max_is_deferred_while_usage_is_not_below_it(rig):
    rig.put(rig.slice, "memory.current", 9000 * MIB)
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == "max"  # the kernel would OOM-kill running builds
    assert rig.read(rig.slice, "memory.swap.max") == str(1024 * MIB)
    st = rig.rt.status()
    assert st["applied"] is False and st["note"].startswith("deferred: usage 9000 MB")
    rig.put(rig.slice, "memory.current", 3000 * MIB)
    rig.rt.tick()
    assert rig.read(rig.slice, "memory.max") == str(8192 * MIB) and rig.rt.status()["applied"] is True


def test_a_missing_slice_or_unwritable_marker_writes_nothing(tmp_path):
    r = Rig(tmp_path, ON)
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
        cgroup.clear_memory_limit(rig.fs, SLICE, "memory.high")
    with pytest.raises(FileNotFoundError):  # never creates a file
        cgroup.write_oom_group(rig.fs, "/nowhere/x.scope")


# ---- rollback -------------------------------------------------------------------------------------


def test_rollback_writes_max_back_once_and_removes_the_marker(rig, tmp_path):
    rig.rt.tick()
    off = BackstopRuntime(
        rig.fs, BackstopConfig(), lambda: 1.0, str(rig.state), lambda *a: None, rig.audits.append, lambda: {}
    )  # a restart without the table
    off.tick()
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
