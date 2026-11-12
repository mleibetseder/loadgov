"""loadgov-mcp against a fake daemon on a real unix socket (server tests need the `mcp` extra)."""

import asyncio
import json
import re
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from loadgov.mcp.server import ToolFailure, Tools

ROOT = Path(__file__).resolve().parents[2]


class FakeDaemon:
    def __init__(self, handlers):
        self.handlers = handlers
        self.requests = []
        self.dir = tempfile.mkdtemp(prefix="lgmcp")
        self.path = self.dir + "/s.sock"
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(8)
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf)
                self.requests.append(req)
                h = self.handlers.get(req["op"])
                reply = h(req["args"]) if h else {"ok": False, "error": "unknown_op", "message": req["op"]}
                c.sendall((json.dumps(reply) + "\n").encode())

    def close(self):
        self.srv.close()


LIMITS = {
    "profile": "silent",
    "effective": {"max_cores": 2},
    "ceiling": "silent",
    "ceiling_info": {
        "profile": "silent",
        "reason": "fan at night",
        "principal": "owner",
        "set_at": "t",
        "expires_at": None,
    },
    "profile_info": None,
    "overrides": [],
    "restricted": True,
    "restricted_by": "owner",
}


def status_reply(caps=None):
    s = {
        "at": "t", "level": "OK", "levels": {}, "cpu": {}, "memory": {}, "swap": {}, "io": {},
        "disk": {}, "limits": LIMITS, "slots": {}, "jobs": [],
    }  # fmt: skip
    if caps is not None:
        s["capabilities"] = caps
    return {"ok": True, "status": s}


def set_handler(args):
    if args.get("max_cores", 0) > 2:
        return {
            "ok": False, "refused": True, "restrictedBy": "owner",
            "reason": "fan at night", "ceiling": "silent",
        }  # fmt: skip
    return {"ok": True, "accepted": True, "principal": "agent"}


def counting(seq):
    calls = []

    def h(args):
        calls.append(args)
        return seq(len(calls))

    h.calls = calls
    return h


def stamp(token):
    """A distinct real timestamp per test token ("a", "b", "7", ...): replies carry `sample_at` strings."""
    n = int(token) if str(token).isdigit() else sum(ord(c) for c in str(token))
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_762_800_000 + n))


def headroom_reply(ok, at, reasons=()):
    return {
        "ok": True,
        "sample_at": stamp(at),
        "timing": {"confirm_s": 1, "poll_s": 1, "max_wait_s": 99},
        "decision": {
            "pass": ok,
            "reasons": list(reasons),
            "values": {
                "load1": 1,
                "load5": 1,
                "cpus": 8,
                "cpu_psi": 0,
                "mem_gb": 20.0,
                "swap_mb": 0,
                "mem_psi": 0,
            },
        },
        "thresholds": {"load1_max": 4.0, "load5_max": None, "psi_max": 25, "mem_min_gb": 4},
    }


@pytest.fixture
def daemon():
    made = []

    def make(**handlers):
        d = FakeDaemon({"status": lambda a: status_reply(), "set": set_handler, **handlers})
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def test_set_limits_refused_under_owner_ceiling_and_never_sends_owner(daemon):
    d = daemon()
    t = Tools(d.path)
    r = t.set_limits("need cores", max_cores=4, agent_id="impl-3", project="p")
    assert r["refused"] is True and r["restrictedBy"] == "owner" and r["reason"] == "fan at night"
    args = d.requests[-1]["args"]
    assert "owner" not in args and args["reason"] == "[agent impl-3] need cores" and args["project"] == "p"
    assert t.set_limits("lower", max_cores=1)["accepted"] is True
    assert all("owner" not in q["args"] for q in d.requests)


def test_set_limits_needs_reason_and_a_limit(daemon):
    t = Tools(daemon().path)
    with pytest.raises(ToolFailure, match="reason"):
        t.set_limits("  ", max_cores=1)
    with pytest.raises(ToolFailure, match="at least one"):
        t.set_limits("why")


def test_unreachable_daemon_is_a_clear_error(tmp_path):
    t = Tools(str(tmp_path / "none.sock"))
    calls = (
        t.get_status,
        t.explain,
        lambda: t.set_limits("r", max_cores=1),
        lambda: t.wait_headroom("impl", 5),
    )
    for call in calls:
        with pytest.raises(ToolFailure, match="loadgovd not reachable"):
            call()
    assert t.capabilities() == []


def test_wait_headroom_needs_a_newer_second_sample(daemon):
    h = counting(lambda n: headroom_reply(True, "a" if n <= 2 else "b"))
    r = Tools(daemon(headroom=h).path, sleep=lambda s: None).wait_headroom("impl", 60, "p")
    assert r["result"] == "ok" and len(h.calls) == 3  # the second sample was stale (same sample_at)
    assert h.calls[0] == {"kind": "impl", "project": "p"}


def test_wait_headroom_times_out_with_reasons_and_caps(daemon):
    h = counting(lambda n: headroom_reply(False, str(n), ["load1 5.40 >= 4.00"]))
    t = Tools(daemon(headroom=h).path, sleep=lambda s: None)
    r = t.wait_headroom("gate", 3)
    assert r["result"] == "timeout" and r["reasons"] == ["load1 5.40 >= 4.00"] and "loadctl wait" in r["hint"]
    assert r["capped_at_sec"] is None
    assert t.wait_headroom("gate", 99999)["capped_at_sec"] == 1800.0


def test_wait_headroom_rejects_bad_input(daemon):
    t = Tools(daemon().path)
    with pytest.raises(ToolFailure):
        t.wait_headroom("nope", 5)
    for bad in (0, -1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ToolFailure, match="finite"):
            t.wait_headroom("impl", bad)


def test_wait_never_sleeps_past_the_budget(daemon):
    h = counting(lambda n: headroom_reply(False, str(n)))
    d = daemon(headroom=h)
    slept = []
    d.handlers["headroom"] = lambda a: {
        **h(a),
        "timing": {"confirm_s": 120, "poll_s": 120, "max_wait_s": 99},
    }
    r = Tools(d.path, sleep=slept.append).wait_headroom("impl", 5)
    assert r["result"] == "timeout" and slept == [5]


def test_mcp_confirm_gap_is_full_even_with_less_budget_left(daemon):
    h = counting(lambda n: headroom_reply(True, str(n)))
    d = daemon(headroom=h)
    d.handlers["headroom"] = lambda a: {**h(a), "timing": {"confirm_s": 60, "poll_s": 30, "max_wait_s": 99}}
    slept = []
    r = Tools(d.path, sleep=slept.append).wait_headroom("impl", 5)
    assert r["result"] == "ok" and slept == [60]


def test_overlapping_waits_keep_their_own_lines_and_stderr(daemon):
    import sys

    before = sys.stderr
    h = counting(lambda n: headroom_reply(False, str(n), ["r"]))
    d = daemon(headroom=h)
    results = []
    barrier = threading.Barrier(2)

    def slow(s):
        barrier.wait(timeout=5)

    def run(kind):
        results.append(Tools(d.path, sleep=slow).wait_headroom(kind, 2, kind))

    ts = [threading.Thread(target=run, args=(k,)) for k in ("impl", "gate")]
    [x.start() for x in ts]
    [x.join(10) for x in ts]
    assert sys.stderr is before and len(results) == 2
    assert {r["kind"] for r in results} == {"impl", "gate"}
    assert all("HEADROOM_TIMEOUT mcp" in r["line"][0] for r in results)


def test_explain_shows_freeze_and_memguard_paused(daemon):
    st = status_reply(["freeze"])
    st["status"]["memguard"] = {"enforce": False, "paused": ["x", "y"]}
    d = daemon(
        status=lambda a: st,
        limits=lambda a: {"ok": True, "limits": {"restriction": {}, "freeze": "memory"}},
    )
    e = Tools(d.path).explain()
    assert e["freeze"] == "memory" and e["memguard_paused"] == ["x", "y"]
    assert Tools(d.path).get_status()["memguard"]["enforce"] is False


def test_explain_and_status(daemon):
    restriction = {"active": True, "kind": "owner", "reason": "fan at night"}
    d = daemon(
        limits=lambda a: {
            "ok": True,
            "limits": {"restriction": restriction, "overrides": [], "hold": False, "freeze": False},
        }
    )
    t = Tools(d.path)
    e = t.explain()
    assert e["restricted"] is True and any("fan at night" in w for w in e["why"])
    s = t.get_status()
    assert s["limits"]["ceiling_info"]["reason"] == "fan at night" and s["capabilities"] == []


def test_optional_ops_return_daemon_reply_verbatim(daemon):
    refusal = {"ok": False, "error": "owner_denied", "message": "freeze is owner-only"}
    d = daemon(freeze=lambda a: refusal)
    assert Tools(d.path).freeze("x") == refusal
    assert "owner" not in d.requests[-1]["args"]


def test_skill_frontmatter_lint():
    skills = list((ROOT / "skills").glob("*/SKILL.md"))
    assert skills
    for skill in skills:
        m = re.match(r"---\n(.*?)\n---\n", skill.read_text(), re.S)
        assert m, skill
        fm = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        assert fm["name"].strip() == skill.parent.name and len(fm["description"].strip()) > 40
    for f in [*(ROOT / "skills").rglob("*.md"), ROOT / "docs" / "mcp.md"]:
        assert "/root" + "/" not in f.read_text(), f


# ---- server (mcp extra) -------------------------------------------------------------------------


def tool_names(srv):
    return sorted(x.name for x in asyncio.run(srv.list_tools()))


def test_tool_listing_follows_capabilities(daemon):
    pytest.importorskip("mcp")
    from loadgov.mcp.server import build_server

    caps = []
    d = daemon(status=lambda a: status_reply(list(caps)))
    srv = build_server(Tools(d.path))
    base = ["load_explain", "load_get_status", "load_set_limits", "load_wait_headroom"]
    assert tool_names(srv) == base
    caps.extend(["reap", "freeze"])
    assert tool_names(srv) == sorted([*base, "load_reap_idle_daemons", "load_freeze", "load_unfreeze"])
    caps[:] = ["reap"]
    assert tool_names(srv) == sorted([*base, "load_reap_idle_daemons"])
    caps.clear()
    assert tool_names(srv) == base


def test_server_tool_call_and_unreachable(daemon, tmp_path):
    pytest.importorskip("mcp")
    from mcp.server.fastmcp.exceptions import ToolError

    from loadgov.mcp.server import build_server

    srv = build_server(Tools(daemon().path))
    res = asyncio.run(srv.call_tool("load_set_limits", {"reason": "r", "max_cores": 4}))
    assert "restrictedBy" in str(res)
    down = build_server(Tools(str(tmp_path / "none.sock")))
    with pytest.raises(ToolError, match="loadgovd not reachable"):
        asyncio.run(down.call_tool("load_get_status", {}))
