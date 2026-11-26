"""The image train: window math, queue rules, owner `now`, persistence, runner, ETA, alerts, status.

Fakes only: a fake /proc tree, a fake clock and time zone, a fake launcher for the runner."""

import argparse
import calendar
import dataclasses
import json
import os
import re
import subprocess
import time

import pytest
from conftest import parity_config
from test_run import Host, go, make_daemon, wire

from loadgov.cli import image, loadctl, run
from loadgov.config import KINDS, ConfigError, ImageConfig, parse_config
from loadgov.daemon.main import Daemon
from loadgov.engines import headroom
from loadgov.engines.image import find_builds, first_open_at_or_after, instances, window_state
from loadgov.engines.jobs import Job
from loadgov.engines.watch import Watch
from loadgov.host.procfs import HostFS
from loadgov.protocol import VERSION, validate_status


def ts(text: str) -> float:
    """'2025-11-11 00:10' UTC as epoch seconds."""
    return float(calendar.timegm(time.strptime(text, "%Y-%m-%d %H:%M")))


def hm(t: float) -> str:
    return time.strftime("%H:%M", time.gmtime(t)) if t else "-"


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


NIGHT = ts("2025-11-11 00:10")  # inside the default window 00:00-06:00 UTC
DAY = ts("2025-11-10 19:20")
RUNNER, AGENT, OWNER = 500, 610, 501  # pids of the fake /proc below


def add_proc(root, pid, cgroup, cmdline, ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    add_proc(root, 1, "/init.scope", "init")
    add_proc(root, RUNNER, "/system.slice/loadgov-image-runner.service", "loadctl image runner", 1)
    add_proc(root, OWNER, "/user.slice/session-1.scope", "loadctl image now", 1)
    add_proc(root, AGENT, "/build.slice/build-work.slice/build-work-web-app.slice/x.scope", "claude", 1)
    add_proc(root, 700, "/user.slice/session-1.scope", "docker build -t img --build-arg TOKEN=s3cret .", 1)
    add_proc(root, 701, "/user.slice/session-1.scope", "docker run --rm img build", 1)
    return root


class Clock:
    t = NIGHT

    def __call__(self):
        return self.t


def alive(pid):
    return f"{pid} (x) S " + " ".join(["1"] * 18) + " 100"


@pytest.fixture
def procs():
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


def new_daemon(tmp_path, proc, clock, **image_kw):
    fs = HostFS(proc_root=str(proc), cgroup_root=str(tmp_path / "cg"), lock_root=str(tmp_path / "lock"))
    cfg = parity_config(image=ImageConfig(**image_kw))
    d = Daemon(
        fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None, config=cfg,
        clock=clock,
    )  # fmt: skip
    d.slots.stat_reader = alive  # the fake pids of the runner are "running"
    return d


@pytest.fixture
def clock():
    c = Clock()
    c.t = NIGHT
    return c


@pytest.fixture
def d(tmp_path, proc, clock):
    return new_daemon(tmp_path, proc, clock)


def call(d, op, pid=AGENT, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args}, pid)


def submit(d, project="web-app", label="build", channel="release", pid=AGENT, **kw):
    args = {"project": project, "label": label, "channel": channel, "argv": ["make", "image"], "cwd": "/srv/x",
            "env": {"PATH": "/usr/bin", "SECRET": "no", "HOME": "/root"}, **kw}  # fmt: skip
    return call(d, "image_submit", pid, **args)


def audit(d):
    return [json.loads(line) for line in open(d.audit.path)]


def next_job(d, pid=RUNNER):
    return call(d, "image_next", pid)["job"]


def done(d, job, code=0, pid=RUNNER, **kw):
    return call(d, "image_done", pid, id=job["id"], exit_code=code, duration_s=60, **kw)


# ---- window math ----------------------------------------------------------------------------------


def test_default_window_in_and_out():
    cfg = ImageConfig()
    assert window_state(cfg, ts("2025-11-11 00:00"))[0] is True
    assert window_state(cfg, ts("2025-11-11 05:59"))[0] is True
    inside, start, end, nxt = window_state(cfg, ts("2025-11-11 06:00"))
    assert (inside, start, end) == (False, None, None) and nxt == ts("2025-11-12 00:00")
    assert window_state(cfg, NIGHT)[1:3] == (ts("2025-11-11 00:00"), ts("2025-11-11 06:00"))


def test_window_crossing_midnight():
    cfg = ImageConfig(window="22:00-02:00")
    for t in ("2025-11-10 23:30", "2025-11-11 01:59"):
        inside, start, end, _ = window_state(cfg, ts(t))
        assert inside and (start, end) == (ts("2025-11-10 22:00"), ts("2025-11-11 02:00"))
    inside, _, _, nxt = window_state(cfg, ts("2025-11-11 03:00"))
    assert not inside and nxt == ts("2025-11-11 22:00")


def test_window_follows_wall_clock_across_dst():
    cfg = ImageConfig(timezone="America/New_York")  # 00:00-06:00 local

    def night(day):  # the instance whose local start is that date
        return next(
            (s, e)
            for s, e in instances(cfg, ts(f"{day} 12:00"))
            if time.gmtime(s).tm_mday in (int(day[-2:]),)
        )

    assert [e - s for s, e in (night("2025-03-08"), night("2025-03-10"))] == [6 * 3600, 6 * 3600]
    spring = [(s, e) for s, e in instances(cfg, ts("2025-03-09 12:00")) if s == ts("2025-03-09 05:00")]
    fall = [(s, e) for s, e in instances(cfg, ts("2025-11-02 12:00")) if s == ts("2025-11-02 04:00")]
    assert [e - s for s, e in spring] == [5 * 3600] and [e - s for s, e in fall] == [7 * 3600]
    assert window_state(cfg, ts("2025-03-09 09:59"))[0] and not window_state(cfg, ts("2025-03-09 10:00"))[0]
    assert window_state(cfg, ts("2025-11-02 10:59"))[0] and not window_state(cfg, ts("2025-11-02 11:00"))[0]


def test_first_open_at_or_after():
    cfg = ImageConfig()
    assert first_open_at_or_after(cfg, ts("2025-11-11 03:00")) == ts("2025-11-11 03:00")
    assert first_open_at_or_after(cfg, ts("2025-11-11 07:00")) == ts("2025-11-12 00:00")


# ---- config ---------------------------------------------------------------------------------------


def test_image_config_defaults_and_validation():
    c = parse_config("").image
    assert (c.window, c.timezone, c.beta_interval_h, c.max_runtime_min, c.max_per_night) == (
        "00:00-06:00", "UTC", 48, 90, 6)  # fmt: skip
    assert c.env_allow == ("PATH", "HOME", "LANG", "DOCKER_HOST", "DOCKER_CONFIG", "LOADGOV_KEY")
    ok = parse_config(
        '[image]\nwindow = "22:30-02:15"\ntimezone = "America/New_York"\nenv_allow = ["PATH"]\n'
    )
    assert (ok.image.window, ok.image.env_allow) == ("22:30-02:15", ("PATH",))
    bad = [
        'window = "0:00-6:00"', 'window = "00:00-24:00"', 'window = "03:00-03:00"', "window = 5",
        'timezone = "Mars/Base"', "timezone = 1", "beta_interval_h = 0", "beta_interval_h = 1.5",
        "max_runtime_min = 4", "max_per_night = 51", 'env_allow = ["A B"]', 'env_allow = "PATH"', "color = 1",
    ]  # fmt: skip
    for line in bad:
        with pytest.raises(ConfigError, match="image"):
            parse_config(f"[image]\n{line}\n")


def test_image_is_a_kind_that_takes_the_heavy_slot_and_obeys_hold():
    assert "image" in KINDS and run.KIND_SLOT["image"] == "heavy"
    assert "image" in headroom.HOLD_KINDS
    assert headroom.paused_reasons(None, "image", "owner said stop", None) == ["owner hold: owner said stop"]
    assert headroom.paused_reasons(None, "light", "owner said stop", None) == []


def test_reload_makes_the_window_live(tmp_path, proc, clock):
    cfg_file = tmp_path / "loadgov.toml"
    cfg_file.write_text('[image]\nwindow = "00:00-06:00"\n')
    d = new_daemon(tmp_path, proc, clock)
    d.config_path = str(cfg_file)
    assert d.image_status()["window"] == "00:00-06:00" and d.image_status()["in_window"]
    cfg_file.write_text('[image]\nwindow = "12:00-13:00"\nmax_per_night = 2\n')
    d.reload_config()
    st = d.image_status()
    assert (st["window"], st["in_window"], st["max_per_night"]) == ("12:00-13:00", False, 2)


# ---- submit, list, persistence --------------------------------------------------------------------


def test_submit_records_the_job_with_an_allowlisted_env_and_audit(d):
    r = submit(
        d, key="PROJ-42", env={"PATH": "/usr/bin", "SECRET": "no", "HOME": "/h", "DOCKER_HOST": "tcp://x"}
    )
    assert r["ok"] and r["id"] == 1 and r["position"] == 1 and r["state"] == "queued"
    assert r["planned_start"] == stamp(NIGHT)  # inside the window: now
    job = d.image.get(1)
    assert job.env == {"PATH": "/usr/bin", "HOME": "/h", "DOCKER_HOST": "tcp://x"}  # SECRET is not kept
    assert (job.argv, job.cwd, job.key, job.channel) == (["make", "image"], "/srv/x", "PROJ-42", "release")
    assert job.submitter["pid"] == AGENT and job.submitter["principal"] == "agent"
    line = audit(d)[-1]
    assert (line["op"], line["result"], line["request"]["argv0"]) == ("image_submit", "accepted", "make")
    assert line["request"]["env_names"] == ["DOCKER_HOST", "HOME", "PATH"]
    assert "/usr/bin" not in json.dumps(line) and "tcp://x" not in json.dumps(line)  # values stay out


def test_env_allow_is_configurable(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock, env_allow=("PATH", "CUSTOM_VAR"))
    submit(d, env={"PATH": "/p", "HOME": "/h", "CUSTOM_VAR": "v"})
    assert d.image.get(1).env == {"PATH": "/p", "CUSTOM_VAR": "v"}


@pytest.mark.parametrize(
    "kw, error",
    [
        ({"channel": "nightly"}, "invalid_args"), ({"channel": 3}, "bad_request"), ({"argv": []}, "invalid_args"),
        ({"argv": "make"}, "bad_request"), ({"cwd": "rel/dir"}, "invalid_args"), ({"key": "nokey"}, "invalid_args"),
        ({"env": {"PATH": 1}}, "bad_request"), ({"project": ""}, "invalid_args"),
    ],
)  # fmt: skip
def test_submit_validation(d, kw, error):
    assert submit(d, **kw)["error"] == error
    assert d.image.jobs == []


def test_list_and_position_in_submission_order(d):
    for label in ("a", "b", "c"):
        submit(d, label=label)
    q = call(d, "image_list")["image"]["queue"]
    assert [(r["id"], r["position"], r["label"]) for r in q] == [(1, 1, "a"), (2, 2, "b"), (3, 3, "c")]


def test_queue_is_persisted_across_a_restart(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock)
    submit(d, project="web-app", label="b1", channel="beta", key="PROJ-1")
    submit(d, project="api", label="r1")
    job = next_job(d)  # b1 starts: beta interval and night count are recorded
    clock.t = NIGHT + 60
    d2 = new_daemon(tmp_path, proc, clock)
    assert [(j.id, j.label, j.state) for j in d2.image.jobs] == [(1, "b1", "running"), (2, "r1", "queued")]
    assert d2.image.get(1).env == job["env"] and d2.image.get(1).key == "PROJ-1"
    assert d2.image.last_beta == {"web-app": NIGHT} and d2.image.next_id == 3
    assert submit(d2, project="web-app", channel="beta")["error"] == "refused"  # the interval survived
    assert submit(d2, project="api")["id"] == 3


# ---- beta interval --------------------------------------------------------------------------------


def test_beta_is_refused_inside_the_interval_with_the_next_allowed_time(d, clock):
    submit(d, channel="beta", label="b1")
    assert next_job(d)["label"] == "b1"
    done(d, {"id": 1})
    clock.t = NIGHT + 3600
    r = submit(d, channel="beta", label="b2")
    assert r["error"] == "refused" and "interval is 48 h" in r["message"]
    assert r["next_allowed"] == stamp(ts("2025-11-13 00:10"))  # 48 h after the start, window open then
    assert audit(d)[-1]["result"] == "refused"
    assert submit(d, channel="beta", project="api")["ok"]  # another project
    assert submit(d, channel="release", label="r1")["ok"]  # releases are window only
    clock.t = ts("2025-11-13 00:11")
    assert submit(d, channel="beta", label="b3")["ok"]


def test_next_allowed_moves_to_the_next_window_when_the_interval_ends_outside_it(d, clock):
    submit(d, channel="beta")
    next_job(d)
    clock.t = NIGHT + 7200
    assert submit(d, channel="beta")["next_allowed"] == stamp(ts("2025-11-13 00:10"))
    d.image.last_beta["web-app"] = ts("2025-11-11 03:00")  # interval ends 2025-11-13 03:00, inside the window
    assert submit(d, channel="beta")["next_allowed"] == stamp(ts("2025-11-13 03:00"))
    d.image.last_beta["web-app"] = ts("2025-11-11 07:00")  # ends 07:00 outside: next window start
    assert submit(d, channel="beta")["next_allowed"] == stamp(ts("2025-11-14 00:00"))


def test_a_second_queued_beta_of_a_project_is_refused_and_the_owner_waives_the_interval(d):
    assert submit(d, channel="beta")["ok"]
    r = submit(d, channel="beta", label="again")
    assert r["error"] == "refused" and "already queued" in r["message"]
    assert submit(d, channel="beta", label="x", pid=AGENT, owner=True, reason="r")["error"] == "owner_denied"
    waived = submit(d, channel="beta", label="owner-says", pid=OWNER, owner=True, reason="release blocker")
    assert waived["ok"] and d.image.get(2).submitter["principal"] == "owner"
    assert submit(d, pid=OWNER, owner=True)["error"] == "invalid_args"  # a reason is required


def test_a_queued_beta_waits_for_its_interval(d, clock):
    submit(d, channel="beta", label="b1")
    next_job(d)
    done(d, {"id": 1})
    submit(d, channel="beta", label="b2", pid=OWNER, owner=True, reason="r")  # interval waived at submit
    assert next_job(d) is None  # but it still starts only after the interval
    clock.t = ts("2025-11-13 00:11")
    assert next_job(d)["label"] == "b2"


# ---- ordering, one at a time, window end ----------------------------------------------------------


def test_one_job_at_a_time_in_submission_order(d):
    for label in ("a", "b", "c"):
        submit(d, label=label)
    first = next_job(d)
    assert first["label"] == "a" and first["start_by"] == ts("2025-11-11 06:00")
    assert next_job(d) is None  # a is running
    done(d, first)
    assert next_job(d)["label"] == "b"
    assert [r["state"] for r in call(d, "image_list")["image"]["queue"]] == ["running", "queued"]


def test_nothing_starts_outside_the_window_or_after_its_end(d, clock):
    submit(d, label="a")
    submit(d, label="b")
    clock.t = DAY
    r = call(d, "image_next", RUNNER)
    assert r["job"] is None and r["in_window"] is False
    clock.t = ts("2025-11-11 05:59")
    a = next_job(d)
    assert a["label"] == "a"
    clock.t = ts("2025-11-11 06:30")  # a runs over the end: it runs to its end ...
    assert d.image.running().label == "a"
    assert next_job(d) is None
    done(d, a)
    assert next_job(d) is None  # ... and b is not started after the end
    st = d.image_status()
    assert [r["state"] for r in st["queue"]] == ["queued"] and st["in_window"] is False
    assert st["queue"][0]["planned_start"] == stamp(ts("2025-11-12 00:00"))
    clock.t = ts("2025-11-12 00:01")
    assert next_job(d)["label"] == "b"


def test_a_job_that_never_started_goes_back_to_the_queue(d, clock):
    submit(d, channel="beta")
    job = next_job(d)
    assert d.image.last_beta and d.image.starts
    r = done(d, job, code=77, started=False)
    assert r["requeued"] is True
    j = d.image.get(1)
    assert j.state == "queued" and d.image.last_beta == {} and d.image.starts == []
    assert audit(d)[-1]["result"] == "deferred"
    assert next_job(d)["id"] == 1  # runnable again


def test_max_per_night_defers_the_rest_to_the_next_night(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock, max_per_night=2)
    for label in "abc":
        submit(d, label=label)
    for want in "ab":
        job = next_job(d)
        assert job["label"] == want
        done(d, job)
    assert next_job(d) is None
    q = d.image_status()["queue"]
    assert [r["state"] for r in q] == ["deferred"]
    assert q[0]["planned_start"] == stamp(ts("2025-11-12 00:00"))
    clock.t = ts("2025-11-12 00:05")
    assert next_job(d)["label"] == "c"


def test_the_runner_is_not_an_agent_and_only_it_reports(d):
    submit(d)
    assert call(d, "image_next", AGENT)["error"] == "owner_denied"  # inside a governed cgroup
    assert audit(d)[-1]["result"] == "refused"
    job = next_job(d)
    assert done(d, job, pid=OWNER)["error"] == "owner_denied"  # not the runner that started it
    assert done(d, job)["ok"] and d.image.jobs == []
    assert call(d, "image_done", RUNNER, id=1, exit_code=0, duration_s=1)["error"] == "invalid_args"


def test_a_dead_runner_drops_its_job(d):
    submit(d)
    next_job(d)
    d.image.running().runner_pid = 99999999  # no such process
    d.slots.stat_reader = lambda pid: None
    call(d, "image_list")
    assert d.image.jobs == [] and audit(d)[-1]["result"] == "failed"


# ---- owner now, cancel ----------------------------------------------------------------------------


def test_owner_now_lets_a_job_run_outside_the_window(d, clock):
    submit(d, channel="beta", label="b1")
    next_job(d)
    done(d, {"id": 1})
    clock.t = DAY
    assert submit(d, channel="beta", label="b2", pid=OWNER, owner=True, reason="r")["ok"]
    submit(d, label="r1")
    assert next_job(d) is None
    assert call(d, "image_now", AGENT, id=2, owner=True, reason="x")["error"] == "owner_denied"  # governed
    assert call(d, "image_now", OWNER, id=2, reason="x")["error"] == "owner_denied"  # no owner claim
    assert d.image.get(2).now_ok is False and next_job(d) is None
    assert call(d, "image_now", OWNER, id=2, owner=True)["error"] == "invalid_args"  # a reason is required
    r = call(d, "image_now", OWNER, id=2, owner=True, reason="release blocker")
    assert r["ok"]
    line = audit(d)[-1]
    assert (line["op"], line["principal"], line["result"]) == ("image_now", "owner", "accepted")
    assert "release blocker" in line["reason"]
    assert [x["result"] for x in audit(d) if x["op"] == "image_now"][:3] == ["refused", "refused", "accepted"]
    assert d.image_status()["queue"][0]["state"] == "now"
    job = next_job(d)  # outside the window, inside the beta interval: the owner's word counts
    assert job["label"] == "b2" and job["start_by"] is None
    assert next_job(d) is None  # still one at a time, and r1 stays in the queue
    done(d, job)
    assert next_job(d) is None  # r1 is not released: window only
    assert d.image.get(3).state == "queued"


def test_cancel_by_project_or_owner_only(d):
    submit(d, project="web-app")
    submit(d, project="api")
    assert call(d, "image_cancel", AGENT, id=1, project="api")["error"] == "owner_denied"
    assert call(d, "image_cancel", AGENT, id=1)["error"] == "owner_denied"
    assert call(d, "image_cancel", AGENT, id=1, owner=True)["error"] == "owner_denied"  # governed agent
    assert d.image.get(1)
    assert call(d, "image_cancel", AGENT, id=1, project="web-app")["cancelled"] is True
    assert call(d, "image_cancel", OWNER, id=2, owner=True)["cancelled"] is True
    assert d.image.jobs == [] and call(d, "image_cancel", OWNER, id=2, owner=True)["error"] == "invalid_args"
    submit(d)
    next_job(d)
    assert "running" in call(d, "image_cancel", AGENT, id=3, project="web-app")["message"]


# ---- ETA ------------------------------------------------------------------------------------------


def record(d, project, seconds, n=2):
    for _ in range(n):
        d.jobs.stats.record(project, "image", {"duration_s": seconds})


def plan(d):
    return [(r["id"], hm(ts_(r["planned_start"])), hm(ts_(r["planned_finish"])), r["state"])
            for r in d.image_status()["queue"]]  # fmt: skip


def ts_(s):
    return ts(s[:10] + " " + s[11:16]) if s else 0


def test_eta_chains_from_now_inside_the_window_and_stops_at_the_first_unknown(d):
    record(d, "web-app", 1800)
    for project in ("web-app", "web-app", "api", "web-app"):
        submit(d, project=project)
    assert plan(d) == [
        (1, "00:10", "00:40", "queued"), (2, "00:40", "01:10", "queued"),
        (3, "01:10", "-", "queued"), (4, "-", "-", "queued"),
    ]  # fmt: skip


def test_eta_starts_at_the_window_open_outside_and_defers_what_does_not_fit(d, clock):
    clock.t = DAY
    record(d, "web-app", 3 * 3600)
    for _ in range(3):
        submit(d)
    q = d.image_status()["queue"]
    assert [(hm(ts_(r["planned_start"])), r["state"]) for r in q] == [
        ("00:00", "queued"), ("03:00", "queued"), ("00:00", "deferred")]  # fmt: skip
    assert q[2]["planned_start"] == stamp(ts("2025-11-12 00:00"))


def test_eta_follows_the_running_job_and_a_hold_makes_it_unknown(d, clock):
    record(d, "web-app", 1800)
    submit(d)
    submit(d)
    next_job(d)
    clock.t = NIGHT + 300
    assert plan(d) == [(1, "00:10", "00:40", "running"), (2, "00:40", "01:10", "queued")]
    d.limits.set_flag("hold", "owner hold")
    assert [r["planned_start"] for r in d.image_status()["queue"][1:]] == [None]
    assert next_job(d) is None  # and nothing is released


def test_eta_for_an_owner_released_job_is_when_the_slot_is_free(d, clock):
    record(d, "web-app", 1800)
    submit(d)
    submit(d)
    next_job(d)
    call(d, "image_now", OWNER, id=2, owner=True, reason="r")
    assert [r["state"] for r in d.image_status()["queue"]] == ["running", "now"]


# ---- the runner -----------------------------------------------------------------------------------


class FakeLauncher:
    """Stands in for `loadctl run`: records what the job would see and returns scripted exit codes."""

    def __init__(self, *codes):
        self.codes, self.seen = list(codes), []

    def __call__(self, ns):
        self.seen.append({"cwd": os.getcwd(), "env": dict(os.environ), "ns": ns})
        code = self.codes.pop(0)
        if code == "missed":  # what run_command reports when --start-by passed before any launch
            ns.outcome["start_by_missed"] = True
            return run.EXIT_START_BY
        return code


@pytest.fixture
def runner_env(d, monkeypatch, tmp_path):
    monkeypatch.setattr(image, "request",
                        lambda _s, op, args=None, **_k: d.dispatch({"v": VERSION, "op": op, "args": args or {}}, RUNNER))  # fmt: skip
    work = tmp_path / "work"
    work.mkdir()
    return d, work


def test_runner_runs_each_job_in_its_cwd_with_the_allowlisted_env_and_reports_exit_codes(runner_env, capsys):
    d, work = runner_env
    submit(d, label="one", cwd=str(work), key="PROJ-7", env={"PATH": "/x/bin", "HOME": "/h", "SECRET": "s"})
    submit(d, label="two", channel="beta", cwd=str(work))
    cwd, env = os.getcwd(), dict(os.environ)
    launcher = FakeLauncher(0, 3)
    rc = image.runner(argparse.Namespace(socket="x", once=False), image.Deps(run=launcher))
    assert rc == 0 and d.image.jobs == []
    first, second = launcher.seen
    assert first["cwd"] == str(work) and first["env"] == {
        "PATH": "/x/bin",
        "HOME": "/h",
    }  # allowlist, not ours
    ns = first["ns"]
    assert (ns.kind, ns.project, ns.label, ns.key, ns.job_cmd) == ("image", "web-app", "one", "PROJ-7",
                                                                    ["--", "make", "image"])  # fmt: skip
    assert ns.start_by == ts("2025-11-11 06:00") and ns.headroom == "full" and ns.no_wait is False
    assert (os.getcwd(), dict(os.environ)) == (cwd, env)  # the runner's own environment is back
    done_lines = [x for x in audit(d) if x["op"] == "image_done"]
    assert [(x["result"], x["request"]["exit_code"]) for x in done_lines] == [("ok", 0), ("failed", 3)]
    out = capsys.readouterr().out
    assert "IMAGE_DONE 1 web-app one exit=0" in out and "IMAGE_DONE 2 web-app two exit=3" in out


def test_runner_once_runs_one_job_and_stops_when_the_queue_is_empty(runner_env):
    d, work = runner_env
    submit(d, label="one", cwd=str(work))
    submit(d, label="two", cwd=str(work))
    launcher = FakeLauncher(0, 0)
    assert image.runner(argparse.Namespace(socket="x", once=True), image.Deps(run=launcher)) == 0
    assert [s["ns"].label for s in launcher.seen] == ["one"] and [j.label for j in d.image.jobs] == ["two"]
    assert image.runner(argparse.Namespace(socket="x", once=False), image.Deps(run=FakeLauncher(0))) == 0
    assert image.runner(argparse.Namespace(socket="x", once=False), image.Deps(run=FakeLauncher())) == 0


def test_runner_requeues_a_job_whose_start_deadline_passed_and_stops(runner_env, capsys):
    d, work = runner_env
    submit(d, label="one", cwd=str(work))
    submit(d, label="two", cwd=str(work))
    launcher = FakeLauncher("missed")
    assert image.runner(argparse.Namespace(socket="x", once=False), image.Deps(run=launcher)) == 0
    assert len(launcher.seen) == 1  # no further job after the window closed on it
    assert [(j.label, j.state) for j in d.image.jobs] == [("one", "queued"), ("two", "queued")]
    assert "not started" in capsys.readouterr().out


def test_a_job_that_ran_and_exited_77_is_started_and_failed_not_requeued(runner_env):
    d, work = runner_env
    submit(d, label="b1", channel="beta", cwd=str(work))
    launcher = FakeLauncher(77)  # the build's own exit code (EX_NOPERM, "skipped"): it did start
    assert image.runner(argparse.Namespace(socket="x", once=False), image.Deps(run=launcher)) == 0
    assert len(launcher.seen) == 1 and d.image.jobs == []  # done, not queued again
    assert d.image.last_beta == {"web-app": NIGHT} and d.image.starts == [NIGHT]  # interval and night kept
    line = [x for x in audit(d) if x["op"] == "image_done"][-1]
    assert (line["result"], line["request"]["exit_code"]) == ("failed", 77)
    assert submit(d, channel="beta")["error"] == "refused"  # the 48 h rule still holds
    assert next_job(d) is None


def test_runner_reports_a_missing_cwd_as_a_failed_job(runner_env):
    d, _ = runner_env
    submit(d, cwd="/nonexistent/dir")
    launcher = FakeLauncher()
    assert image.runner(argparse.Namespace(socket="x", once=True), image.Deps(run=launcher)) == 0
    assert launcher.seen == [] and audit(d)[-1]["request"]["exit_code"] == 127


def test_runner_from_an_agent_is_refused(d, monkeypatch):
    monkeypatch.setattr(image, "request",
                        lambda _s, op, args=None, **_k: d.dispatch({"v": VERSION, "op": op, "args": args or {}}, AGENT))  # fmt: skip
    submit(d)
    assert (
        image.runner(argparse.Namespace(socket="x", once=True), image.Deps(run=FakeLauncher(0)))
        == loadctl.EXIT_DENIED
    )


def test_submit_command_sends_only_the_allowlisted_environment(d, monkeypatch, tmp_path, capsys):
    sent = {}

    def fake_request(_s, op, args=None, **_k):
        if op == "image_submit":
            sent.update(args)
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, AGENT)

    monkeypatch.setattr(image, "request", fake_request)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SECRET_TOKEN", "t0p")
    monkeypatch.delenv("LOADGOV_KEY", raising=False)
    a = loadctl._parser().parse_args(
        ["image", "submit", "--project", "web-app", "--label", "nightly", "--channel", "release",
         "--key", "PROJ-9", "--cwd", str(tmp_path), "--", "make", "image"])  # fmt: skip
    assert image.image_command(a) == 0
    assert sent["argv"] == ["make", "image"] and sent["cwd"] == str(tmp_path) and sent["key"] == "PROJ-9"
    assert "SECRET_TOKEN" not in sent["env"] and sent["env"]["PATH"] == "/usr/bin"
    assert "queued image job 1" in capsys.readouterr().out
    a.channel = "beta"
    assert image.image_command(a) == 0
    assert image.image_command(a) == loadctl.EXIT_DENIED  # one queued beta per project
    assert "already queued" in capsys.readouterr().err


def test_loadctl_run_for_an_image_job_takes_the_heavy_slot_and_honours_start_by(
    fake_host, tmp_path, monkeypatch, procs, capsys
):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    ops = []
    wire(monkeypatch, d, ops)
    h = Host(host, procs().pid)
    argv = ["--kind", "image", "--project", "web-app", "--label", "img", "--start-by", "1", "--", "true"]
    assert go(monkeypatch, d, h.deps, *argv) == run.EXIT_START_BY == 77
    assert h.launched == [] and "slot_acquire" in ops  # it queued for the heavy slot first
    assert d.slots.slots["heavy"].holder is None  # and gave it back
    h2 = Host(host, procs().pid)
    ops.clear()
    assert go(monkeypatch, d, h2.deps, *argv[:6], "--", "true") == 0
    assert d.slots.slots["heavy"].holder is None and ops[-2:] == ["job_start", "job_end"]
    assert h2.launched and "--unit=loadgov-image-img-" in " ".join(h2.launched[0][0])


def test_run_command_reports_a_missed_start_by_in_process(fake_host, tmp_path, monkeypatch, procs):
    host = fake_host("calm")
    d = make_daemon(host, tmp_path)
    wire(monkeypatch, d)
    h = Host(host, procs().pid)
    base = ["run", "--kind", "image", "--project", "web-app"]
    a = loadctl._parser().parse_args([*base, "--label", "img", "--start-by", "1", "--", "true"])
    a.outcome = {}
    assert run.run_command(a, h.deps) == run.EXIT_START_BY and a.outcome == {"start_by_missed": True}
    assert h.launched == []
    h2 = Host(host, procs().pid, code=77)  # a launched job that exits 77 says nothing of the kind
    a = loadctl._parser().parse_args([*base, "--label", "img2", "--", "true"])
    a.outcome = {}
    assert run.run_command(a, h2.deps) == 77 and a.outcome == {} and h2.launched


def test_host_tools_are_resolved_outside_the_job_path(monkeypatch, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemd-run").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", str(fake))  # a submitter's allowlisted PATH
    argv = run.trusted(["systemd-run", "--scope", "--", "make"])
    assert argv[0] in ("/usr/bin/systemd-run", "/bin/systemd-run")
    assert argv[1:] == ["--scope", "--", "make"]
    seen = {}
    monkeypatch.setattr(run.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv))
    run._spawn(["systemd-run", "x"], {"PATH": str(fake)})
    assert seen["argv"][0] == argv[0]
    assert run.trusted(["nice", "-n", "10"]) == [
        "nice",
        "-n",
        "10",
    ]  # only the tools loadctl run calls itself


# ---- alerts ---------------------------------------------------------------------------------------


def add_image_job(
    d, since, project="web-app", cgroup="/build.slice/build-work.slice/loadgov-image-x-1.scope"
):
    unit = "loadgov-image-x-1.scope"
    d.jobs.jobs[unit] = Job("image", project, "img", unit, cgroup, 4242, 1, RUNNER, "heavy", since)
    return cgroup


def minimal_status():
    return {
        "memory": {"avail_mb": 20000}, "swap": {"used_mb": 0},
        "disk": {"free_gb": 500, "path": "/", "used_pct": 10.0, "paths": []},
    }  # fmt: skip


def test_overrun_raises_the_alert_and_never_kills(d, clock):
    add_image_job(d, since=NIGHT - 89 * 60)
    assert d._image_alerts() == {}
    clock.t = NIGHT + 61  # 90 min and a second
    found = d._image_alerts()
    level, title, msg, actions = found["image_overrun:web-app"]
    assert level == "warning" and "90 min" in msg and "not stopped" in msg and actions == []
    assert d.jobs.jobs  # nothing was ended, no runner is involved


def test_the_alerts_reach_the_watch_and_resolve(d, clock):
    w = Watch(clock)
    w.disk_busy, w.disk_limit = d.watch.disk_busy, d.watch.disk_limit
    add_image_job(d, since=NIGHT - 3 * 3600)
    w.external = d._image_alerts()
    ev = w.evaluate({**minimal_status(), "slots": {}}, 10.0, 65.0, "balanced", None, [])
    assert [(e["key"], e["level"]) for e in ev if e["type"] == "alert"] == [
        ("image_overrun:web-app", "warning")
    ]
    d.jobs.jobs.clear()
    w.external = d._image_alerts()
    for _ in range(2):
        ev = w.evaluate({**minimal_status(), "slots": {}}, 10.0, 65.0, "balanced", None, [])
    assert [e["type"] for e in ev] == ["resolved"]


def docker_ticks(*pids, comm="docker"):
    return {p: (1, 10, comm, 50) for p in pids}


def test_out_of_window_build_activity_is_an_alert_and_names_no_arguments(d, clock):
    d.sampler.proc_ticks = docker_ticks(700, 701)  # a `docker build ... TOKEN=s3cret`, and a `docker run`
    clock.t = NIGHT  # in the window: nothing to say
    assert d._image_alerts() == {}
    clock.t = DAY
    level, title, msg, _ = d._image_alerts()["image_out_of_window"]
    assert level == "warning" and "docker build (pid 700)" in msg and "701" not in msg
    assert "00:00-06:00 UTC" in msg and "s3cret" not in msg and "TOKEN" not in msg


def test_a_build_inside_a_running_image_jobs_scope_is_not_out_of_window(d, clock, proc):
    clock.t = DAY
    d.sampler.proc_ticks = docker_ticks(700)
    cg = add_image_job(d, since=DAY - 600)
    add_proc(proc, 700, cg + "/child", "docker build .", 1)  # the job's own build
    assert d._image_alerts() == {}
    add_proc(proc, 700, "/user.slice/session-1.scope", "docker build .", 1)
    assert "image_out_of_window" in d._image_alerts()


def test_find_builds_recognises_the_usual_clients_and_only_those(proc, tmp_path):
    fs = HostFS(proc_root=str(proc), cgroup_root=str(tmp_path / "cg"), lock_root=str(tmp_path / "lock"))
    cases = {
        710: ("docker buildx build --push .", "docker", True), 711: ("docker compose -f x.yml build", "docker", True),
        712: ("docker buildx bake all", "docker-buildx", True), 713: ("buildctl build --frontend x", "buildctl", True),
        714: ("docker ps", "docker", False), 715: ("docker run --rm img build", "docker", False),
        716: ("python build.py", "python3", False), 717: ("docker pull img", "docker", False),
    }  # fmt: skip
    for pid, (cmd, _comm, _hit) in cases.items():
        add_proc(proc, pid, "/user.slice/session-1.scope", cmd, 1)
    ticks = {pid: (1, 1, comm, 1) for pid, (_c, comm, _h) in cases.items()}
    found = find_builds(fs, ticks, [])
    assert [b["pid"] for b in found] == [p for p, (_c, _m, hit) in cases.items() if hit]
    assert {b["what"] for b in found} == {
        "docker buildx build",
        "docker build",
        "docker buildx bake",
        "buildctl build",
    }


# ---- status and protocol --------------------------------------------------------------------------


def test_status_validates_and_carries_the_image_object(fake_host, tmp_path):
    host = fake_host("calm")
    clock = Clock()
    d = Daemon(
        host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=parity_config(), clock=clock,
    )  # fmt: skip
    os.makedirs(d.run_dir)
    d.sampler.prime(host.prev_stat)
    first = d.tick()
    assert validate_status(first) == []
    assert first["image"]["queue"] == [] and first["image"]["in_window"] is True
    d.config = dataclasses.replace(
        d.config, keys=dataclasses.replace(d.config.keys, url="https://tracker.example/{key}")
    )
    record(d, "web-app", 1800)
    submit(d, channel="beta", key="PROJ-5", label="b")
    d.dispatch(
        {
            "v": VERSION,
            "op": "image_submit",
            "args": {"project": "api", "label": "r", "channel": "release", "argv": ["x"], "cwd": "/"},
        },
        AGENT,
    )
    d.dispatch(
        {
            "v": VERSION,
            "op": "image_submit",
            "args": {"project": "web-app", "label": "r2", "channel": "release", "argv": ["x"], "cwd": "/"},
        },
        AGENT,
    )
    st = d.dispatch({"v": VERSION, "op": "status"})["status"]
    assert validate_status(st) == []
    im = st["image"]
    assert (im["window"], im["timezone"], im["in_window"], im["queued"]) == ("00:00-06:00", "UTC", True, 3)
    assert im["next_window_start"] == stamp(ts("2025-11-12 00:00"))
    assert im["queue"][0]["url"] == "https://tracker.example/PROJ-5"
    assert {k for k in im["queue"][0]} == {
        "id", "position", "project", "label", "key", "url", "channel", "submitted", "planned_start",
        "planned_finish", "state"}  # fmt: skip
    assert loadctl.format_status(st).count("image train: 3 queued, next 00:10 (web-app beta)") == 1


def test_the_status_queue_is_bounded(d):
    for i in range(30):
        submit(d, project=f"p{i}", label=f"l{i}")
    assert len(d.image_status()["queue"]) == 20 and d.image_status()["queued"] == 30
    assert len(call(d, "image_list")["image"]["queue"]) == 30


def test_status_line_formats():
    im = {
        "timezone": "UTC", "queued": 3, "running": 0, "next_window_start": "2025-11-12T00:00:00Z",
        "queue": [{"state": "queued", "planned_start": "2025-11-11T00:40:00Z", "project": "web-app", "channel": "beta"}],
    }  # fmt: skip
    assert image.format_image_line(im) == "image train: 3 queued, next 00:40 (web-app beta)"
    im["queue"][0]["planned_start"] = None  # unknown start: the next window opening
    assert image.format_image_line(im) == "image train: 3 queued, next 00:00 (web-app beta)"
    assert image.format_image_line({**im, "queue": []}) is None
    assert image.format_image_line(None) is None
    ny = {**im, "timezone": "America/New_York"}
    assert image.format_image_line(ny).endswith("next 19:00 (web-app beta)")  # shown in the window's zone


# ---- hardening: window edges, restart, night counts, overrun, status rendering --------------------


def claim_at(d, clock, t):
    clock.t = t
    return call(d, "image_next", RUNNER)


def test_default_window_edges_start_is_open_and_end_is_closed(d, clock):
    submit(d, label="a")
    assert claim_at(d, clock, ts("2025-11-10 23:59") + 59)["job"] is None  # one second before the start
    r = claim_at(d, clock, ts("2025-11-11 00:00"))
    assert r["job"]["label"] == "a" and r["job"]["start_by"] == ts("2025-11-11 06:00")
    done(d, r["job"])
    submit(d, label="b")
    assert claim_at(d, clock, ts("2025-11-11 06:00"))["job"] is None  # the end itself is outside


def test_a_job_claimed_at_0559_must_start_by_0600_and_is_requeued_after(d, clock):
    submit(d)
    job = claim_at(d, clock, ts("2025-11-11 05:59"))["job"]
    assert job["start_by"] == ts("2025-11-11 06:00")
    clock.t = ts("2025-11-11 06:00")  # the slot wait ran past the end: nothing started
    assert done(d, job, code=77, started=False)["requeued"] is True
    assert d.image.starts == [] and d.image.get(1).state == "queued"
    assert claim_at(d, clock, ts("2025-11-11 06:01"))["job"] is None
    assert claim_at(d, clock, ts("2025-11-12 00:00"))["job"]["id"] == 1


def test_a_window_across_midnight_has_its_edges_at_2200_and_0200(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock, window="22:00-02:00")
    submit(d, label="a")
    assert claim_at(d, clock, ts("2025-11-10 21:59") + 59)["job"] is None
    job = claim_at(d, clock, ts("2025-11-10 22:00"))["job"]
    assert job["label"] == "a" and job["start_by"] == ts("2025-11-11 02:00")
    done(d, job)
    submit(d, label="b")
    assert claim_at(d, clock, ts("2025-11-11 01:59"))["job"]["label"] == "b"  # after midnight, same window
    assert d.image.starts == [ts("2025-11-10 22:00"), ts("2025-11-11 01:59")]
    done(d, {"id": 2})
    submit(d, label="c")
    assert claim_at(d, clock, ts("2025-11-11 02:00"))["job"] is None


def test_max_per_night_counts_a_window_that_crosses_midnight_as_one_night(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock, window="22:00-02:00", max_per_night=2)
    for label in "abc":
        submit(d, label=label)
    for t, want in (("2025-11-10 23:30", "a"), ("2025-11-11 00:30", "b")):  # two sides of midnight
        job = claim_at(d, clock, ts(t))["job"]
        assert job["label"] == want
        done(d, job)
    assert claim_at(d, clock, ts("2025-11-11 01:30"))["job"] is None  # the third of the same night
    q = d.image_status()["queue"]
    assert q[0]["state"] == "deferred" and q[0]["planned_start"] == stamp(ts("2025-11-11 22:00"))
    assert claim_at(d, clock, ts("2025-11-11 22:00"))["job"]["label"] == "c"


@pytest.mark.parametrize(
    ("inside", "outside"),
    [
        # Europe/Berlin springs forward on 2026-03-29 (02:00 CET -> 03:00 CEST): 01:00-04:00 local is 2 h
        (("2026-03-29 01:59", "2026-03-29 00:00"), ("2026-03-29 02:00", "2026-03-28 23:59")),
        # and falls back on 2026-10-25 (03:00 CEST -> 02:00 CET): 01:00-04:00 local is 4 h
        (("2026-10-25 02:59", "2026-10-24 23:00"), ("2026-10-25 03:00", "2026-10-24 22:59")),
    ],
)
def test_the_daemon_follows_the_wall_clock_through_dst_nights(tmp_path, proc, clock, inside, outside):
    d = new_daemon(tmp_path, proc, clock, window="01:00-04:00", timezone="Europe/Berlin")
    for n, t in enumerate((*inside, *outside)):
        submit(d, label=f"j{n}")
        got = claim_at(d, clock, ts(t))["job"]
        assert (got is not None) == (n < 2), t
        if got:
            done(d, got)
        else:
            call(d, "image_cancel", AGENT, id=n + 1, project="web-app")


def test_restart_keeps_now_releases_night_counts_and_the_beta_interval(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock, max_per_night=2)
    submit(d, label="a", channel="beta")
    submit(d, label="b")
    submit(d, label="c")
    done(d, next_job(d))
    done(d, next_job(d))  # two starts this night; c waits for the next one
    clock.t = DAY
    call(d, "image_now", OWNER, id=3, owner=True, reason="release blocker")
    d2 = new_daemon(tmp_path, proc, clock, max_per_night=2)
    assert d2.image.get(3).now_ok is True and d2.image.get(3).now_reason == "release blocker"
    assert d2.image.starts == [NIGHT, NIGHT] and d2.image.last_beta == {"web-app": NIGHT}
    assert next_job(d2)["id"] == 3  # the owner's release outside the window survived
    done(d2, {"id": 3})
    submit(d2, label="d")
    clock.t = NIGHT + 600  # the same night: both starts still count after the restart
    assert next_job(d2) is None
    assert d2.image_status()["queue"][0]["state"] == "deferred"


def test_a_leftover_running_job_with_a_live_runner_survives_a_restart(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock)
    submit(d)
    next_job(d)
    d2 = new_daemon(tmp_path, proc, clock)  # the runner pid is alive for the fake stat reader
    call(d2, "image_list")
    assert d2.image.running() is not None and not [a for a in audit(d2) if a["op"] == "image_done"]
    assert next_job(d2) is None  # still one at a time
    assert done(d2, {"id": 1})["ok"]


def test_a_running_job_whose_runner_died_while_the_daemon_was_down_is_dropped_failed_and_audited(
    tmp_path, proc, clock
):
    d = new_daemon(tmp_path, proc, clock)
    submit(d, label="gone", channel="beta")
    submit(d, label="next")
    next_job(d)
    d2 = new_daemon(tmp_path, proc, clock)
    d2.slots.stat_reader = lambda pid: None  # the runner is no longer there
    call(d2, "image_list")
    assert [j.label for j in d2.image.jobs] == ["next"]
    line = [a for a in audit(d2) if a["op"] == "image_done"][-1]
    assert line["result"] == "failed" and line["principal"] == "system"
    assert d2.image.last_beta == {"web-app": NIGHT}  # a half-done beta still counts for the interval
    assert next_job(d2)["label"] == "next"  # the dropped job is not run again; the queue goes on


def test_cancelling_a_running_job_is_refused_for_project_and_owner_and_it_stays_running(d):
    submit(d)
    next_job(d)
    for pid, kw in ((AGENT, {"project": "web-app"}), (OWNER, {"owner": True})):
        r = call(d, "image_cancel", pid, id=1, **kw)
        assert r["ok"] is False and "running" in r["message"]
    assert d.image.running().id == 1 and not [
        a for a in audit(d) if a["op"] == "image_cancel" and a["result"] == "accepted"
    ]


def test_the_overrun_alert_is_raised_once_and_clears_when_the_job_ends(d, clock):
    w = Watch(clock)
    w.disk_busy, w.disk_limit = d.watch.disk_busy, d.watch.disk_limit
    add_image_job(d, since=NIGHT - 3600)

    def tick(t):
        clock.t = t
        w.external = d._image_alerts()
        return w.evaluate({**minimal_status(), "slots": {}}, 10.0, 65.0, "balanced", None, [])

    assert tick(NIGHT + 20 * 60) == []  # 80 min of 90: not yet
    first = tick(NIGHT + 31 * 60)
    assert [(e["type"], e["key"]) for e in first] == [("alert", "image_overrun:web-app")]
    for t in (NIGHT + 32 * 60, NIGHT + 33 * 60):
        assert tick(t) == []  # no repeat inside the dedupe window
    assert d.jobs.jobs  # never killed
    d.jobs.jobs.clear()
    seen = [e["type"] for t in (NIGHT + 34 * 60, NIGHT + 35 * 60) for e in tick(t)]
    assert seen == ["resolved"]


def test_the_status_line_for_an_empty_train_near_its_window_and_with_a_running_job():
    im = {"window": "00:00-06:00", "timezone": "UTC", "in_window": False, "queued": 0, "running": 0,
          "next_window_start": "2025-11-12T00:00:00Z", "queue": []}  # fmt: skip
    at = "2025-11-11T17:00:00Z"
    assert image.format_image_line(im, at) == "image train: empty, window 00:00-06:00 UTC, opens in 7 h"
    assert image.format_image_line(im, "2025-11-11T23:15:00Z").endswith("opens in 45 min")
    assert image.format_image_line(im, "2025-11-11T10:00:00Z") is None  # not near: 14 h
    assert image.format_image_line(im, None) is None and image.format_image_line(im) is None
    assert image.format_image_line({**im, "in_window": True}, at).endswith("UTC, open")
    running = {"id": 1, "state": "running", "project": "web-app", "channel": "release"}
    only = {**im, "running": 1, "queue": [running]}
    assert image.format_image_line(only, at) == "image train: 1 running"
    queued = {"state": "queued", "planned_start": "2025-11-12T01:00:00Z", "project": "api", "channel": "beta"}
    both = {**im, "queued": 1, "running": 1, "queue": [running, queued]}
    assert image.format_image_line(both, at) == "image train: 1 queued, 1 running, next 01:00 (api beta)"
    ny = {**im, "timezone": "America/New_York", "next_window_start": "2025-11-12T05:00:00Z"}
    assert image.format_image_line(ny, at).endswith("opens in 12 h")


def test_status_shows_light_image_and_job_lines_with_the_same_labels(fake_host, tmp_path):
    host = fake_host("calm")
    clock = Clock()
    clock.t = DAY
    d = Daemon(
        host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        compat_link=None, run_dir=str(tmp_path / "run"), config=parity_config(), clock=clock,
    )  # fmt: skip
    os.makedirs(d.run_dir)
    d.sampler.prime(host.prev_stat)
    st = d.tick()
    assert validate_status(st) == []
    row = {"project": "web-app", "kind": "image", "label": "img", "key": None, "slot": "heavy"}
    st["jobs"] = [{**row, "eta": stamp(DAY + 2400)}]  # a running image job with its expected finish
    st["at"] = stamp(DAY)  # the sampler stamps the real time; the status line compares against it
    lines = loadctl.format_status(st).splitlines()
    assert "  image train: empty, window 00:00-06:00 UTC, opens in 5 h" in lines  # 19:20, window at 00:00
    job = next(line for line in lines if line.startswith("  job   web-app image"))
    assert re.fullmatch(r"  job   web-app image img \[heavy\] eta \d\d:\d\d", job)
    st["jobs"] = [{**row, "eta": stamp(DAY - 60), "overdue": True}]
    assert "  job   web-app image img [heavy] overdue" in loadctl.format_status(st).splitlines()
    st["jobs"] = [{**row, "eta": stamp(DAY + 2400)}]
    assert any(line.startswith("  light 0/") for line in lines)
    assert any(line.startswith("  disk  ") for line in lines)
    labels = {line[2:8] for line in lines if line.startswith("  ") and line[2:3].isalpha()}
    assert {"light ", "image ", "job   ", "disk  "} <= labels  # one label column, six wide
    # 14 h before the window: too far away to be worth a line
    st["at"] = stamp(ts("2025-11-10 10:00"))
    assert not any(x.startswith("  image train") for x in loadctl.format_status(st).splitlines())


# ---- runtime settings: the owner's override of window, timezone and beta interval (ILM-32) --------


def setimg(d, pid=OWNER, **kw):
    kw.setdefault("owner", True)
    kw.setdefault("reason", "shift the night")
    return call(d, "set", pid, **kw)


def settings(d):
    st = d.image_status()
    return {k: st[k] for k in ("window", "timezone", "beta_interval_h", "source")}


def saved_override(tmp_path):
    return json.load(open(tmp_path / "state" / "state.json"))["image"]["override"]


def test_owner_sets_the_image_window_and_the_status_shows_it(d):
    assert settings(d) == {
        "window": "00:00-06:00",
        "timezone": "UTC",
        "beta_interval_h": 48,
        "source": "config",
    }
    r = setimg(d, image_window="22:00-04:00", image_timezone="America/New_York", image_beta_interval_h=24)
    assert r["ok"] and r["cleared"] is False
    assert r["before"]["source"] == "config" and r["after"]["window"] == "22:00-04:00"
    assert settings(d) == {"window": "22:00-04:00", "timezone": "America/New_York", "beta_interval_h": 24,
                           "source": "override"}  # fmt: skip
    assert d.config.image.window == "00:00-06:00"  # the config default underneath is untouched


def test_a_partial_set_keeps_the_other_override_values(d):
    setimg(d, image_window="22:00-04:00")
    setimg(d, image_beta_interval_h=0)
    assert settings(d) == {
        "window": "22:00-04:00",
        "timezone": "UTC",
        "beta_interval_h": 0,
        "source": "override",
    }
    assert d.image.override == {"window": "22:00-04:00", "beta_interval_h": 0}


def test_an_agent_is_refused_and_audited(d):
    for kw in ({"pid": AGENT, "owner": True}, {"pid": OWNER, "owner": False}, {"pid": AGENT, "owner": False}):
        assert setimg(d, image_window="22:00-04:00", **kw)["error"] == "owner_denied"
    assert settings(d)["source"] == "config" and d.image.override == {}
    lines = [x for x in audit(d) if x["op"] == "image_settings"]
    assert [(x["principal"], x["result"]) for x in lines] == [("agent", "refused")] * 3
    assert call(d, "set", AGENT, image_clear=True, reason="x")["error"] == "owner_denied"  # no claim at all


@pytest.mark.parametrize(
    "kw, text",
    [
        ({"image_window": "6:00-7:00"}, "HH:MM-HH:MM"), ({"image_window": "24:00-02:00"}, "HH:MM-HH:MM"),
        ({"image_window": "00:60-02:00"}, "HH:MM-HH:MM"), ({"image_window": "night"}, "HH:MM-HH:MM"),
        ({"image_window": "03:00-03:00"}, "different start and end"),
        ({"image_timezone": "Mars/Base"}, "unknown IANA time zone"),
        ({"image_timezone": "../etc/passwd"}, "unknown"), ({"image_timezone": ""}, "unknown"),
        ({"image_beta_interval_h": -1}, "0 to 336"), ({"image_beta_interval_h": 337}, "0 to 336"),
    ],
)  # fmt: skip
def test_a_bad_value_is_refused_with_a_clear_message(d, kw, text):
    r = setimg(d, **kw)
    assert r["ok"] is False and r["error"] == "invalid_args" and text in r["message"]
    assert d.image.override == {} and not os.path.exists(d.audit.path)  # nothing changed, nothing audited


@pytest.mark.parametrize(
    "kw",
    [{"image_window": 600}, {"image_timezone": 1}, {"image_beta_interval_h": 1.5},
     {"image_beta_interval_h": "48"}, {"image_beta_interval_h": True}, {"image_clear": "yes"}],
)  # fmt: skip
def test_a_value_of_the_wrong_type_is_a_bad_request(d, kw):
    assert setimg(d, **kw)["error"] == "bad_request" and d.image.override == {}


def test_a_reason_is_required_and_image_settings_do_not_mix_with_limits(d):
    assert setimg(d, image_window="22:00-04:00", reason=" ")["error"] == "invalid_args"
    assert setimg(d, image_window="22:00-04:00", image_clear=True)["error"] == "invalid_args"
    assert setimg(d, image_clear=False)["error"] == "invalid_args"  # nothing to set
    r = setimg(d, image_window="22:00-04:00", max_cores=2)
    assert r["error"] == "invalid_args" and "cannot be combined" in r["message"]
    assert d.image.override == {}


def test_midnight_crossing_window_and_the_boundaries_are_accepted(d, clock):
    assert setimg(d, image_window="23:30-00:15", image_beta_interval_h=336)["ok"]
    clock.t = ts("2025-11-11 23:45")
    assert d.image_status()["in_window"] is True
    clock.t = ts("2025-11-11 00:20")
    assert d.image_status()["in_window"] is False


def test_clear_restores_the_config_values_and_is_audited(d):
    setimg(d, image_window="22:00-04:00", image_beta_interval_h=12)
    r = setimg(d, image_clear=True, reason="back to the rule in the file")
    assert r["ok"] and r["cleared"] is True and r["after"]["source"] == "config"
    assert settings(d) == {
        "window": "00:00-06:00",
        "timezone": "UTC",
        "beta_interval_h": 48,
        "source": "config",
    }
    assert d.image.override == {}
    lines = [x for x in audit(d) if x["op"] == "image_settings"]
    assert [x["result"] for x in lines] == ["accepted", "cleared"]
    assert lines[1]["before"]["window"] == "22:00-04:00" and lines[1]["after"]["window"] == "00:00-06:00"
    assert setimg(d, image_clear=True)["ok"]  # clearing nothing is harmless


def test_every_change_is_audited_with_before_after_principal_and_reason(d):
    setimg(d, image_window="22:00-04:00", reason="owner moved the night")
    line = [x for x in audit(d) if x["op"] == "image_settings"][-1]
    assert (line["principal"], line["result"], line["reason"]) == (
        "owner",
        "accepted",
        "owner moved the night",
    )
    assert line["before"] == {
        "window": "00:00-06:00",
        "timezone": "UTC",
        "beta_interval_h": 48,
        "source": "config",
    }
    assert line["after"] == {
        "window": "22:00-04:00",
        "timezone": "UTC",
        "beta_interval_h": 48,
        "source": "override",
    }
    assert line["pid"] == OWNER and line["request"] == {"owner": True, "image_window": "22:00-04:00"}


def test_the_override_survives_a_restart(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock)
    setimg(d, image_window="22:00-04:00", image_timezone="America/New_York", image_beta_interval_h=24)
    assert saved_override(tmp_path) == {"window": "22:00-04:00", "timezone": "America/New_York",
                                        "beta_interval_h": 24}  # fmt: skip
    d2 = new_daemon(tmp_path, proc, clock)
    assert settings(d2) == {"window": "22:00-04:00", "timezone": "America/New_York", "beta_interval_h": 24,
                            "source": "override"}  # fmt: skip
    setimg(d2, image_clear=True)
    assert settings(new_daemon(tmp_path, proc, clock))["source"] == "config"
    assert saved_override(tmp_path) == {}


def test_an_unreadable_saved_override_falls_back_to_the_config(tmp_path, proc, clock):
    d = new_daemon(tmp_path, proc, clock)
    setimg(d, image_window="22:00-04:00")
    p = tmp_path / "state" / "state.json"
    raw = json.load(open(p))
    raw["image"]["override"] = {"window": "99:99-00:00"}
    p.write_text(json.dumps(raw))
    assert settings(new_daemon(tmp_path, proc, clock))["source"] == "config"


def test_a_reload_keeps_the_override_and_updates_the_default_underneath(tmp_path, proc, clock):
    cfg_file = tmp_path / "loadgov.toml"
    cfg_file.write_text('[image]\nwindow = "00:00-06:00"\nbeta_interval_h = 48\n')
    d = new_daemon(tmp_path, proc, clock)
    d.config_path = str(cfg_file)
    setimg(d, image_window="22:00-04:00")
    cfg_file.write_text('[image]\nwindow = "12:00-13:00"\nbeta_interval_h = 72\nmax_per_night = 2\n')
    d.reload_config()
    st = d.image_status()
    assert (st["window"], st["source"], st["max_per_night"]) == ("22:00-04:00", "override", 2)  # window kept
    assert st["beta_interval_h"] == 72  # a field the override does not name follows the new default
    assert d.config.image.window == "12:00-13:00"
    setimg(d, image_clear=True)
    assert settings(d) == {
        "window": "12:00-13:00",
        "timezone": "UTC",
        "beta_interval_h": 72,
        "source": "config",
    }


def test_planned_starts_are_recomputed_on_a_change(d, clock):
    submit(d, label="a")
    submit(d, label="b")
    assert d.image_status()["queue"][0]["planned_start"] == stamp(NIGHT)  # inside 00:00-06:00
    setimg(d, image_window="10:00-12:00")
    st = d.image_status()
    assert st["in_window"] is False and st["next_window_start"] == stamp(ts("2025-11-11 10:00"))
    assert [r["planned_start"] for r in st["queue"]] == [
        stamp(ts("2025-11-11 10:00")),
        None,
    ]  # b: no median yet
    assert next_job(d) is None  # nothing is released outside the new window
    setimg(d, image_clear=True)
    assert d.image_status()["queue"][0]["planned_start"] == stamp(NIGHT)
    assert next_job(d)["label"] == "a"


def test_a_running_job_is_not_touched_by_a_change(d):
    submit(d, label="a", channel="beta")
    submit(d, label="b")
    job = next_job(d)
    run = d.image.get(job["id"])
    before = (run.started, run.state, dict(d.image.last_beta))
    setimg(d, image_window="10:00-12:00", image_beta_interval_h=0)
    assert (run.started, run.state, dict(d.image.last_beta)) == before
    row = d.image_status()["queue"][0]
    assert (row["id"], row["state"], row["planned_start"]) == (job["id"], "running", stamp(NIGHT))
    assert done(d, job)["ok"]  # the runner can still report it
    assert next_job(d) is None  # b waits for the new window


def test_the_beta_interval_override_applies_to_new_betas(d):
    submit(d, channel="beta", label="b1")
    done(d, next_job(d))
    assert submit(d, channel="beta", label="b2")["error"] == "refused"  # 48 h from the config
    setimg(d, image_beta_interval_h=0)
    assert submit(d, channel="beta", label="b2")["ok"]


def test_loadctl_set_sends_the_image_fields_and_prints_the_result(d, monkeypatch, capsys):
    sent = []

    def fake_request(_s, op, args=None, **_k):
        sent.append((op, args))
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, OWNER)

    monkeypatch.setattr(loadctl, "request", fake_request)
    argv = ["set", "--owner", "--reason", "r", "--image-window", "22:00-04:00", "--image-timezone", "America/New_York",
            "--image-beta-interval-h", "24"]  # fmt: skip
    assert loadctl.main(argv) == 0
    op, args = sent[-1]
    assert op == "set" and args["image_window"] == "22:00-04:00" and args["image_beta_interval_h"] == 24
    assert "max_cores" not in args and args["owner"] is True
    assert (
        "image set: window 22:00-04:00 America/New_York, beta interval 24 h (override)"
        in capsys.readouterr().out
    )
    assert loadctl.main(["set", "--owner", "--reason", "r", "--image-clear"]) == 0
    assert "image cleared" in capsys.readouterr().out and d.image.override == {}
    assert (
        loadctl.main(["set", "--owner", "--reason", "r", "--image-window", "03:00-03:00"])
        == loadctl.EXIT_ERROR
    )
    err = capsys.readouterr().err
    assert "different start and end" in err and "Traceback" not in err
    with pytest.raises(SystemExit):  # argparse: a non-integer interval is a usage error, not a traceback
        loadctl.main(["set", "--owner", "--reason", "r", "--image-beta-interval-h", "x"])


def test_loadctl_set_from_an_agent_is_denied(d, monkeypatch, capsys):
    monkeypatch.setattr(loadctl, "request", lambda _s, op, args=None, **_k:
                        d.dispatch({"v": VERSION, "op": op, "args": args or {}}, AGENT))  # fmt: skip
    argv = ["set", "--owner", "--reason", "r", "--image-window", "22:00-04:00"]
    assert loadctl.main(argv) == loadctl.EXIT_DENIED
    assert "owner denied" in capsys.readouterr().err and d.image.override == {}


def test_status_line_and_queue_mark_an_override(d):
    setimg(d, image_window="22:00-04:00")
    im = d.image_status()
    assert im["source"] == "override"
    assert "(override)" in image.format_image_line({**im, "in_window": True}, None)
    assert "(override)" in image.format_queue(im)
    assert "(override)" not in image.format_queue({**im, "source": "config"})
