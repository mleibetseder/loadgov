"""REST per-client tokens (owner and read scope) and the read-only run endpoints (ILM-46).

Same rig as test_rest.py: loopback listeners, fake host, run records in a tmp_path tree. The tokens here are
random per run; nothing real is read or printed.
"""

import asyncio
import json
import os
import secrets
from urllib.parse import parse_qs

import pytest
from test_rest import TOKEN, audit, http, make, run_rest
from test_ui import FakeDaemon, cfg, make_run, write_log

from loadgov.daemon.rest import DEFAULT_CLIENT, POST_KEYS, TokenError, load_clients
from loadgov.daemon.ui import UiServer

OWNER2 = secrets.token_urlsafe(32)
READ = secrets.token_urlsafe(32)
READ2 = secrets.token_urlsafe(32)


def tokens_file(tmp_path, *lines, default=TOKEN):
    p = tmp_path / "tokens.env"
    head = [f"LOADGOV_REST_TOKEN={default}"] if default else []
    p.write_text("\n".join(["# test", *head, *lines]) + "\n")
    p.chmod(0o600)
    return str(p)


def two_clients(tmp_path):
    return tokens_file(
        tmp_path,
        f"LOADGOV_REST_CLIENT=ha {OWNER2} scope=owner",
        f'LOADGOV_REST_CLIENT="fleet {READ} scope=read"',
    )


def rest_daemon(tmp_path, file, fake_host=None):
    return make(tmp_path, fake_host("calm") if fake_host else None, token_file=file,
                runs_dir=str(tmp_path / "runs"))  # fmt: skip


# ---- token file ---------------------------------------------------------------------------------------


def test_old_single_token_file_is_client_default_owner(tmp_path):
    clients = load_clients(tokens_file(tmp_path))
    assert [(c.name, c.token, c.scope) for c in clients] == [(DEFAULT_CLIENT, TOKEN.encode(), "owner")]


def test_named_entries_are_read_beside_the_default(tmp_path):
    clients = load_clients(two_clients(tmp_path))
    assert [(c.name, c.scope) for c in clients] == [("default", "owner"), ("ha", "owner"), ("fleet", "read")]
    only_named = load_clients(
        tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=read", default=None)
    )
    assert [(c.name, c.scope) for c in only_named] == [("fleet", "read")]


@pytest.mark.parametrize(
    "line, word",
    [
        (
            f"LOADGOV_REST_CLIENT=default {READ} scope=read",
            "duplicate",
        ),  # clashes with the single-token client
        (
            f"LOADGOV_REST_CLIENT=fleet {READ} scope=read\nLOADGOV_REST_CLIENT=fleet {READ2} scope=read",
            "duplicate",
        ),
        (f"LOADGOV_REST_CLIENT=fleet {TOKEN} scope=read", "shared by two clients"),
        ("LOADGOV_REST_CLIENT=fleet tooshort scope=read", "at least 32 random bytes"),
        (f"LOADGOV_REST_CLIENT=fleet {READ} scope=admin", "scope must be"),
        (f"LOADGOV_REST_CLIENT=fleet {READ}", "expected"),
        (f"LOADGOV_REST_CLIENT=Fleet {READ} scope=read", "name must match"),
        (f"LOADGOV_REST_CLIENT=a_b {READ} scope=read", "name must match"),
        (f"LOADGOV_REST_CLIENT={'x' * 33} {READ} scope=read", "name must match"),
        (f"LOADGOV_REST_CLIENT=fleet {READ} scope=read extra", "expected"),
    ],
)
def test_bad_entries_are_refused_without_echoing_a_token(tmp_path, line, word):
    with pytest.raises(TokenError, match=word) as e:
        load_clients(
            tokens_file(
                tmp_path,
                line,
            )
        )
    assert READ not in str(e.value) and OWNER2 not in str(e.value) and TOKEN not in str(e.value)


def test_a_bad_entry_keeps_rest_down_and_the_file_mode_is_still_checked(tmp_path, fake_host):
    d = rest_daemon(tmp_path, tokens_file(tmp_path, "LOADGOV_REST_CLIENT=fleet short scope=read"), fake_host)
    d.tick()

    async def go():
        task = asyncio.create_task(d.rest.supervise())
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert d.rest.state == "down" and "entry 1" in d.rest.error
    f = tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=read")
    os.chmod(f, 0o640)
    with pytest.raises(TokenError, match="accessible by group/other"):
        load_clients(f)


# ---- scopes -------------------------------------------------------------------------------------------


def test_each_token_acts_as_its_own_client(tmp_path, fake_host):
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)
    d.tick()

    async def scenario(port):
        for tok in (TOKEN, OWNER2, READ):
            assert (await http(port, token=tok))[0] == 200
        assert (await http(port, token=READ[:-1] + "x"))[0] == 401
        body = {"max_light": 2, "reason": "t"}
        assert (await http(port, "POST", "/v1/set", body, token=OWNER2))[0] == 200
        assert (await http(port, "POST", "/v1/set", body, token=TOKEN))[0] == 200

    run_rest(d, scenario)
    clients = [r["client"] for r in audit(d) if r.get("source") == "rest"]
    assert clients == ["ha", "default"]  # the audit line names who asked


def test_read_token_gets_get_and_403_read_only_on_every_write_path(tmp_path, fake_host):
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)
    d.tick()
    write_paths = sorted(POST_KEYS)
    assert write_paths == [
        "/v1/action", "/v1/cancel-holder", "/v1/ceiling", "/v1/freeze", "/v1/hold", "/v1/image/cancel",
        "/v1/image/now", "/v1/reap", "/v1/reset", "/v1/set", "/v1/stray/kill", "/v1/unfreeze",
    ]  # fmt: skip

    async def scenario(port):
        for path in ("/v1/status", "/v1/fleet", "/v1/runs"):
            assert (await http(port, path=path, token=READ))[0] in (200, 503), path
        for path in write_paths:
            code, body, _ = await http(port, "POST", path, {"reason": "t"}, token=READ)
            assert (code, body["error"]) == (403, "read_only"), path
        for method in ("POST", "PUT", "DELETE", "PATCH"):  # any path that is not a GET, known or not
            for path in ("/v1/set", "/v1/nothing", "/v1/runs", "/v1/status", "/"):
                code, body, _ = await http(port, method, path, {}, token=READ)
                assert (code, body["error"]) == (403, "read_only"), (method, path)

    run_rest(d, scenario)
    refused = [r for r in audit(d) if r["op"] == "rest_write"]
    assert len(refused) == len(write_paths) + 20
    assert {r["client"] for r in refused} == {"fleet"} and {r["result"] for r in refused} == {"refused"}
    assert (
        refused[0]["request"] == {"method": "POST", "path": write_paths[0]} and refused[0]["source"] == "rest"
    )
    assert READ not in open(d.audit.path).read()  # never the token


def test_read_token_changes_nothing(tmp_path, fake_host):
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)
    d.tick()
    before = json.dumps(d.dispatch({"v": 1, "op": "status"})["status"]["limits"], sort_keys=True)

    async def scenario(port):
        assert (await http(port, "POST", "/v1/set", {"max_light": 1, "reason": "t"}, token=READ))[0] == 403

    run_rest(d, scenario)
    assert json.dumps(d.dispatch({"v": 1, "op": "status"})["status"]["limits"], sort_keys=True) == before


def test_reload_picks_up_a_new_token_and_keeps_the_old_set_on_a_bad_file(tmp_path, fake_host):
    f = tokens_file(tmp_path)
    d = rest_daemon(tmp_path, f, fake_host)
    d.tick()

    async def scenario(port):
        assert (await http(port, token=READ))[0] == 401
        tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=read")
        d.rest.reload_tokens()
        assert (await http(port, token=READ))[0] == 200 and (await http(port, token=TOKEN))[0] == 200
        tokens_file(tmp_path, "LOADGOV_REST_CLIENT=fleet short scope=read")
        d.rest.reload_tokens()  # refused: the working set stays
        assert (await http(port, token=READ))[0] == 200
        tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ2} scope=read", default=None)  # rotation
        d.rest.reload_tokens()
        assert [(await http(port, token=t))[0] for t in (READ, READ2, TOKEN)] == [401, 200, 401]

    run_rest(d, scenario)


# ---- run endpoints ------------------------------------------------------------------------------------


def test_runs_list_bounds_and_shape(tmp_path, fake_host):
    _, a = make_run(tmp_path, now=1_762_800_000.0)
    _, b = make_run(tmp_path, now=1_762_900_000.0)
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)

    async def scenario(port):
        code, r, _ = await http(port, path="/v1/runs", token=READ)
        assert code == 200 and [x["id"] for x in r["runs"]] == [b, a] and r["damaged"] == 0
        assert r["runs"][0]["job_count"] == 4 and "jobs" not in r["runs"][0]
        assert [x["id"] for x in (await http(port, path="/v1/runs?limit=1", token=READ))[1]["runs"]] == [b]
        assert (await http(port, path="/v1/runs?limit=200", token=READ))[0] == 200
        for bad in ("limit=0", "limit=201", "limit=x", "limit=-1", "limit=1e3"):
            assert (await http(port, path=f"/v1/runs?{bad}", token=READ))[0] == 400, bad
        assert (await http(port, "POST", "/v1/runs", {}, token=TOKEN))[0] == 405  # owner: GET only too

    run_rest(d, scenario)


def test_the_run_has_jobs_with_levels_like_the_ui(tmp_path, fake_host):
    _, rid = make_run(tmp_path)
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)

    async def scenario(port):
        code, r, _ = await http(port, path=f"/v1/runs/{rid}", token=READ)
        run = r["run"]
        assert code == 200 and run["levels"] == [["build", "lint"], ["test"], ["ship"]]
        assert {j["key"]: j["level"] for j in run["jobs"]} == {"build": 1, "test": 2, "lint": 1, "ship": 3}
        assert len(run["jobs"][0]["steps"]) == 2
        for bad in ("nope", "20260101T000000Z-ZZZZZZ", "..", "%2e%2e"):
            assert (await http(port, path=f"/v1/runs/{bad}", token=READ))[0] == 400, bad
        code, r, _ = await http(port, path="/v1/runs/20260101T000000Z-abcdef", token=READ)
        assert (code, r["error"]) == (404, "not_found")  # an exact id that is not there
        assert (await http(port, path=f"/v1/runs/{rid}/nothing", token=READ))[0] == 404

    run_rest(d, scenario)


def test_a_damaged_record_is_422_and_skipped_in_the_list(tmp_path, fake_host):
    _, bad = make_run(tmp_path)
    _, good = make_run(tmp_path, now=1_762_700_000.0)
    (tmp_path / "runs" / bad / "run.json").write_text("{not json")
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)

    async def scenario(port):
        code, r, _ = await http(port, path=f"/v1/runs/{bad}", token=READ)
        assert (code, r["error"]) == (422, "damaged_record") and "Traceback" not in json.dumps(r)
        code, r, _ = await http(port, path="/v1/runs", token=READ)
        assert code == 200 and [x["id"] for x in r["runs"]] == [good] and r["damaged"] == 1
        assert (await http(port, path=f"/v1/runs/{good}", token=READ))[0] == 200

    run_rest(d, scenario)


def test_step_log_paging_and_addresses(tmp_path, fake_host):
    _, rid = make_run(tmp_path)
    write_log(tmp_path, rid, "build", 1, b"0123456789" * 10)
    base = f"/v1/runs/{rid}/jobs/build/steps/1/log"
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)

    async def scenario(port):
        r = (await http(port, path=f"{base}?limit=30", token=READ))[1]
        assert (r["text"], r["offset"], r["next_offset"], r["size"], r["eof"]) == (
            "0123456789" * 3,
            0,
            30,
            100,
            False,
        )
        r = (await http(port, path=f"{base}?offset=30&limit=1000", token=READ))[1]
        assert (len(r["text"]), r["next_offset"], r["eof"], r["status"]) == (70, 100, True, "success")
        for bad in ("limit=0", "limit=262145", "offset=-1", "offset=x"):
            assert (await http(port, path=f"{base}?{bad}", token=READ))[0] == 400, bad
        for path in (
            f"/v1/runs/{rid}/jobs/missing/steps/1/log",
            f"/v1/runs/{rid}/jobs/..%2Fbuild/steps/1/log",
            f"/v1/runs/{rid}/jobs/build/steps/3/log",
            f"/v1/runs/{rid}/jobs/build/steps/0/log",
        ):
            assert (await http(port, path=path, token=READ))[0] == 404, path

    run_rest(d, scenario)


def test_the_ui_and_rest_answer_the_same_run_json(tmp_path, fake_host):
    _, rid = make_run(tmp_path)
    write_log(tmp_path, rid, "build", 1, b"hello\n")
    d = rest_daemon(tmp_path, two_clients(tmp_path), fake_host)
    ui = UiServer(FakeDaemon(), cfg(tmp_path), allow_loopback=True, peer_addr="192.0.2.30")
    paths = ["runs", f"runs/{rid}", f"runs/{rid}/jobs/build/steps/1/log", "runs?limit=0", "runs/bad"]
    seen = {}

    async def scenario(port):
        for p in paths:
            code, body, _ = await http(port, path=f"/v1/{p}", token=READ)
            seen[p] = (code, body)

    run_rest(d, scenario)

    async def via_ui():
        for p in paths:
            path, _, query = p.partition("?")
            code, _, body = await ui._api(path.split("/"), parse_qs(query))
            assert (code, json.loads(body)) == seen[p], p

    asyncio.run(via_ui())


# ---- a refused reload is visible (ILM-46 follow-up) ---------------------------------------------------


def test_a_refused_reload_sets_shows_and_clears_the_warning(tmp_path, fake_host):
    from loadgov.cli import loadctl
    from loadgov.protocol import validate_status

    d = rest_daemon(tmp_path, tokens_file(tmp_path), fake_host)
    d.tick()

    async def scenario(port):
        assert d.rest.status()["tokens_warning"] is None
        tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=admin")
        d.rest.reload_tokens()
        w = d.rest.tokens_warning  # set: names the entry, never a token
        assert "entry 1" in w and "scope must be" in w and READ not in w and TOKEN not in w
        assert (await http(port, token=TOKEN))[0] == 200  # the working tokens stay
        st = d.dispatch({"v": 1, "op": "status"})["status"]
        assert st["rest"]["tokens_warning"] == w and validate_status(st) == []
        shown = [ln for ln in loadctl.format_status(st).splitlines() if "warn  rest" in ln]
        assert len(shown) == 1 and "entry 1" in shown[0]  # shown
        d.rest.reload_tokens()  # a second refusal: another audit line
        tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=read")
        d.rest.reload_tokens()
        assert d.rest.tokens_warning is None  # cleared
        st = d.dispatch({"v": 1, "op": "status"})["status"]
        assert not [ln for ln in loadctl.format_status(st).splitlines() if "warn  rest" in ln]

    run_rest(d, scenario)
    rows = [r for r in audit(d) if r["op"] == "rest_tokens"]
    assert [(r["result"], r["principal"]) for r in rows] == [("refused", "system")] * 2
    assert "entry 1" in rows[0]["reason"] and READ not in open(d.audit.path).read()


def test_duplicate_and_shared_token_errors_name_the_entries(tmp_path):
    with pytest.raises(TokenError, match=r"duplicate client name 'fleet': .*entry 2 repeats .*entry 1"):
        load_clients(tokens_file(
            tmp_path, f"LOADGOV_REST_CLIENT=fleet {READ} scope=read",
            f"LOADGOV_REST_CLIENT=fleet {READ2} scope=read",
        ))  # fmt: skip
    with pytest.raises(TokenError, match=r"shared by two clients: .*entry 1 repeats LOADGOV_REST_TOKEN"):
        load_clients(tokens_file(tmp_path, f"LOADGOV_REST_CLIENT=fleet {TOKEN} scope=read"))


def test_sighup_reload_path_calls_reload_tokens(tmp_path, fake_host):
    d = rest_daemon(tmp_path, tokens_file(tmp_path), fake_host)
    calls = []
    d.rest.reload_tokens = lambda: calls.append(1)
    d.request_reload()  # what the SIGHUP handler does; the main loop then runs reload_config
    assert d._reload is True
    d.reload_config()  # no config file: refused, but the tokens were still re-read first
    assert calls == [1] and d._reload is False
