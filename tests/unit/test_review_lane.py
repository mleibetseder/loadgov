"""The review lane: reviews check only the memory floor and max_reviewers, never load or PSI."""

import pytest
from test_headroom import GB, Sleeper, hr, make_daemon, wire

from loadgov.cli import loadctl
from loadgov.config import (
    BUILTIN_PROFILES,
    HEADROOM_DEFAULTS,
    ConfigError,
    HeadroomConfig,
    parse_config,
    thresholds,
)
from loadgov.engines.jobs import Job

BUSY = {"load1": 9.0, "load5": 9.0, "cpu_psi": (0.0, 80.0), "mem_psi": (0.0, 30.0, 0.0)}


def add_jobs(d, n, kind="review", project="proj-a"):
    for i in range(n):
        unit = f"loadgov-{kind}-{project}-{len(d.jobs.jobs)}-{i}.scope"
        d.jobs.jobs[unit] = Job(kind, project, "r", unit, f"/x/{unit}", 1000 + i, 1, 1000 + i, None, d.t)


def test_profile_defaults_and_range():
    got = {n: p.max_reviewers for n, p in BUILTIN_PROFILES.items()}
    assert got == {"silent": 1, "balanced": 3, "boost": 4, "drain": 0}
    assert parse_config("[profiles.balanced]\nmax_reviewers = 8\n").profiles["balanced"].max_reviewers == 8
    for bad in ("9", "-1", "2.5", "true"):
        with pytest.raises(ConfigError, match="max_reviewers"):
            parse_config(f"[profiles.balanced]\nmax_reviewers = {bad}\n")


def test_review_default_memory_floor_is_2gb_and_project_values_still_apply():
    h = HeadroomConfig()
    assert thresholds(h, "review", None, 6).mem_min_gb == 2
    assert thresholds(h, "impl", None, 6).mem_min_gb == HEADROOM_DEFAULTS["mem_min_gb"]
    c = parse_config("[projects.p.kinds.review]\nmem_min_gb = 3\npoll_s = 5\nload1_max = 1\n")
    t = thresholds(c.headroom, "review", "p", 6)
    assert (t.mem_min_gb, t.poll_s) == (3, 5)


def test_review_starts_despite_high_load_and_psi(fake_host, tmp_path):
    d = make_daemon(fake_host("busy", **BUSY), tmp_path)
    assert hr(d, "impl")["decision"]["pass"] is False
    r = hr(d, "review", "other")["decision"]
    assert r["pass"] is True and r["reasons"] == []
    assert r["reason"].startswith("review lane: 0 of 3 reviewers running, ")


def test_review_blocked_by_the_cap_across_projects_and_reason_names_the_lane(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    add_jobs(d, 2, project="proj-a")
    add_jobs(d, 1, project="proj-b")
    add_jobs(d, 5, kind="impl")  # other kinds do not count
    r = hr(d, "review", "proj-c")["decision"]
    assert r["pass"] is False and r["reasons"] == ["review lane: 3 of 3 reviewers running"]
    del d.jobs.jobs[next(u for u, j in d.jobs.jobs.items() if j.kind == "review")]
    ok = hr(d, "review", "proj-c")["decision"]
    assert ok["pass"] is True and ok["reason"].startswith("review lane: 2 of 3 reviewers running, ")


def test_review_blocked_by_the_memory_floor(fake_host, tmp_path):
    low = make_daemon(fake_host("calm", avail_mb=3 * GB - 1), tmp_path)
    r = hr(low, "review", "other")["decision"]
    assert r["pass"] is False and r["reasons"][0].startswith("mem available 3.0 GB < 3 GB")
    assert hr(make_daemon(fake_host("calm", avail_mb=3 * GB), tmp_path), "review", "other")["decision"][
        "pass"
    ]


def test_drain_blocks_reviews_and_silent_allows_one(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.limits.set("owner", {}, "t", profile="drain")
    r = hr(d, "review", "other")["decision"]
    assert r["pass"] is False and r["reasons"] == ["review lane: 0 of 0 reviewers running"]
    d.limits.set("owner", {}, "t", profile="silent")
    assert hr(d, "review", "other")["decision"]["pass"] is True
    add_jobs(d, 1)
    assert hr(d, "review", "other")["decision"]["pass"] is False


def test_impl_and_gate_are_unchanged_by_the_lane(fake_host, tmp_path):
    d = make_daemon(fake_host("busy", **BUSY), tmp_path)
    for kind in ("impl", "gate", "compile"):
        r = hr(d, kind, "other")["decision"]
        assert r["pass"] is False and r["reason"] == "" and any(x.startswith("load1") for x in r["reasons"])


def test_owner_hold_does_not_stop_reviews(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.limits.set_flag("hold", "gate running")
    assert hr(d, "review", "other")["decision"]["pass"] is True
    assert hr(d, "gate", "other")["decision"]["pass"] is False


def test_agent_raise_refused_under_a_human_restriction_lower_is_free(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    assert d.limits.set("agent", {"max_reviewers": 2}, "less")["accepted"]
    d.limits.reset("r", all_=True)
    d.limits.ceiling_set("silent", "owner at home")
    r = d.limits.set("agent", {"max_reviewers": 3}, "more")
    assert r["refused"] is True and r["restrictedBy"] == "owner"
    assert d.limits.effective()["max_reviewers"] == 1


def test_set_op_and_status_carry_max_reviewers(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    r = d.dispatch({"v": 1, "op": "set", "args": {"max_reviewers": 2, "reason": "t"}})
    assert r["ok"] and r["effective"]["max_reviewers"] == 2
    assert d.dispatch({"v": 1, "op": "set", "args": {"max_reviewers": 9, "reason": "t"}})["ok"] is False
    assert "reviewers 2" in loadctl._lim(d.limits.effective())


def test_wait_for_a_review_passes_on_a_busy_host_and_prints_the_lane(
    fake_host, tmp_path, monkeypatch, capsys
):
    wire(monkeypatch, make_daemon(fake_host("busy", **BUSY), tmp_path))
    assert loadctl.main(["wait", "--kind", "review", "--label", "rv"], sleep=Sleeper()) == 0
    assert capsys.readouterr().out.startswith("HEADROOM_OK rv review lane: 0 of 3 reviewers running, ")


def test_review_floor_is_never_below_the_profile_ram_floor(fake_host, tmp_path):
    d = make_daemon(
        fake_host("calm", avail_mb=int(2.5 * GB)), tmp_path
    )  # review default 2 GB, ram floor 3 GB
    r = hr(d, "review", "other")["decision"]
    assert r["pass"] is False and r["reasons"] == ["mem available 2.5 GB < 3 GB"]
    assert hr(make_daemon(fake_host("calm", avail_mb=3 * GB), tmp_path), "review", "other")["decision"][
        "pass"
    ]


def test_no_review_starts_while_the_memory_guard_is_pausing_work(fake_host, tmp_path):
    d = make_daemon(fake_host("calm"), tmp_path)
    d.memguard.engine.paused = [object()]
    r = hr(d, "review", "other")["decision"]
    assert r["pass"] is False and r["reasons"] == ["memory guard is pausing work"]
    d.memguard.engine.paused = []
    assert hr(d, "review", "other")["decision"]["pass"] is True
