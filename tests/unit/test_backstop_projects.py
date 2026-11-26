"""Per-project caps, the leaked-memory alert, tmpfs leftovers and the restored-cap rule (ILM-44), on a fake
cgroup tree, a fake /proc/self/mounts and a fake tmpfs directory: no host, no systemd."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_backstop_runtime import ON, SLICE, Rig

from loadgov.config import BackstopConfig, ConfigError, parse_config
from loadgov.daemon import leftovers
from loadgov.daemon.backstop_ops import MIB, RETRY_S

BW = "[backstop]\nenabled = true\nmemory_max_mb = 8192\nmemory_swap_max_mb = 1024\n"
BETA = "build-work-beta.slice"
BETA_WEB = "build-work-beta-web.slice"


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, ON)


def caps(default=4096, **over):
    projects = tuple(sorted((name.replace("_", "-"), v) for name, v in over.items()))
    return replace(ON, project_memory_max_mb=default, projects=projects)


def project(rig, unit, usage_mb=100, procs="", events="oom_kill 0", stat=None):
    """A project slice at its nested path (dashes nest), with its memory files."""
    from loadgov.cli.run import slice_cgroup

    d = rig.root / slice_cgroup(unit)
    d.mkdir(parents=True, exist_ok=True)
    for name, val in (("memory.max", "max"), ("memory.high", "max"), ("memory.current", usage_mb * MIB),
                      ("memory.events", events), ("cgroup.procs", procs),
                      ("memory.stat", stat or "anon 0\nfile 0\nshmem 0")):  # fmt: skip
        rig.put(d, name, val)
    return d


def setprop(unit, mx, high):
    return ["systemctl", "set-property", "--runtime", unit, f"MemoryMax={mx}M", f"MemoryHigh={high}M"]


def project_calls(rig):
    return [c for c in rig.systemd.calls if c[3] != "build-work.slice"]


# ---- config ---------------------------------------------------------------------------------------


def test_project_caps_and_leak_keys_are_off_or_defaulted():
    c = parse_config("").backstop
    assert (c.project_memory_max_mb, c.project_memory_high_pct, c.projects) == (0, 90, ())
    assert (c.leak_alert_mb, c.leak_alert_min, c.leftover_report_mb) == (1024, 5, 1024)


def test_project_overrides_come_from_the_project_tables():
    c = parse_config(BW + "project_memory_max_mb = 4096\n[projects.alpha]\nmemory_max_mb = 0\n"
                     "[projects.beta.kinds.impl]\nload1_max = 4\n").backstop  # fmt: skip
    assert c.project_memory_max_mb == 4096 and c.projects == (("alpha", 0), ("beta", None))


@pytest.mark.parametrize(
    "body, msg",
    [
        (BW + "project_memory_max_mb = 1023\n", "project_memory_max_mb: expected 0"),
        (BW + "project_memory_max_mb = 8193\n", "at most backstop.memory_max_mb"),
        ("[backstop]\nproject_memory_max_mb = 2048\n", "needs backstop.memory_max_mb"),
        (BW + "[projects.p]\nmemory_max_mb = 9000\n", "projects.p.memory_max_mb"),
        (BW + "[projects.p]\nmemory_max_mb = true\n", "projects.p.memory_max_mb"),
        (BW + "project_memory_high_pct = 101\n", "project_memory_high_pct"),
        (BW + "leak_alert_mb = 0\n", "leak_alert_mb"),
        (BW + "leak_alert_min = 0\n", "leak_alert_min"),
        (BW + "leftover_report_mb = -1\n", "leftover_report_mb"),
    ],
)
def test_project_cap_values_are_validated(body, msg):
    with pytest.raises(ConfigError, match=msg):
        parse_config(body)


# ---- caps -----------------------------------------------------------------------------------------


def test_caps_use_nesting_correct_unit_names_and_one_project_call_per_tick(tmp_path):
    r = Rig(tmp_path, caps(4096, beta=None, beta_web=6144))
    amb, beta = project(r, BETA_WEB), project(r, BETA)
    assert amb.parent == beta  # build-work-beta-web.slice sits under build-work-beta.slice
    r.rt.tick()
    assert r.systemd.verbs() == ["set-property", "set-property"]  # build-work.slice + one project
    r.rt.tick()
    assert project_calls(r) == [setprop(BETA_WEB, 6144, 5529), setprop(BETA, 4096, 3686)]
    assert r.read(amb, "memory.max") == str(6144 * MIB) and r.read(amb, "memory.high") == str(5529 * MIB)
    r.rt.tick()
    assert len(r.systemd.calls) == 3  # not again while the files hold the values
    st = r.rt.status()["projects"]
    assert st["beta-web"] == {"unit": BETA_WEB, "cap_mb": 6144, "high_mb": 5529, "capped_mb": None,
                                    "usage_mb": 100, "shmem_mb": 0, "file_mb": 0, "applied": True}  # fmt: skip
    assert json.load(open(r.rt.marker))["projects"] == {BETA_WEB: None, BETA: None}
    r.put(amb, "memory.max", "max")  # systemd re-realized it without the property
    r.rt.tick()
    assert project_calls(r)[-1] == setprop(BETA_WEB, 6144, 5529)


def test_an_inactive_configured_slice_gets_the_stored_property_and_counts_as_applied(tmp_path):
    r = Rig(tmp_path, caps(4096, alpha=None))
    r.rt.tick()
    assert project_calls(r) == [setprop("build-work-alpha.slice", 4096, 3686)]
    assert r.rt.status()["projects"]["alpha"]["applied"] is True
    assert "start" not in r.systemd.verbs()[1:]


def test_a_seen_unconfigured_slice_gets_the_default_and_an_override_of_0_is_no_cap(tmp_path):
    r = Rig(tmp_path, caps(4096, alpha=0))
    project(r, "build-work-alpha.slice")
    (project(r, "build-work-other.slice") / "loadgov-impl-x-1.scope").mkdir()
    r.rt.tick()
    r.rt.tick()
    assert project_calls(r) == [setprop("build-work-other.slice", 4096, 3686)]
    assert r.rt.status()["projects"]["alpha"]["cap_mb"] == 0


def test_project_caps_are_off_by_default(tmp_path):
    r = Rig(tmp_path, ON)
    (project(r, BETA) / "loadgov-impl-x-1.scope").mkdir()
    for _ in range(3):
        r.rt.tick()
    assert project_calls(r) == [] and r.rt.status()["projects"]["beta"]["cap_mb"] == 0


def test_flag_off_writes_nothing_and_only_rolls_back_what_the_marker_names(tmp_path):
    r = Rig(tmp_path, replace(caps(4096, beta=None), enabled=False))
    d = project(r, BETA)
    r.rt.tick()
    assert r.systemd.calls == [] and r.read(d, "memory.max") == "max" and not os.path.exists(r.rt.marker)
    on = r.runtime(caps(4096, beta=None), r.systemd)
    on.tick()
    assert r.read(d, "memory.max") == str(4096 * MIB)
    off = r.runtime(BackstopConfig(), r.systemd)  # a restart with the flag off
    off.tick()
    assert r.systemd.calls[-2][3] == "build-work.slice"  # build-work.slice first, then the project
    assert r.systemd.calls[-1] == ["systemctl", "set-property", "--runtime", BETA, "MemoryMax=infinity",
                                   "MemoryHigh=infinity"]  # fmt: skip
    assert r.read(d, "memory.max") == "max" and r.read(d, "memory.high") == "max"
    assert not os.path.exists(off.marker) and r.audits[-1]["result"] == "rollback"


def test_a_removed_cap_is_rolled_back_and_retried_until_it_succeeds(tmp_path):
    r = Rig(tmp_path, caps(4096, beta=None))
    project(r, BETA)
    r.rt.tick()
    r.rt.configure(caps(4096, beta=0))  # a reload sets the project's cap to 0
    r.systemd.rc = 1
    r.rt.tick()
    undo = ["systemctl", "set-property", "--runtime", BETA, "MemoryMax=infinity", "MemoryHigh=infinity"]
    assert project_calls(r)[-1] == undo and BETA in json.load(open(r.rt.marker))["projects"]
    assert r.rt.note.startswith("rollback incomplete") and r.audits[-1]["result"] == "failed"
    n = len(r.systemd.calls)
    r.t[0] += RETRY_S - 1
    r.rt.tick()
    assert len(r.systemd.calls) == n  # rate-limited
    r.systemd.rc = 0
    r.t[0] += 1
    r.rt.tick()
    assert project_calls(r)[-1] == undo and json.load(open(r.rt.marker))["projects"] == {}
    assert r.audits[-1]["result"] == "rollback"


def test_usage_above_a_project_cap_is_capped_above_it_with_one_alert(tmp_path):
    r = Rig(tmp_path, caps(4096, beta=None))
    d = project(r, BETA, usage_mb=5000)
    r.rt.tick()
    assert project_calls(r) == [setprop(BETA, 5512, 3686)]  # never at or below usage; throttled by high
    assert r.events == [("backstop_cap", r.events[0][1])] and r.events[0][1]["key"] == "backstop_cap:beta"
    assert r.rt.status()["projects"]["beta"]["capped_mb"] == 5512
    assert json.load(open(r.rt.marker))["projects"] == {BETA: 5512}
    r.put(d, "memory.current", 3000 * MIB)
    r.rt.tick()
    assert project_calls(r)[-1] == setprop(BETA, 4096, 3686) and len(r.events) == 1


def test_an_oom_kill_in_a_nested_project_slice_names_that_project(tmp_path):
    r = Rig(tmp_path, caps(4096, beta=None))
    beta, amb = project(r, BETA), project(r, BETA_WEB)
    r.rt.tick()  # the baseline
    r.put(amb, "memory.events", "oom_kill 2")
    r.put(beta, "memory.events", "oom_kill 2")  # hierarchical: the parent counts the child's kills
    r.put(amb, "memory.peak", 3000 * MIB)
    r.rt.tick()
    oom = [p for k, p in r.events if k == "backstop_oom"]
    assert len(oom) == 1 and oom[0]["key"] == "backstop_oom:beta-web"
    assert "2 process(es)" in oom[0]["message"] and oom[0]["memory_peak_mb"] == 3000
    r.rt.tick()
    assert len([k for k, _ in r.events if k == "backstop_oom"]) == 1


# ---- restored cap below usage (ILM-43 auditor note) -----------------------------------------------


def test_a_restored_cap_below_usage_starts_a_new_episode_not_a_cap_below_usage(rig):
    rig.put(rig.slice, "memory.current", 9000 * MIB)
    rig.rt.tick()
    assert rig.rt.capped_mb == 9512
    rig.put(rig.slice, "memory.max", "max")  # systemd re-realized the slice, then the daemon restarts
    rig.put(rig.slice, "memory.current", 9800 * MIB)
    again = rig.runtime(ON, rig.systemd)
    again.tick()
    assert rig.read(rig.slice, "memory.max") == str(10312 * MIB) and again.capped_mb == 10312
    assert [k for k, _ in rig.events] == ["backstop_cap", "backstop_cap"]  # a new episode


# ---- leaked memory --------------------------------------------------------------------------------


def leak_rig(tmp_path, procs=""):
    r = Rig(tmp_path, BackstopConfig())  # read-only: runs with the flag off too
    d = project(r, "build-work-alpha.slice", stat=f"anon 0\nfile {200 * MIB}\nshmem {2048 * MIB}")
    s = d / "loadgov-test-x-1.scope"
    s.mkdir()
    r.put(s, "cgroup.procs", procs)
    r.put(r.slice, "cgroup.procs", "")
    r.put(r.slice, "memory.stat", f"file {200 * MIB}\nshmem {2048 * MIB}")
    return r


def test_the_leak_alert_fires_once_after_the_minutes_with_no_process(tmp_path):
    r = leak_rig(tmp_path)
    r.rt.tick()
    r.t[0] += 4 * 60
    r.rt.tick()
    assert r.events == []
    r.t[0] += 60
    r.rt.tick()
    assert [k for k, _ in r.events] == ["backstop_leak"]  # the deepest slice only, not build-work too
    p = r.events[0][1]
    assert p["key"] == "backstop_leak:build-work-alpha.slice" and "2048 MB of shmem" in p["message"]
    assert "nothing was deleted" in p["message"] and r.audits[-1]["result"] == "leak"
    assert r.systemd.calls == []  # read-only
    r.t[0] += 600
    r.rt.tick()
    assert len(r.events) == 1
    assert r.rt.status()["projects"]["alpha"]["shmem_mb"] == 2048 and r.rt.status()["shmem_mb"] == 2048


def test_no_leak_alert_while_a_process_exists(tmp_path):
    r = leak_rig(tmp_path, procs="4242")
    for _ in range(4):
        r.rt.tick()
        r.t[0] += 300
    assert r.events == []


def test_page_cache_in_an_idle_slice_is_no_leak(tmp_path):
    r = leak_rig(tmp_path)
    stat = f"anon 0\nfile {8192 * MIB}\nshmem {100 * MIB}"  # a big reclaimable page cache, little shmem
    r.put(r.root / SLICE / BETA.replace("beta", "alpha"), "memory.stat", stat)
    r.put(r.slice, "memory.stat", stat)
    for _ in range(4):
        r.rt.tick()
        r.t[0] += 300
    assert r.events == [] and r.rt.status()["projects"]["alpha"]["file_mb"] == 8192  # information only


def test_status_line_shows_the_shmem_share_above_the_threshold(tmp_path, fake_host):
    from test_loadctl import status as base_status

    from loadgov.cli import loadctl
    from loadgov.protocol import validate_status

    r = leak_rig(tmp_path)
    r.rt.tick()
    st = base_status(fake_host("calm"))
    st["backstop"] = r.rt.status()
    assert [e for e in validate_status(st) if e.startswith("backstop")] == []
    text = loadctl.format_status(st)
    assert "backstop off, shmem 2048 MB in build-work.slice" in text
    assert "backstop projects: alpha shmem 2048 MB" in text


# ---- tmpfs leftovers ------------------------------------------------------------------------------


def tmpfs_rig(tmp_path, report_mb=1):
    r = Rig(tmp_path, replace(BackstopConfig(), leftover_report_mb=report_mb))
    (tmp_path / "proc" / "self" / "mounts").write_text(
        "/dev/sda1 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw,nosuid 0 0\ntmpfs /run/user\\0401000 tmpfs rw 0 0\n"
    )
    mnt = tmp_path / "mnt"
    (mnt / "tmp").mkdir(parents=True)
    (mnt / "run" / "user 1000").mkdir(parents=True)
    r.rt = r.runtime(r.rt.cfg, r.systemd, mount_root=str(mnt))
    return r, mnt / "tmp"


def fill(d, mb, mtime):
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "blob", "wb") as f:
        f.write(b"x" * (mb * MIB))  # real blocks: a sparse file would count 0
    os.utime(d, (mtime, mtime))


def job(unit, since, project="alpha", key=None):
    return SimpleNamespace(unit=unit, project=project, key=key, since=since)


def test_a_job_end_names_big_tmp_dirs_from_its_window_in_the_job_end_line(tmp_path):
    r, tmp = tmpfs_rig(tmp_path)
    t = r.t[0]
    fill(tmp / "tmp.big", 2, t - 50)
    fill(tmp / "tmp.small", 0, t - 50)
    fill(tmp / "tmp.old", 2, t - 10_000)  # before the window
    (tmp / "link").symlink_to(tmp_path / "proc")  # never followed
    ev = {}
    r.rt.job_ended(job("loadgov-test-a-1.scope", t - 100, key="ILM-1"), ev)
    assert [(f["path"], f["mb"]) for f in ev["leftovers"]] == [("/tmp/tmp.big", 2)]
    assert ev["leftovers"][0]["candidates"] == [{"unit": "loadgov-test-a-1.scope", "project": "alpha",
                                                 "key": "ILM-1"}]  # fmt: skip
    st = r.rt.status()["leftovers"]
    assert {f["path"] for f in st} == {"/tmp/tmp.big", "/tmp/tmp.old"}
    assert next(f for f in st if f["path"] == "/tmp/tmp.old")["candidates"] == []
    assert (tmp / "tmp.big" / "blob").exists()  # nothing is deleted


def test_overlapping_jobs_are_all_candidates(tmp_path):
    r, tmp = tmpfs_rig(tmp_path)
    t = r.t[0]
    fill(tmp / "shared", 2, t - 50)
    r.rt.job_ended(job("loadgov-test-a-1.scope", t - 100), {})
    r.jobs["loadgov-test-b-2.scope"] = job("loadgov-test-b-2.scope", t - 80, project="beta")
    ev = {}
    r.rt.job_ended(job("loadgov-test-c-3.scope", t - 60, project="web-app"), ev)
    units = [c["unit"] for c in ev["leftovers"][0]["candidates"]]
    assert units == ["loadgov-test-a-1.scope", "loadgov-test-c-3.scope", "loadgov-test-b-2.scope"]


def test_the_walk_is_bounded_per_step_and_resumes_with_a_job_leftovers_line(tmp_path, monkeypatch):
    monkeypatch.setattr(leftovers, "DIRS_PER_STEP", 2)
    r, tmp = tmpfs_rig(tmp_path)
    t = r.t[0]
    for i in range(5):
        (tmp / f"d{i}" / "a" / "b").mkdir(parents=True)
    fill(tmp / "d0", 2, t - 50)
    ev = {}
    r.rt.job_ended(job("loadgov-test-a-1.scope", t - 100), ev)
    assert "leftovers" not in ev  # not finished within one step
    for _ in range(20):
        r.rt.tick()
    line = next(a for a in r.audits if a["op"] == "job_leftovers")
    assert line["request"]["unit"] == "loadgov-test-a-1.scope"
    assert [f["path"] for f in line["leftovers"]] == ["/tmp/d0"]


def counting_scandir(monkeypatch):
    opened = []
    real = os.scandir

    def scandir(path):
        opened.append(path)
        return real(path)

    monkeypatch.setattr(leftovers.os, "scandir", scandir)
    return opened


def test_the_job_end_step_is_small_and_the_ticks_finish_the_scan(tmp_path, monkeypatch):
    r, tmp = tmpfs_rig(tmp_path)
    t = r.t[0]
    for i in range(30):
        (tmp / f"d{i:02}").mkdir()
    fill(tmp / "d00", 2, t - 50)
    opened = counting_scandir(monkeypatch)
    ev = {}
    r.rt.job_ended(job("loadgov-test-a-1.scope", t - 100), ev)
    assert "leftovers" not in ev
    assert len(opened) <= 1 + leftovers.JOB_END_DIRS  # the /tmp listing + at most 8 directories
    r.rt.tick()
    line = next(a for a in r.audits if a["op"] == "job_leftovers")
    assert [(f["path"], f["mb"]) for f in line["leftovers"]] == [("/tmp/d00", 2)]


def test_the_top_level_listing_of_a_mount_is_capped_per_step_and_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(leftovers, "LIST_PER_MOUNT", 4)
    r, tmp = tmpfs_rig(tmp_path)
    for i in range(10):
        (tmp / f"d{i}").mkdir()
    scan = r.rt.scan
    scan.trigger()
    assert scan.step() is False and len(scan._queue) == 4  # 4 of the 10 entries of /tmp listed
    assert scan.step() is False and len(scan._queue) == 4  # those 4 sized, the next 4 listed
    assert scan._listing is not None  # the open listing resumes where it stopped
    assert scan.step() is True and scan._listing is None and not scan._mounts


def test_the_job_end_step_stops_at_its_entry_budget_mid_directory(tmp_path):
    r, tmp = tmpfs_rig(tmp_path)
    t = r.t[0]
    big = tmp / "only"
    big.mkdir()
    for i in range(3000):
        (big / f"f{i}").write_bytes(b"")
    fill(big, 2, t - 50)
    ev = {}
    r.rt.job_ended(job("loadgov-test-a-1.scope", t - 100), ev)
    assert "leftovers" not in ev
    assert r.rt.scan._cur["entries"] < leftovers.JOB_END_ENTRIES  # 1 went to the /tmp listing
    r.rt.tick()
    line = next(a for a in r.audits if a["op"] == "job_leftovers")
    assert [(f["path"], f["mb"]) for f in line["leftovers"]] == [("/tmp/only", 2)]


def test_the_walk_stops_at_the_entry_bound_and_the_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(leftovers, "MAX_ENTRIES", 3)
    r, tmp = tmpfs_rig(tmp_path)
    (tmp / "many").mkdir()
    for i in range(5):
        (tmp / "many" / f"f{i}").write_bytes(b"x" * MIB)
    fill(tmp / "deep" / "a" / "b" / "c" / "d", 2, r.t[0])  # the blob sits at depth 5: not counted
    r.rt.scan.trigger()
    while not r.rt.scan.step():
        pass
    found = {f["path"]: f for f in r.rt.scan.leftovers}
    assert set(found) == {"/tmp/many"} and found["/tmp/many"]["partial"] is True
    assert 3 <= found["/tmp/many"]["mb"] < 5


def test_the_scanner_is_off_with_injected_roots_and_no_mount_root(rig):
    assert rig.rt.scan.enabled is False
    ev = {}
    rig.rt.job_ended(job("x.scope", 0.0), ev)
    assert ev == {} and rig.rt.status()["leftovers"] == []


def test_mounts_parse_only_tmpfs_and_unescape(tmp_path):
    r, _ = tmpfs_rig(tmp_path)
    assert leftovers.tmpfs_mounts(r.fs) == ["/tmp", "/run/user 1000"]
    assert SLICE == "build.slice/build-work.slice"


class Spy:
    """Wraps os.scandir and os.lstat of the scanner: every path it stats or opens is recorded."""

    def __init__(self, monkeypatch):
        self.touched = []
        real_scandir, real_lstat = os.scandir, os.lstat
        spy = self

        class Entry:
            def __init__(self, e):
                self._e, self.name, self.path = e, e.name, e.path

            def is_dir(self, follow_symlinks=True):
                spy.touched.append(self.path)
                return self._e.is_dir(follow_symlinks=follow_symlinks)

            def stat(self, follow_symlinks=True):
                spy.touched.append(self.path)
                return self._e.stat(follow_symlinks=follow_symlinks)

        class Listing:
            def __init__(self, path):
                spy.touched.append(str(path))
                self._it = real_scandir(path)

            def __iter__(self):
                return self

            def __next__(self):
                return Entry(next(self._it))

            def close(self):
                self._it.close()

        def lstat(path, *a, **k):
            spy.touched.append(str(path))
            return real_lstat(path, *a, **k)

        monkeypatch.setattr(leftovers.os, "scandir", Listing)
        monkeypatch.setattr(leftovers.os, "lstat", lstat)


def test_a_mount_point_below_a_tmpfs_is_never_stated_or_opened(tmp_path, monkeypatch):
    r, tmp = tmpfs_rig(tmp_path)
    (tmp_path / "proc" / "self" / "mounts").write_text(
        "tmpfs /tmp tmpfs rw 0 0\n"
        "app /tmp/.mount_app fuse.app ro 0 0\n"  # at the top level of the tmpfs
        "gvfsd-fuse /tmp/big/a/doc fuse.gvfsd-fuse rw 0 0\n"  # deeper inside a walked directory
    )
    fill(tmp / "big", 2, r.t[0])
    (tmp / ".mount_app").mkdir()
    (tmp / "big" / "a" / "doc").mkdir(parents=True)
    spy = Spy(monkeypatch)
    scan = r.rt.scan
    scan.trigger()
    while not scan.step():
        pass
    monkeypatch.undo()
    mounts = {str(tmp / ".mount_app"), str(tmp / "big" / "a" / "doc")}
    assert mounts.isdisjoint(spy.touched)  # a stuck FUSE or NFS mount could not block the tick
    assert str(tmp / "big" / "a") in spy.touched  # the walk itself went on around it
    assert [f["path"] for f in scan.leftovers] == ["/tmp/big"]


def test_opening_a_mount_listing_counts_against_the_dirs_budget(tmp_path):
    r, _ = tmpfs_rig(tmp_path)
    scan = r.rt.scan
    scan.trigger()
    assert scan.step(dirs=1) is False  # /tmp's listing used the one directory
    assert scan.step(dirs=0) is False and len(scan._mounts) == 1  # nothing opened without budget


# ---- an unreadable marker -------------------------------------------------------------------------


def test_an_unreadable_marker_rolls_back_build_work_only_and_stays_until_that_succeeded(tmp_path):
    r = Rig(tmp_path, BackstopConfig())
    project(r, BETA)
    r.put(r.slice, "memory.max", 8192 * MIB)
    (r.state / "backstop.json").write_text("{not json")
    r.systemd.rc = 1
    off = r.runtime(BackstopConfig(), r.systemd)
    assert r.audits[-1]["result"] == "failed" and "marker unreadable" in r.audits[-1]["reason"]
    off.tick()
    undo = ["systemctl", "set-property", "--runtime", "build-work.slice", "MemoryMax=infinity",
            "MemorySwapMax=infinity"]  # fmt: skip
    assert r.systemd.calls == [undo]  # project slices are unknown: left alone
    assert os.path.exists(off.marker) and off.note.startswith("rollback incomplete")
    r.systemd.rc = 0
    r.t[0] += RETRY_S
    off.tick()
    assert r.systemd.calls == [undo, undo] and r.read(r.slice, "memory.max") == "max"
    assert not os.path.exists(off.marker) and r.audits[-1]["result"] == "rollback"
