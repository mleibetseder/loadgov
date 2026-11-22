"""Light overrun reports whichever side ends the job first: tick, loadctl, or a job that never registered."""

import subprocess

import pytest
from test_headroom import make_daemon
from test_light_lane import add_jobs, go, light_run

from loadgov.cli import loadctl
from loadgov.engines.jobs import RECENT_MAX, RECENT_S, Job
from loadgov.protocol import VERSION


@pytest.fixture(name="procs")
def _procs():
    started = []

    def spawn():
        p = subprocess.Popen(["sleep", "60"])
        started.append(p)
        return p

    yield spawn
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()


def _end(d, unit, peer="unit", **extra):
    """job_end overrun as the loadctl run whose pid ends the unit name (`peer` overrides it)."""
    if peer == "unit":
        tail = unit.removesuffix(".scope").rsplit("-", 1)[-1]
        peer = int(tail) if tail.isdigit() else None
    req = {"v": VERSION, "op": "job_end", "args": {"unit": unit, "overrun": "runtime", **extra}}
    return d.dispatch(req, peer)


def _over(d):
    return [e for e in d.audit.tail(200) if e["op"] == "light_overrun"]


def _counted(d, project="web-app"):
    return len(d.watch._overruns.get(project, []))


def test_tick_first_then_a_late_job_end_overrun_is_audited_and_counted_once(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    unit = next(iter(d.jobs.jobs))
    d.jobs.end(unit, "scope empty")
    assert _over(d) == [] and _counted(d) == 0
    r = _end(d, unit)
    assert r["ok"] and r["ended"] is False
    assert len(_over(d)) == 1 and _over(d)[0]["reason"] == "light_overrun: runtime 600 s exceeded"
    assert _counted(d) == 1
    _end(d, unit)  # a repeat adds nothing
    assert len(_over(d)) == 1 and _counted(d) == 1
    assert [s["count"] for s in d.jobs.stats.summary()] == [1]  # job_stats not double counted


def test_job_end_first_audits_once_and_the_tick_adds_nothing(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    unit = next(iter(d.jobs.jobs))
    assert _end(d, unit)["ended"] is True
    d.jobs.tick()
    assert len(_over(d)) == 1 and _counted(d) == 1
    assert _end(d, unit)["ended"] is False  # a duplicate report
    assert len(_over(d)) == 1 and _counted(d) == 1
    assert [s["count"] for s in d.jobs.stats.summary()] == [1]


def test_a_never_registered_overrun_is_audited_once(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    unit = "loadgov-light-lint-20260101000000-7.scope"
    extra = {"kind": "light", "project": "web-app", "label": "lint"}
    assert _end(d, unit, **extra)["ended"] is False
    assert _end(d, unit, **extra)["ok"]
    over = _over(d)
    assert len(over) == 1 and over[0]["project"] == "web-app" and over[0]["request"]["label"] == "lint"
    assert _counted(d) == 1
    assert d.jobs.stats.summary() == []  # no job bookkeeping


def test_an_unregistered_report_needs_a_light_unit_project_and_label(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    _end(d, "loadgov-light-x-1.scope", kind="light", project="web-app")  # no label
    _end(d, "loadgov-light-x-2.scope", kind="impl", project="web-app", label="x")
    _end(d, "loadgov-impl-x-3.scope", kind="light", project="web-app", label="x")
    _end(d, "loadgov-light-x-4.scope", kind="light", project="web-app", label=5)
    _end(d, "loadgov-light-x-5.scope")
    assert _over(d) == [] and _counted(d) == 0


def test_a_registered_non_light_job_ignores_the_unregistered_fields(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    unit = "loadgov-impl-x-1.scope"
    d.jobs.jobs[unit] = Job("impl", "web-app", "x", unit, f"/x/{unit}", 1000, 1, 1000, None, d.t)
    _end(d, unit, kind="light", project="other", label="y")
    assert _over(d) == [] and _counted(d, "other") == 0


def test_three_overruns_over_mixed_paths_raise_the_alert(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 2)
    first, second = list(d.jobs.jobs)
    d.jobs.end(first, "scope empty")
    _end(d, first)  # tick first
    _end(d, second)  # loadctl first
    _end(d, "loadgov-light-z-9.scope", kind="light", project="web-app", label="z")  # never registered
    assert _counted(d) == 3 and len(_over(d)) == 3
    assert "light_overrun:web-app" in d.watch._overrun_alerts(d.clock())


def test_the_recent_ended_store_is_bounded_by_count_and_age(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, RECENT_MAX + 5)
    for u in list(d.jobs.jobs):
        d.jobs.end(u)
    assert len(d.jobs.recent) == RECENT_MAX
    assert d.jobs.late_overrun("loadgov-light-web-app-0-0.scope") is None  # the oldest was dropped
    d.t += RECENT_S + 1
    last = list(d.jobs.recent)[-1]
    assert d.jobs.late_overrun(last) is None  # too old


def test_loadctl_reports_the_overrun_when_job_start_failed_and_keeps_the_exit_code(
    fake_host, tmp_path, monkeypatch, procs, capsys
):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=-9)
    h.deps.oom_kills = lambda cg: 1
    real = loadctl.request

    def fail_start(sock, op, args=None, **kw):
        if op == "job_start":
            return {"ok": False, "error": "invalid_args", "message": "job pid 1 is not running"}
        return real(sock, op, args, **kw)

    monkeypatch.setattr(loadctl, "request", fail_start)
    assert go(h) == 76
    assert "job not registered" in capsys.readouterr().err
    assert len(_over(d)) == 1 and _counted(d) == 1


def test_a_failed_overrun_report_never_changes_the_exit_code(fake_host, tmp_path, monkeypatch, procs):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=-9)
    h.deps.oom_kills = lambda cg: 1
    real = loadctl.request

    def boom(sock, op, args=None, **kw):
        if op == "job_end":
            raise OSError("socket gone")
        return real(sock, op, args, **kw)

    monkeypatch.setattr(loadctl, "request", boom)
    assert go(h) == 76


LIGHT = {"kind": "light", "project": "web-app", "label": "lint"}


def test_an_unregistered_report_from_another_peer_is_refused_and_not_counted(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    unit = "loadgov-light-lint-20260101000000-7.scope"
    for peer in (8, None):
        r = _end(d, unit, peer=peer, **LIGHT)
        assert r["ok"] is False and r["error"] == "owner_denied"
    assert _over(d) == [] and _counted(d) == 0
    refused = [e for e in d.audit.tail(50) if e.get("result") == "refused" and e["op"] == "job_end"]
    assert len(refused) == 2 and "peer is not the loadctl run" in refused[0]["reason"]
    assert _end(d, unit, peer=7, **LIGHT)["ok"]  # the right peer still gets through
    assert len(_over(d)) == 1 and _counted(d) == 1


def test_an_evicted_or_forgotten_registered_job_is_counted_once_by_its_own_loadctl(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    unit = next(iter(d.jobs.jobs))
    d.jobs.end(unit, "scope empty")
    for i in range(RECENT_MAX):  # 64 later jobs push it out of the recent store
        d.jobs._remember(Job("impl", "p", "x", f"loadgov-impl-x-{i}.scope", "", 1, None, 1, None, d.t), True)
    assert unit not in d.jobs.recent
    # the daemon-made unit name of add_jobs has no pid suffix; use a loadctl-style one for the report
    unit = "loadgov-light-lint-20260101000000-4242.scope"
    assert _end(d, unit, **LIGHT)["ok"]
    assert _end(d, unit, **LIGHT)["ok"]  # repeated: neither registered nor refused, already booked
    assert len(_over(d)) == 1 and _counted(d) == 1
    d2 = make_daemon(fake_host("calm"), tmp_path / "restart")  # a restart: nothing remembered
    assert _end(d2, unit, **LIGHT)["ok"] and len(_over(d2)) == 1


def test_a_recent_unit_is_not_counted_again_and_not_refused_for_a_wrong_peer(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 1)
    unit = next(iter(d.jobs.jobs))
    assert _end(d, unit, peer=1000, **LIGHT)["ended"] is True  # registered path: no pid rule
    r = _end(d, unit, peer=1, **LIGHT)
    assert r["ok"] and len(_over(d)) == 1 and _counted(d) == 1


def test_loadctl_sends_kind_project_and_label_with_every_overrun_report(
    fake_host, tmp_path, monkeypatch, procs
):
    _, d, h, _ = light_run(monkeypatch, fake_host, tmp_path, procs, code=-9)
    h.deps.oom_kills = lambda cg: 1
    real, seen = loadctl.request, []

    def spy(sock, op, args=None, **kw):
        if op == "job_end":
            seen.append(args)
        return real(sock, op, args, **kw)

    monkeypatch.setattr(loadctl, "request", spy)
    assert go(h) == 76
    assert len(seen) == 1
    assert {k: seen[0][k] for k in ("overrun", "kind", "project", "label")} == {
        "overrun": "memory", "kind": "light", "project": "web-app", "label": "lint"
    }  # fmt: skip
    assert len(_over(d)) == 1 and _counted(d) == 1
