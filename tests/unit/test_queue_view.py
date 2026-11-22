"""Issue keys, the published queue (`slots.<slot>.waiters`), estimates and the link template."""

import json
import os
import subprocess
import time

import pytest
from conftest import parity_config

from loadgov.cli import loadctl
from loadgov.config import ConfigError, parse_config
from loadgov.daemon.main import Daemon
from loadgov.engines.eta import finish_estimate, queue_starts
from loadgov.engines.jobs import Job
from loadgov.engines.jobstats import JobStats
from loadgov.engines.slots import SlotManager, Waiter
from loadgov.host.procfs import HostFS
from loadgov.keys import key_error, key_url
from loadgov.protocol import VERSION, validate_status
from loadgov.sample import Sampler

T0 = 1_762_800_000.0
URL = "https://tracker.example/issues/{key}"


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


# ---- key shape and link template -------------------------------------------------------------------


@pytest.mark.parametrize("key", ["PROJ-42", "a-1", "My_Proj2-0007", "X" * 60 + "-1"])
def test_valid_keys(key):
    assert key_error(key) is None


@pytest.mark.parametrize(
    "key",
    [
        "",
        "42",
        "PROJ",
        "PROJ-",
        "-42",
        "1ABC-2",
        "BA TON-1",
        "PROJ-1\n",
        "PROJ-1x",
        "X" * 63 + "-1",
        "A-é1",
    ],
)
def test_invalid_keys(key):
    assert key_error(key)


def test_key_must_be_a_string():
    assert key_error(7) == "key must be a string"


def test_url_template():
    assert key_url("", "PROJ-42") is None  # default: no links
    assert key_url(URL, None) is None
    assert key_url(URL, "PROJ-42") == "https://tracker.example/issues/PROJ-42"
    assert key_url("https://t.example/?q={key}&k={key}", "A-1") == "https://t.example/?q=A-1&k=A-1"
    assert key_url("https://t.example/{key}", "A/../b?#") == "https://t.example/A%2F..%2Fb%3F%23"  # quoted


def test_config_keys_url():
    assert parse_config("").keys.url == ""
    assert parse_config(f'[keys]\nurl = "{URL}"\n').keys.url == URL
    assert parse_config('[keys]\nurl = ""\n').keys.url == ""


@pytest.mark.parametrize(
    "body",
    [
        'url = "https://tracker.example/issues/"',
        'url = "ftp://tracker.example/{key}"',
        'url = "javascript:{key}"',
        'url = "https://tracker.example/a b/{key}"',
        "url = 5",
        'url = "https://t.example/{key}"\nother = 1',
    ],
)
def test_config_keys_url_invalid(body):
    with pytest.raises(ConfigError, match="keys"):
        parse_config(f"[keys]\n{body}\n")


def test_example_config_has_the_commented_keys_table():
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    with open(os.path.join(root, "packaging/config/loadgov.toml.example")) as f:
        text = f.read()
    assert f'# url = "{URL}"' in text
    assert parse_config(text).keys.url == ""


# ---- estimates (pure) ------------------------------------------------------------------------------


def test_finish_estimate():
    assert finish_estimate(1000, 600, 1100) == (1600, False)
    assert finish_estimate(1000, 600, 1600) == (1600, False)  # exactly on time is not overdue
    assert finish_estimate(1000, 600, 1700) == (1600, True)  # the past value stays
    assert finish_estimate(1000, None, 1700) == (None, False)


def test_queue_starts_propagate_unknown():
    assert queue_starts(1600, [600, 300, 100]) == [1600, 2200, 2500]
    assert queue_starts(1600, [None, 300, 100]) == [1600, None, None]
    assert queue_starts(None, [600, 300]) == [None, None]
    assert queue_starts(1600, []) == []


# ---- the slot manager ------------------------------------------------------------------------------


def stat_text(pid):
    return f"{pid} (x) S " + "0 " * 18 + f"{pid} 0\n"


class Rig:
    def __init__(self, tmp_path, url=""):
        self.t = T0
        fs = HostFS(proc_root=str(tmp_path), lock_root=str(tmp_path))
        self.m = SlotManager(fs, clock=lambda: self.t, stat_reader=stat_text)
        self.m.url_template = url
        self.stats = {("proj", "gate"): 600.0, ("proj", "impl"): 100.0}
        self.m.expected_s = lambda project, kind: self.stats.get((project, kind))

    def acquire(self, pid, label="l", project="proj", **kw):
        return self.m.acquire("heavy", project, label, pid, True, **kw)

    def heavy(self):
        return self.m.summary()["heavy"]


def test_waiters_are_published_in_fifo_order(tmp_path):
    r = Rig(tmp_path, URL)
    assert r.heavy()["waiters"] == [] and r.heavy()["key"] is None
    assert r.acquire(10, "holder", kind="gate", key="PROJ-1")["status"] == "granted"
    r.t += 60
    assert r.acquire(11, "second", kind="gate", key="PROJ-2")["position"] == 1
    r.t += 30
    r.acquire(12, "third")  # no kind, no key
    r.t += 30
    r.acquire(11, "second", kind="gate", key="PROJ-2")  # a poll never moves it or resets queued_since
    h = r.heavy()
    assert (h["key"], h["url"], h["queue"]) == ("PROJ-1", "https://tracker.example/issues/PROJ-1", 2)
    first, second = h["waiters"]
    assert first == {
        "position": 1, "project": "proj", "label": "second", "kind": "gate", "key": "PROJ-2",
        "url": "https://tracker.example/issues/PROJ-2", "pid": 11, "queued_since": stamp(T0 + 60),
        "wait_s": 60, "expected_s": 600.0, "eta": stamp(T0 + 600),
    }  # fmt: skip
    assert (second["position"], second["kind"], second["key"], second["url"]) == (2, None, None, None)
    assert (second["wait_s"], second["expected_s"], second["eta"]) == (30, None, stamp(T0 + 1200))


def test_waiter_eta_known_then_unknown_behind(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="gate")
    r.acquire(11, "a", kind="gate")
    r.acquire(12, "b")  # unknown kind: its own start is known, nobody behind it
    r.acquire(13, "c", kind="gate")
    assert [w["eta"] for w in r.heavy()["waiters"]] == [stamp(T0 + 600), stamp(T0 + 1200), None]


def test_waiter_eta_unknown_without_statistics_for_the_holder(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="review")  # no stats for (proj, review)
    r.acquire(11, "a", kind="gate")
    assert r.heavy()["waiters"][0]["eta"] is None
    r2 = Rig(tmp_path)
    r2.acquire(10, "h")  # holder without a kind
    r2.acquire(11, "a", kind="gate")
    assert r2.heavy()["waiters"][0]["eta"] is None


def test_waiter_eta_unknown_when_the_holder_is_overdue(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="gate")
    r.acquire(11, "a", kind="gate")
    r.t += 601
    assert [w["eta"] for w in r.heavy()["waiters"]] == [None]


def test_waiter_eta_unknown_behind_a_hold_or_a_legacy_holder(tmp_path):
    r = Rig(tmp_path)
    r.m.hold_on(["heavy"], "owner is away")
    r.acquire(11, "a", kind="gate")
    h = r.heavy()
    assert h["state"] == "held-by-hold" and h["key"] is None and h["waiters"][0]["eta"] is None
    r2 = Rig(tmp_path)
    r2.m.slots["heavy"].legacy = {"line": None, "pid": None, "since": None}
    r2.m.slots["heavy"].queue.append(Waiter("proj", "a", 11, T0, kind="gate", since=T0))
    assert r2.heavy()["waiters"][0]["eta"] is None


def test_bind_scope_adds_the_kind_and_key_of_the_job(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h")
    assert r.m.bind_scope("heavy", 10, "u.scope", "/cg/u.scope", 20, "gate", "PROJ-9")
    h = r.m.slots["heavy"].holder
    assert (h.job_kind, h.key) == ("gate", "PROJ-9")
    r.acquire(11, "a", kind="gate")
    assert r.heavy()["waiters"][0]["eta"] == stamp(T0 + 600)  # the holder's estimate now has its kind


def test_audit_events_carry_the_key_only_when_present(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", key="PROJ-1")
    r.acquire(11, "a", key="PROJ-2")
    r.acquire(12, "b")
    reqs = {(e["result"], e["pid"]): e["request"] for e in r.m.drain_events() if e["op"] == "slot_acquire"}
    assert reqs[("granted", 10)]["key"] == "PROJ-1"
    assert reqs[("queued", 11)]["key"] == "PROJ-2"
    assert "key" not in reqs[("queued", 12)]


def test_saved_holder_keeps_the_key_and_an_old_state_file_still_loads(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="gate", key="PROJ-1")
    saved = json.loads(json.dumps(r.m.to_state()))
    assert saved["heavy"]["holder"]["key"] == "PROJ-1" and saved["heavy"]["holder"]["job_kind"] == "gate"
    r2 = Rig(tmp_path)
    r2.m.restore(saved)
    assert r2.m.slots["heavy"].holder.key == "PROJ-1"
    old = json.loads(json.dumps(saved))
    for k in ("key", "job_kind"):
        del old["heavy"]["holder"][k]  # a state file from before this change
    r3 = Rig(tmp_path)
    r3.m.restore(old)
    h = r3.m.slots["heavy"].holder
    assert h is not None and (h.key, h.job_kind) == (None, None)
    assert r3.heavy()["key"] is None


# ---- job statistics --------------------------------------------------------------------------------


def test_job_stats_expected_s_is_the_published_p50():
    st = JobStats()
    assert st.expected_s("proj", "gate") is None
    for d in (100, 300, 200):
        st.record("proj", "gate", {"duration_s": d})
    st.record("proj", "gate", {"duration_s": None})  # an unknown duration is left out
    assert st.expected_s("proj", "gate") == 200 == st.summary()[0]["duration_p50_s"]


# ---- the daemon ops --------------------------------------------------------------------------------


@pytest.fixture
def procs():
    started = []

    def spawn():
        p = subprocess.Popen(["sleep", "60"])
        started.append(p)
        return p

    yield spawn
    for p in started:
        p.kill()
        p.wait()


def make_daemon(fake_host, tmp_path, url=""):
    host = fake_host("calm")
    cfg = parse_config(f'[keys]\nurl = "{url}"\n')
    d = Daemon(
        host.fs, clock=lambda: d.t, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"),
        env_file=None, config=parity_config(keys=cfg.keys),
    )  # fmt: skip
    d.t = T0
    s = Sampler(host.fs)
    s.prime(host.prev_stat)
    d.status = s.sample(now=d.t)
    return d


def call(d, op, **args):
    return d.dispatch({"v": VERSION, "op": op, "args": args})


@pytest.mark.parametrize(
    ("key", "error"), [("nope", "invalid_args"), ("A-1" * 30, "invalid_args"), (42, "bad_request")]
)
def test_slot_acquire_rejects_a_bad_key_and_kind(fake_host, tmp_path, procs, key, error):
    d = make_daemon(fake_host, tmp_path)
    p = procs()
    r = call(d, "slot_acquire", slot="heavy", project="proj", label="l", pid=p.pid, wait=True, key=key)
    assert (r["ok"], r["error"]) == (False, error)
    r = call(d, "slot_acquire", slot="heavy", project="proj", label="l", pid=p.pid, wait=True, kind="bogus")
    assert (r["ok"], r["error"]) == (False, "invalid_args")
    assert d.slots.slots["heavy"].holder is None and not d.slots.slots["heavy"].queue


def test_job_start_rejects_a_bad_key(fake_host, tmp_path, procs):
    d = make_daemon(fake_host, tmp_path)
    p = procs()
    unit = "loadgov-gate-x-1.scope"
    r = call(d, "job_start", kind="gate", project="proj", label="l", unit=unit, cgroup=f"/a/{unit}",
             pid=p.pid, owner_pid=p.pid, key="bad key")  # fmt: skip
    assert (r["ok"], r["error"]) == (False, "invalid_args") and d.jobs.jobs == {}


def test_key_flows_from_the_queue_to_the_holder_the_job_and_the_audit_log(fake_host, tmp_path, procs):
    d = make_daemon(fake_host, tmp_path, "https://tracker.example/i/{key}")
    holder, waiter = procs(), procs()
    ask = {"slot": "heavy", "project": "proj", "wait": True, "kind": "gate"}
    r = call(d, "slot_acquire", label="h", pid=holder.pid, key="PROJ-1", **ask)
    assert r["status"] == "granted"
    r = call(d, "slot_acquire", label="w", pid=waiter.pid, key="PROJ-2", **ask)
    assert r["status"] == "queued"
    unit = "loadgov-gate-h-1.scope"
    r = call(d, "job_start", kind="gate", project="proj", label="h", unit=unit, cgroup=f"/a/{unit}",
             pid=holder.pid, owner_pid=holder.pid, slot="heavy", key="PROJ-1")  # fmt: skip
    assert r["ok"] and r["bound"]
    st = call(d, "status")["status"]
    assert [e for e in validate_status(st) if e.startswith(("slots.", "jobs["))] == []
    heavy, job = st["slots"]["heavy"], st["jobs"][0]
    assert (heavy["key"], heavy["url"]) == ("PROJ-1", "https://tracker.example/i/PROJ-1")
    w = heavy["waiters"][0]
    assert (w["key"], w["url"], w["kind"]) == ("PROJ-2", "https://tracker.example/i/PROJ-2", "gate")
    assert (job["key"], job["url"]) == ("PROJ-1", "https://tracker.example/i/PROJ-1")
    assert (job["expected_s"], job["eta"], job["overdue"]) == (None, None, False)  # no statistics yet
    assert call(d, "slot_status")["slots"]["heavy"]["waiters"][0]["key"] == "PROJ-2"
    call(d, "job_end", unit=unit)
    lines = [json.loads(x) for x in open(d.audit.path)]
    keyed = {(x["op"], x["result"]) for x in lines if x.get("request", {}).get("key") == "PROJ-1"}
    assert {("slot_acquire", "granted"), ("job_start", "accepted"), ("job_end", "ended")} <= keyed


def test_without_a_template_every_url_is_null(fake_host, tmp_path, procs):
    d = make_daemon(fake_host, tmp_path)
    p = procs()
    r = call(d, "slot_acquire", slot="heavy", project="proj", label="h", pid=p.pid, wait=True, key="PROJ-1")
    assert r["status"] == "granted"
    heavy = call(d, "status")["status"]["slots"]["heavy"]
    assert (heavy["key"], heavy["url"]) == ("PROJ-1", None)


def test_reload_applies_the_url_template(fake_host, tmp_path):
    d = make_daemon(fake_host, tmp_path)
    d._apply_config(parity_config(keys=parse_config(f'[keys]\nurl = "{URL}"').keys))
    assert d.slots.url_template == d.jobs.url_template == URL


def test_job_estimates_and_overdue(fake_host, tmp_path, procs):
    d = make_daemon(fake_host, tmp_path)
    p = procs()
    for dur in (100, 200, 300):
        d.jobs.stats.record("proj", "gate", {"duration_s": dur})
    unit = "loadgov-gate-h-1.scope"
    job = Job("gate", "proj", "h", unit, f"/a/{unit}", p.pid, None, p.pid, None, d.t, key="PROJ-1")
    d.jobs.jobs[unit] = job
    d.jobs.jobs["u2"] = Job("impl", "proj", "i", "u2", "/a/u2", p.pid, None, p.pid, None, d.t)
    d.t += 150
    gate, impl = d.jobs.summary()
    assert (gate["expected_s"], gate["eta"], gate["overdue"]) == (200, stamp(T0 + 200), False)
    assert (impl["expected_s"], impl["eta"], impl["overdue"], impl["key"]) == (None, None, False, None)
    d.t += 100
    gate = d.jobs.summary()[0]
    assert (gate["eta"], gate["overdue"]) == (stamp(T0 + 200), True)  # eta stays the past value


def test_job_restore_accepts_an_old_state_entry(fake_host, tmp_path, procs):
    d = make_daemon(fake_host, tmp_path)
    p = procs()
    unit = "loadgov-gate-h-1.scope"
    d.jobs.jobs[unit] = Job(
        "gate", "proj", "h", unit, f"/a/{unit}", p.pid, None, p.pid, None, d.t, key="PROJ-1"
    )
    saved = d.jobs.to_state()
    assert saved[0]["key"] == "PROJ-1"
    del saved[0]["key"]
    d.jobs.jobs.clear()
    d.jobs.restore(saved)
    assert d.jobs.jobs[unit].key is None


# ---- the CLI ---------------------------------------------------------------------------------------


class Args:
    def __init__(self, key):
        self.key = key


def test_resolve_key_flag_then_environment():
    assert loadctl.resolve_key(Args("A-1"), {"LOADGOV_KEY": "B-2"}) == ("A-1", None)
    assert loadctl.resolve_key(Args(None), {"LOADGOV_KEY": "B-2"}) == ("B-2", None)
    assert loadctl.resolve_key(Args(None), {"LOADGOV_KEY": ""}) == (None, None)  # empty = unset
    assert loadctl.resolve_key(Args(None), {}) == (None, None)
    key, why = loadctl.resolve_key(Args("bad"), {})
    assert key is None and "PROJ-123" in why
    assert loadctl.resolve_key(Args(None), {"LOADGOV_KEY": "bad"})[1]  # a bad default is refused too


def acquire(monkeypatch, argv, env=None):
    sent = []

    def fake_request(sock, op, args=None):
        sent.append((op, args))
        return {"ok": True, "status": "granted", "holder": "x"}

    monkeypatch.setattr(loadctl, "request", fake_request)
    monkeypatch.delenv("LOADGOV_KEY", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    base = ["slot", "acquire", "heavy", "--project", "p", "--label", "l", "--no-lock", "--pid", "5"]
    return loadctl.main([*base, *argv]), sent


def test_slot_acquire_sends_the_key(monkeypatch, capsys):
    rc, sent = acquire(monkeypatch, ["--key", "PROJ-7"], {"LOADGOV_KEY": "OTHER-1"})
    assert rc == 0 and sent[0][1]["key"] == "PROJ-7"
    rc, sent = acquire(monkeypatch, [], {"LOADGOV_KEY": "OTHER-1"})
    assert rc == 0 and sent[0][1]["key"] == "OTHER-1"  # the environment is the default
    rc, sent = acquire(monkeypatch, [])
    assert rc == 0 and "key" not in sent[0][1] and "kind" not in sent[0][1]
    rc, sent = acquire(monkeypatch, ["--key", "nonsense"])
    assert rc == 2 and sent == [] and "PROJ-123" in capsys.readouterr().err  # refused before any request


# ---- human output ----------------------------------------------------------------------------------


def test_format_waiters_and_jobs():
    sl = {"waiters": [
        {"position": 1, "project": "web-app", "label": "x", "kind": "gate", "key": "PROJ-42", "wait_s": 190,
         "eta": "2026-11-21T17:40:00Z"},
        {"position": 2, "project": "web-app", "label": "lbl", "kind": None, "key": None, "wait_s": 5, "eta": None},
    ]}  # fmt: skip
    first, second = loadctl.format_waiters(sl)
    assert first.startswith("next: 1. web-app gate PROJ-42 waiting 3m eta ")
    assert second == "      2. web-app lbl waiting 5s"
    row = {"project": "web-app", "kind": "gate", "label": "x", "key": "PROJ-42", "pid": 5, "since": "S",
           "eta": "2026-11-21T17:40:00Z", "overdue": False}  # fmt: skip
    assert loadctl.format_jobs([row]).startswith("web-app gate PROJ-42 pid 5 since S eta ")
    assert loadctl.format_jobs([{**row, "overdue": True}]).endswith(" overdue")
    assert loadctl.format_jobs([{**row, "eta": None}]) == "web-app gate PROJ-42 pid 5 since S"


def test_format_waiters_tolerates_a_partial_payload():
    sl = {"waiters": [{"position": 1, "project": "p", "label": "l", "wait_s": 3}]}
    assert loadctl.format_waiters(sl) == ["next: 1. p l waiting 3s"]


def _holder_with_waiter(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="gate")
    r.acquire(11, "a", kind="gate")
    assert r.heavy()["waiters"][0]["eta"] == stamp(T0 + 600)
    return r


def test_a_pending_owner_hold_behind_a_job_holder_makes_every_eta_unknown(tmp_path):
    r = _holder_with_waiter(tmp_path)
    r.m.hold_on(["heavy"], "owner is away")
    h = r.heavy()
    assert h["hold"] and h["state"] == "held" and h["waiters"][0]["eta"] is None
    r.m.hold_off(["heavy"])
    assert r.heavy()["waiters"][0]["eta"] == stamp(T0 + 600)  # the estimate returns


def test_a_pending_drain_profile_hold_makes_every_eta_unknown(tmp_path):
    r = _holder_with_waiter(tmp_path)
    r.m.set_profile_hold(True)
    assert r.heavy()["hold_origin"] == "profile" and r.heavy()["waiters"][0]["eta"] is None
    r.m.set_profile_hold(False)
    assert r.heavy()["waiters"][0]["eta"] == stamp(T0 + 600)


def test_first_waiter_eta_equals_the_bound_job_eta(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h")
    r.t += 40  # the headroom wait: the job registers after the grant
    assert r.m.bind_scope("heavy", 10, "u.scope", "/cg/u.scope", 20, "gate", None, r.t)
    r.acquire(11, "a", kind="gate")
    job = Job("gate", "proj", "h", "u.scope", "/cg/u.scope", 20, None, 10, "heavy", r.t)
    eta, _ = finish_estimate(job.since, r.stats[("proj", "gate")], r.t)
    assert r.heavy()["waiters"][0]["eta"] == stamp(eta) == stamp(T0 + 40 + 600)


def test_unbound_holder_eta_uses_the_grant_time(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h", kind="gate")
    r.t += 40
    r.acquire(11, "a", kind="gate")
    assert r.heavy()["waiters"][0]["eta"] == stamp(T0 + 600)


def test_saved_holder_without_job_since_still_loads(tmp_path):
    r = Rig(tmp_path)
    r.acquire(10, "h")
    r.m.bind_scope("heavy", 10, "u.scope", "/cg/u.scope", 20, "gate", None, T0 + 5)
    saved = r.m.to_state()
    assert saved["heavy"]["holder"]["job_since"] == T0 + 5
    del saved["heavy"]["holder"]["job_since"]  # a state.json written before the field existed
    r2 = Rig(tmp_path)
    r2.m.restore(saved)
    h = r2.m.slots["heavy"].holder
    assert h is not None and h.job_since is None


def test_job_start_wires_the_registration_time_into_the_slot_eta(fake_host, tmp_path, monkeypatch):
    monkeypatch.setattr("loadgov.daemon.slot_ops.pid_alive", lambda pid, *a: True)  # fake pids, no real /proc
    d = make_daemon(fake_host, tmp_path)
    d.slots.stat_reader = d.jobs.stat_reader = stat_text  # every pid is a live fake process; no real /proc
    d.jobs.stats.record("proj", "gate", {"duration_s": 600.0})
    ask = {"slot": "heavy", "project": "proj", "wait": True, "kind": "gate"}
    assert call(d, "slot_acquire", label="h", pid=4001, **ask)["status"] == "granted"
    d.t += 40  # the headroom wait between the grant and the job registration
    unit = "loadgov-gate-h-1.scope"
    r = call(d, "job_start", kind="gate", project="proj", label="h", unit=unit, cgroup=f"/a/{unit}",
             pid=4002, owner_pid=4001, slot="heavy")  # fmt: skip
    assert r["ok"] and r["bound"]
    d.t += 5
    assert call(d, "slot_acquire", label="w", pid=4003, **ask)["status"] == "queued"
    st = call(d, "status")["status"]
    job, waiter = st["jobs"][0], st["slots"]["heavy"]["waiters"][0]
    assert job["eta"] == stamp(T0 + 40 + 600)  # registration time + p50, not the grant time
    assert waiter["eta"] == job["eta"]
