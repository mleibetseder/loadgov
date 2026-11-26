"""The per-host web UI listener: allowlist, static serving rules, the JSON endpoints, damaged records.

Every listener binds 127.0.0.1 on an ephemeral port; the loopback bind check is skipped through the test-only
UiServer argument. Build and runs directories are tmp_path trees with fake files; no host access.
"""

import asyncio
import json
import os

import pytest

from loadgov.config import ConfigError, UiConfig, parse_config
from loadgov.daemon import runs_api
from loadgov.daemon.main import main as daemon_main
from loadgov.daemon.ui import HASHED, UiServer, decode_clean, levels_of
from loadgov.workflow.records import RunRecordError, RunStore, now_iso

STATUS = {
    "at": "2026-11-26T10:00:00Z",
    "host": {"id": "build-a", "mode": "govern", "cores": 8},
    "level": "ok",
    "slots": {
        "heavy": {"state": "held", "holder": "web-app gate", "waiters": [{"position": 1, "eta": None}]}
    },
    "light": {"running": 1, "max": 4},
    "image": {"source": "config", "window": "01:00-05:00", "env_allow": ["PATH"], "queue": []},
    "rest": {"enabled": True},
}


class FakeDaemon:
    def __init__(self, reply=None):
        self.reply = reply or {"ok": True, "status": STATUS}

    def dispatch(self, req):
        assert req == {"v": 1, "op": "status"}  # the UI only ever asks for the status
        return self.reply


def cfg(tmp_path, **kw):
    build = tmp_path / "build"
    return UiConfig(
        enabled=True, bind="127.0.0.1", port=0, allow=("192.0.2.30",), dir=str(build),
        runs_dir=str(tmp_path / "runs"), tempo_url="https://tempo.example", rate_per_s=1000, **kw,
    )  # fmt: skip


def build_app(tmp_path):
    b = tmp_path / "build"
    (b / "assets").mkdir(parents=True)
    (b / "index.html").write_text("<!doctype html><base href=/><app-root></app-root>")
    (b / "main-ABCD1234.js").write_text("console.log(1)")
    (b / "styles-WXYZ9876.css").write_text("a{}")
    (b / "polyfills.js").write_text("//")
    (b / "favicon.ico").write_bytes(b"\0\0")
    (b / "logo.svg").write_text("<svg/>")
    (b / "font-AAAA1111.woff2").write_bytes(b"wOF2")
    (b / "pic.png").write_bytes(b"\x89PNG")
    (b / "main-ABCD1234.js.map").write_text("{}")
    (b / "data.json").write_text("{}")
    (b / "assets" / "x.txt").write_text("x")
    return b


async def http(port, method="GET", path="/", headers=()):
    head = [f"{method} {path} HTTP/1.1", "Host: x", *headers]
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(("\r\n".join(head) + "\r\n\r\n").encode())
    await w.drain()
    resp = await r.read()
    w.close()
    head_b, _, payload = resp.partition(b"\r\n\r\n")
    lines = head_b.decode().split("\r\n")
    hdrs = dict(ln.split(": ", 1) for ln in lines[1:])
    return int(lines[0].split()[1]), hdrs, payload


def run_ui(tmp_path, scenario, *, config=None, daemon=None, peer="192.0.2.30"):
    ui = UiServer(daemon or FakeDaemon(), config or cfg(tmp_path), allow_loopback=True, peer_addr=peer,
                  backoff=(0.05, 0.2))  # fmt: skip

    async def go():
        task = asyncio.create_task(ui.supervise())
        for _ in range(200):
            if ui.state == "up":
                break
            await asyncio.sleep(0.01)
        assert ui.state == "up", ui.status()
        try:
            await scenario(ui.port)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    return ui


def jget(payload):
    return json.loads(payload)


# ---- config -------------------------------------------------------------------------------------------


def test_disabled_by_default_and_nothing_listens(tmp_path):
    c = parse_config("")
    assert c.ui.enabled is False
    ui = UiServer(FakeDaemon(), c.ui)
    assert ui.status() == {"enabled": False, "state": "disabled", "error": None, "bind": None,
                           "port": 7421, "built": False}  # fmt: skip


@pytest.mark.parametrize(
    "text, word",
    [
        ("[ui]\nenabled = true\nallow = ['192.0.2.30']", "ui.bind"),
        ("[ui]\nenabled = true\nbind = '192.0.2.10'", "ui.allow"),
        ("[ui]\nenabled = true\nbind = '0.0.0.0'\nallow = ['192.0.2.30']", "ui.bind"),
        ("[ui]\nenabled = true\nbind = '192.0.2.10'\nallow = ['0.0.0.0/0']", "every address"),
        ("[ui]\nbind = '192.0.2.10'\nallow = ['192.0.2.30']\ndir = 'relative'", "ui.dir"),
        ("[ui]\ntempo_url = 'javascript:alert(1)'", "ui.tempo_url"),
        ("[ui]\nbogus = 1", "unknown key"),
        ("[ui]\nport = 0", "ui.port"),
    ],
)
def test_bad_ui_config_is_refused(text, word):
    with pytest.raises(ConfigError, match=word):
        parse_config(text)


def test_good_ui_config():
    c = parse_config(
        "[ui]\nenabled = true\nbind = '192.0.2.10'\nallow = ['192.0.2.30']\ntempo_url = 'https://t.example/x'"
    )
    assert (c.ui.port, c.ui.dir, c.ui.tempo_url) == (7421, "/opt/loadgov/ui", "https://t.example/x")


def test_check_config_warns_when_enabled_without_a_build(tmp_path, capsys):
    p = tmp_path / "c.toml"
    p.write_text(
        f"[ui]\nenabled = true\nbind = '192.0.2.10'\nallow = ['192.0.2.30']\ndir = '{tmp_path}/none'\n"
    )
    assert daemon_main(["--check-config", str(p)]) == 0
    assert "no build is at" in capsys.readouterr().err
    (tmp_path / "none").mkdir()
    (tmp_path / "none" / "index.html").write_text("x")
    assert daemon_main(["--check-config", str(p)]) == 0
    assert capsys.readouterr().err == ""


# ---- access -------------------------------------------------------------------------------------------


def test_non_allowlisted_client_is_refused_before_reading(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        code, _, body = await http(port, path="/")
        assert code == 403 and jget(body)["error"] == "forbidden"
        assert (await http(port, path="/api/queue"))[0] == 403

    run_ui(tmp_path, scenario, peer="198.51.100.7")


def test_allowlist_accepts_a_cidr(tmp_path):
    build_app(tmp_path)
    c = cfg(tmp_path)
    c = UiConfig(**{**c.__dict__, "allow": ("192.0.2.0/24",)})

    async def scenario(port):
        assert (await http(port, path="/"))[0] == 200

    run_ui(tmp_path, scenario, config=c, peer="192.0.2.99")


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head(tmp_path, method):
    build_app(tmp_path)

    async def scenario(port):
        code, hdrs, _ = await http(port, method, "/api/queue")
        assert (code, hdrs["Allow"]) == (405, "GET, HEAD")
        assert (await http(port, method, "/"))[0] == 405

    run_ui(tmp_path, scenario)


def test_head_has_headers_and_no_body(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        code, hdrs, body = await http(port, "HEAD", "/main-ABCD1234.js")
        assert (code, body) == (200, b"")
        assert hdrs["Content-Length"] == str(len("console.log(1)"))
        code, hdrs, body = await http(port, "HEAD", "/api/config")
        assert code == 200 and body == b"" and int(hdrs["Content-Length"]) > 10

    run_ui(tmp_path, scenario)


# ---- static files -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/../etc/passwd", "/%2e%2e/etc/passwd", "/%2E%2E/x", "/assets/../index.html", "/a/%2e%2e/%2e%2e/b",
     "/..%2fetc/passwd", "/assets%2f..%2f..%2fx", "//etc/passwd", "/./index.html", "/%00", "/a%5c..%5cb",
     "/.hidden", "/assets/.git/config", "/%ff", "/" + "a" * 2000],
)  # fmt: skip
def test_traversal_is_refused(tmp_path, path):
    build_app(tmp_path)
    (tmp_path / "secret.txt").write_text("secret")

    async def scenario(port):
        code, _, body = await http(port, path=path)
        assert code == 400 and b"secret" not in body

    run_ui(tmp_path, scenario)


def test_absolute_path_is_just_a_name_inside_the_dir(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        # "/etc/passwd" is the app route /etc/passwd inside the dir (no such file): the SPA, never the host file
        code, _, body = await http(port, path="/etc/passwd")
        assert code == 200 and b"app-root" in body and b"root:" not in body
        assert (await http(port, path="/etc/passwd.txt"))[0] == 404

    run_ui(tmp_path, scenario)


def test_symlinks_are_refused(tmp_path):
    b = build_app(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    os.symlink(secret, b / "leak.txt")
    os.symlink(tmp_path, b / "outdir")
    os.symlink(b / "logo.svg", b / "inside.svg")  # inside the dir: refused too

    async def scenario(port):
        for p in ("/leak.txt", "/outdir/secret.txt", "/inside.svg"):
            code, _, body = await http(port, path=p)
            assert code == 404 and b"secret" not in body and b"<svg" not in body
        code, _, body = await http(port, path="/outdir")  # an extensionless symlink: the app route
        assert code == 200 and b"app-root" in body

    run_ui(tmp_path, scenario)


def test_symlinked_index_is_not_served(tmp_path):
    b = build_app(tmp_path)
    (tmp_path / "evil.html").write_text("evil")
    (b / "index.html").unlink()
    os.symlink(tmp_path / "evil.html", b / "index.html")

    async def scenario(port):
        code, _, body = await http(port, path="/")
        assert code == 503 and b"evil" not in body

    run_ui(tmp_path, scenario)


def test_spa_fallback_versus_404(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        for p in ("/", "/runs", "/runs/20261126T100000Z-abcdef", "/queue", "/assets"):
            code, hdrs, body = await http(port, path=p)
            assert (code, hdrs["Content-Type"]) == (200, "text/html; charset=utf-8"), p
            assert b"app-root" in body and hdrs["Cache-Control"] == "no-cache"
        for p in ("/missing.js", "/assets/nope.png", "/x.map", "/runs/1.2"):
            assert (await http(port, path=p))[0] == 404, p
        code, hdrs, _ = await http(port, path="/api/unknown")  # never the SPA
        assert (code, hdrs["Content-Type"]) == (404, "application/json")
        assert (await http(port, path="/api"))[0] == 404

    run_ui(tmp_path, scenario)


def test_no_directory_listing(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        code, _, body = await http(port, path="/assets/")
        assert b"x.txt" not in body  # the empty last segment is refused
        code, _, body = await http(port, path="/assets")
        assert b"x.txt" not in body

    run_ui(tmp_path, scenario)


@pytest.mark.parametrize(
    "path, ctype",
    [("/main-ABCD1234.js", "text/javascript; charset=utf-8"), ("/styles-WXYZ9876.css", "text/css; charset=utf-8"),
     ("/index.html", "text/html; charset=utf-8"), ("/logo.svg", "image/svg+xml"),
     ("/font-AAAA1111.woff2", "font/woff2"), ("/data.json", "application/json"),
     ("/favicon.ico", "image/x-icon"), ("/pic.png", "image/png"),
     ("/main-ABCD1234.js.map", "application/json")],
)  # fmt: skip
def test_content_types(tmp_path, path, ctype):
    build_app(tmp_path)

    async def scenario(port):
        code, hdrs, _ = await http(port, path=path)
        assert (code, hdrs["Content-Type"]) == (200, ctype)

    run_ui(tmp_path, scenario)


def test_cache_headers_and_etag(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        immutable = "public, max-age=31536000, immutable"
        for p in ("/main-ABCD1234.js", "/styles-WXYZ9876.css", "/font-AAAA1111.woff2"):
            assert (await http(port, path=p))[1]["Cache-Control"] == immutable, p
        for p in ("/", "/index.html", "/polyfills.js", "/favicon.ico", "/data.json"):
            assert (await http(port, path=p))[1]["Cache-Control"] == "no-cache", p
        _, hdrs, _ = await http(port, path="/index.html")
        code, h2, body = await http(port, path="/index.html", headers=[f"If-None-Match: {hdrs['ETag']}"])
        assert (code, body, h2["Cache-Control"]) == (304, b"", "no-cache")

    run_ui(tmp_path, scenario)


def test_hash_pattern():
    assert HASHED.search("chunk-A1B2C3D4.js") and HASHED.search("main.0123456789abcdef.js")
    assert not HASHED.search("polyfills.js") and not HASHED.search("index.html")


def test_security_headers_on_every_kind_of_answer(tmp_path):
    build_app(tmp_path)

    async def scenario(port):
        for p, want in (("/", 200), ("/main-ABCD1234.js", 200), ("/missing.js", 404), ("/api/config", 200),
                        ("/api/nope", 404), ("/../x", 400)):  # fmt: skip
            code, hdrs, _ = await http(port, path=p)
            assert code == want, p
            csp = hdrs["Content-Security-Policy"]
            assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
            assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
            assert "script-src 'self';" in csp  # no inline script
            assert hdrs["X-Content-Type-Options"] == "nosniff"
            assert hdrs["Referrer-Policy"] == "no-referrer"
        assert (await http(port, "POST", "/"))[1]["X-Content-Type-Options"] == "nosniff"

    run_ui(tmp_path, scenario)


def test_oversize_file_is_413(tmp_path, monkeypatch):
    b = build_app(tmp_path)
    (b / "big.js").write_bytes(b"x" * 100)
    monkeypatch.setattr("loadgov.daemon.ui.MAX_FILE_BYTES", 50)

    async def scenario(port):
        assert (await http(port, path="/big.js"))[0] == 413
        assert (await http(port, path="/logo.svg"))[0] == 200

    run_ui(tmp_path, scenario)


def test_missing_build_is_503_and_status_says_so(tmp_path):
    async def scenario(port):
        for p in ("/", "/runs", "/main.js"):
            code, hdrs, body = await http(port, path=p)
            assert code == 503 and b"not built" in body and hdrs["Content-Type"].startswith("text/html")
            assert "Content-Security-Policy" in hdrs
        assert (await http(port, path="/api/config"))[0] == 200  # the JSON endpoints keep working

    ui = run_ui(tmp_path, scenario)
    assert ui.status()["built"] is False
    build_app(tmp_path)
    assert ui.status()["built"] is True


# ---- JSON endpoints -----------------------------------------------------------------------------------


def test_config_endpoint(tmp_path):
    async def scenario(port):
        code, hdrs, body = await http(port, path="/api/config")
        c = jget(body)
        assert code == 200 and hdrs["Cache-Control"] == "no-store"
        assert (c["host_id"], c["mode"], c["tempo_url"], c["api_base"]) == (
            "build-a",
            "govern",
            "https://tempo.example",
            "/api",
        )
        assert c["limits"]["log_max_bytes"] == 256 * 1024 and "token" not in body.decode().lower()

    run_ui(tmp_path, scenario)


def test_tempo_url_is_null_when_unset(tmp_path):
    c = UiConfig(**{**cfg(tmp_path).__dict__, "tempo_url": None})

    async def scenario(port):
        assert jget((await http(port, path="/api/config"))[2])["tempo_url"] is None

    run_ui(tmp_path, scenario, config=c)


def test_queue_endpoint(tmp_path):
    async def scenario(port):
        code, _, body = await http(port, path="/api/queue")
        q = jget(body)
        assert code == 200 and q["slots"]["heavy"]["holder"] == "web-app gate"
        assert q["image"]["source"] == "config" and "env_allow" not in q["image"]
        assert q["light"]["running"] == 1 and "rest" not in q  # only what the pages need

    run_ui(tmp_path, scenario)


def test_queue_while_the_daemon_has_no_sample_is_503(tmp_path):
    d = FakeDaemon({"ok": False, "error": "starting", "message": "no sample yet"})

    async def scenario(port):
        code, _, body = await http(port, path="/api/queue")
        assert (code, jget(body)["error"]) == (503, "starting")

    run_ui(tmp_path, scenario, daemon=d)


def make_run(tmp_path, jobs=None, steps=2, now=1_762_800_000.0):
    store = RunStore(str(tmp_path / "runs"))
    rid = store.create(
        {
            "workflow": {"name": "CI", "file": ".github/workflows/ci.yml", "stem": "ci"},
            "event": "push", "project": "web-app", "host_id": "build-a", "created_at": now_iso(now),
        },
        now=now,
    )  # fmt: skip
    spec = jobs or [("build", []), ("test", ["build"]), ("lint", []), ("ship", ["test", "lint"])]
    for order, (jid, needs) in enumerate(spec, 1):
        store.write_job(rid, {
            "run_id": rid, "order": order, "key": jid, "id": jid, "name": jid, "needs": needs,
            "lane": "light", "status": "success", "reason": None, "exit_code": 0, "duration_s": 1.5,
            "continue_on_error": False,
            "steps": [{"index": i, "id": None, "name": f"s{i}", "status": "success", "exit_code": 0,
                       "duration_s": 0.5, "log": f"logs/{jid}-{i}.log", "log_truncated": False}
                      for i in range(1, steps + 1)],
        })  # fmt: skip
    return store, rid


def test_runs_list_newest_first_with_limit(tmp_path):
    store, a = make_run(tmp_path, now=1_762_800_000.0)
    _, b = make_run(tmp_path, now=1_762_900_000.0)

    async def scenario(port):
        code, _, body = await http(port, path="/api/runs")
        r = jget(body)
        assert code == 200 and [x["id"] for x in r["runs"]] == [b, a] and r["damaged"] == 0
        assert r["runs"][0]["job_count"] == 4 and "jobs" not in r["runs"][0]
        assert r["runs"][0]["status"] == "success"
        assert [x["id"] for x in jget((await http(port, path="/api/runs?limit=1"))[2])["runs"]] == [b]
        for bad in ("limit=0", "limit=201", "limit=x", "limit=-1", "limit=1e3"):
            assert (await http(port, path=f"/api/runs?{bad}"))[0] == 400, bad

    run_ui(tmp_path, scenario)


def test_runs_list_with_no_runs_dir(tmp_path):
    async def scenario(port):
        assert jget((await http(port, path="/api/runs"))[2]) == {"ok": True, "runs": [], "damaged": 0}

    run_ui(tmp_path, scenario)


def test_run_has_jobs_grouped_by_needs(tmp_path):
    _, rid = make_run(tmp_path)

    async def scenario(port):
        code, _, body = await http(port, path=f"/api/runs/{rid}")
        run = jget(body)["run"]
        assert code == 200 and run["levels"] == [["build", "lint"], ["test"], ["ship"]]
        assert {j["key"]: j["level"] for j in run["jobs"]} == {"build": 1, "lint": 1, "test": 2, "ship": 3}
        assert len(run["jobs"][0]["steps"]) == 2

    run_ui(tmp_path, scenario)


def test_levels_of_handles_unknown_needs_and_cycles():
    jobs = [{"id": "a", "needs": ["b"]}, {"id": "b", "needs": ["a"]}, {"id": "c", "needs": ["ghost"]}]
    lv = levels_of(jobs)
    assert lv["c"] == 1 and set(lv) == {"a", "b", "c"}  # terminates


def test_run_ids_are_exact_and_checked(tmp_path):
    _, rid = make_run(tmp_path)

    async def scenario(port):
        assert (await http(port, path=f"/api/runs/{rid[:12]}"))[0] == 400
        assert (await http(port, path="/api/runs/latest"))[0] == 400
        assert (await http(port, path="/api/runs/..%2f..%2fetc"))[0] == 400
        assert (await http(port, path="/api/runs/20200101T000000Z-abcdef"))[0] == 404
        assert (await http(port, path=f"/api/runs/{rid}/jobs"))[0] == 404

    run_ui(tmp_path, scenario)


def write_log(tmp_path, rid, key, n, data: bytes):
    (tmp_path / "runs" / rid / "logs" / f"{key}-{n}.log").write_bytes(data)


def test_log_pages_with_offset_and_cap(tmp_path):
    _, rid = make_run(tmp_path)
    write_log(tmp_path, rid, "build", 1, b"0123456789" * 10)
    base = f"/api/runs/{rid}/jobs/build/steps/1/log"

    async def scenario(port):
        r = jget((await http(port, path=f"{base}?limit=30"))[2])
        assert (r["text"], r["offset"], r["next_offset"], r["size"], r["eof"]) == (
            "0123456789" * 3,
            0,
            30,
            100,
            False,
        )
        r = jget((await http(port, path=f"{base}?offset=30&limit=1000"))[2])
        assert (len(r["text"]), r["next_offset"], r["eof"], r["status"]) == (70, 100, True, "success")
        r = jget((await http(port, path=f"{base}?offset=5000"))[2])  # past the end reads as the end
        assert (r["text"], r["next_offset"], r["eof"]) == ("", 100, True)
        for bad in ("limit=0", "limit=262145", "offset=-1", "offset=x"):
            assert (await http(port, path=f"{base}?{bad}"))[0] == 400, bad
        assert len(jget((await http(port, path=f"{base}?limit=262144"))[2])["text"]) == 100

    run_ui(tmp_path, scenario)


def test_log_does_not_split_a_multibyte_character(tmp_path):
    _, rid = make_run(tmp_path)
    data = "aé€b".encode()  # a(1) é(2) €(3) b(1) = 7 bytes
    write_log(tmp_path, rid, "build", 1, data)
    base = f"/api/runs/{rid}/jobs/build/steps/1/log"

    async def scenario(port):
        r = jget((await http(port, path=f"{base}?limit=5"))[2])  # cuts inside the euro sign
        assert (r["text"], r["next_offset"]) == ("aé", 3)
        r = jget((await http(port, path=f"{base}?offset=3&limit=5"))[2])
        assert (r["text"], r["next_offset"], r["eof"]) == ("€b", 7, True)

    run_ui(tmp_path, scenario)


def test_decode_clean_final_replaces_bad_bytes():
    assert decode_clean(b"a\xff", final=True) == ("a�", 2)
    assert decode_clean(b"a\xe2\x82", final=False) == ("a", 1)


def test_log_of_a_running_step_is_not_eof_and_missing_log_is_empty(tmp_path):
    store, rid = make_run(tmp_path)
    job = json.load(open(store.job_path(rid, "build")))
    job["steps"][0]["status"] = "running"
    store.write_job(rid, job)
    write_log(tmp_path, rid, "build", 1, b"hello")

    async def scenario(port):
        r = jget((await http(port, path=f"/api/runs/{rid}/jobs/build/steps/1/log"))[2])
        assert (r["text"], r["next_offset"], r["eof"], r["status"]) == ("hello", 5, False, "running")
        r = jget((await http(port, path=f"/api/runs/{rid}/jobs/build/steps/2/log"))[2])  # no file yet
        assert (r["text"], r["size"]) == ("", 0)

    run_ui(tmp_path, scenario)


def test_log_addresses_are_checked(tmp_path):
    _, rid = make_run(tmp_path)

    async def scenario(port):
        base = f"/api/runs/{rid}/jobs"
        assert (await http(port, path=f"{base}/ghost/steps/1/log"))[0] == 404
        assert (await http(port, path=f"{base}/build/steps/9/log"))[0] == 404
        assert (await http(port, path=f"{base}/build/steps/0/log"))[0] == 404
        assert (await http(port, path=f"{base}/build/steps/x/log"))[0] == 404
        assert (await http(port, path=f"{base}/..%2f..%2fx/steps/1/log"))[0] == 400
        assert (await http(port, path=f"{base}/b%20d/steps/1/log"))[0] == 404

    run_ui(tmp_path, scenario)


def test_log_ignores_a_path_stored_in_the_record_and_refuses_symlinks(tmp_path):
    store, rid = make_run(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    job = json.load(open(store.job_path(rid, "build")))
    job["steps"][0]["log"] = "../../../secret.txt"
    store.write_job(rid, job)
    os.symlink(secret, tmp_path / "runs" / rid / "logs" / "build-2.log")

    async def scenario(port):
        for n in (1, 2):
            code, _, body = await http(port, path=f"/api/runs/{rid}/jobs/build/steps/{n}/log")
            assert b"secret" not in body
            assert code in (200, 422) if n == 1 else code == 422

    run_ui(tmp_path, scenario)


@pytest.mark.parametrize(
    "damage",
    [
        "run_garbage",
        "job_garbage",
        "job_not_object",
        "job_missing_status",
        "run_is_list",
        "job_missing_id",
        "run_missing_workflow",
        "step_wrong_type",
        "run_id_not_its_directory",
    ],  # fmt: skip
)
def test_damaged_records_give_a_clean_error(tmp_path, damage):
    store, rid = make_run(tmp_path)
    good_store, good = make_run(tmp_path, now=1_762_700_000.0)
    d = tmp_path / "runs" / rid
    if damage == "run_garbage":
        (d / "run.json").write_text("{not json")
    elif damage == "run_is_list":
        (d / "run.json").write_text("[1,2]")
    elif damage == "job_garbage":
        (d / "jobs" / "build.json").write_text("{nope")
    elif damage == "job_not_object":
        (d / "jobs" / "build.json").write_text("[1]")
    elif damage == "job_missing_status":
        (d / "jobs" / "build.json").write_text('{"key": "build", "id": "build", "steps": []}')
    elif damage == "job_missing_id":
        (d / "jobs" / "build.json").write_text('{"key": "build", "status": "success", "steps": []}')
    elif damage == "run_missing_workflow":
        r = json.load(open(d / "run.json"))
        del r["workflow"]
        (d / "run.json").write_text(json.dumps(r))
    elif damage == "run_id_not_its_directory":
        r = json.load(open(d / "run.json"))
        r["id"] = good
        (d / "run.json").write_text(json.dumps(r))
    elif damage == "step_wrong_type":
        j = json.load(open(d / "jobs" / "build.json"))
        j["steps"][0]["index"] = "one"
        (d / "jobs" / "build.json").write_text(json.dumps(j))

    async def scenario(port):
        code, _, body = await http(port, path=f"/api/runs/{rid}")
        assert code == 422 and jget(body)["error"] == "damaged_record" and b"Traceback" not in body
        code, _, body = await http(port, path=f"/api/runs/{rid}/jobs/build/steps/1/log")
        assert code in (404, 422) and b"Traceback" not in body
        r = jget((await http(port, path="/api/runs"))[2])  # the list skips what it cannot read
        assert good in [x["id"] for x in r["runs"]]
        assert r["damaged"] == 1
        assert (await http(port, path=f"/api/runs/{good}"))[0] == 200

    run_ui(tmp_path, scenario)


@pytest.mark.parametrize(
    "exc", [RunRecordError("x: bad"), ValueError("v"), OSError("o"), RecursionError("r")]
)
def test_record_errors_map_to_422(tmp_path, monkeypatch, exc):
    make_run(tmp_path)

    def boom(self, *a, **k):
        raise exc

    monkeypatch.setattr(runs_api, "list_runs", boom)

    async def scenario(port):
        code, _, body = await http(port, path="/api/runs")
        assert code == 422 and jget(body) == {
            "ok": False, "error": "damaged_record", "message": "the run record could not be read",
        }  # fmt: skip

    run_ui(tmp_path, scenario)


def test_an_unexpected_error_is_a_500_without_a_traceback(tmp_path, monkeypatch):
    make_run(tmp_path)

    def boom(self, *a, **k):
        raise TypeError("secret internals")

    monkeypatch.setattr(runs_api, "list_runs", boom)

    async def scenario(port):
        code, _, body = await http(port, path="/api/runs")
        assert code == 500 and jget(body)["error"] == "internal"
        assert b"Traceback" not in body and b"secret internals" not in body

    run_ui(tmp_path, scenario)


def test_listener_failure_is_reported_and_supervised(tmp_path):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    c = UiConfig(**{**cfg(tmp_path).__dict__, "port": s.getsockname()[1]})
    ui = UiServer(FakeDaemon(), c, allow_loopback=True, backoff=(0.02, 0.05))

    async def go():
        task = asyncio.create_task(ui.supervise())
        await asyncio.sleep(0.2)
        assert ui.state == "down" and ui.error
        s.close()
        for _ in range(100):
            if ui.state == "up":
                break
            await asyncio.sleep(0.02)
        assert ui.state == "up"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())


def test_decode_clean_leaves_a_partial_character_alone():
    euro = "€".encode()
    assert decode_clean(euro[:1], final=False) == ("", 0)  # nothing but a partial character: no U+FFFD
    assert decode_clean(b"ab" + euro[:2], final=False) == ("ab", 2)
    assert decode_clean(euro, final=False) == ("€", 3)
    assert decode_clean(b"a\xff", final=False) == ("a\ufffd", 2)  # real bad bytes are not held back
    assert decode_clean(euro[:2], final=True) == ("\ufffd", 2)


def test_log_page_of_a_partial_character_keeps_the_offset(tmp_path):
    store, rid = make_run(tmp_path)
    job = json.load(open(store.job_path(rid, "build")))
    job["steps"][0]["status"] = "running"
    store.write_job(rid, job)
    write_log(tmp_path, rid, "build", 1, b"ab" + "€".encode()[:2])  # the writer is mid-character

    async def scenario(port):
        base = f"/api/runs/{rid}/jobs/build/steps/1/log"
        r = jget((await http(port, path=base))[2])
        assert (r["text"], r["next_offset"]) == ("ab", 2)
        r = jget((await http(port, path=f"{base}?offset=2"))[2])
        assert (r["text"], r["next_offset"], r["eof"]) == ("", 2, False)

    run_ui(tmp_path, scenario)


def test_underscore_and_long_matrix_job_keys_have_logs(tmp_path):
    long_key = "build-" + "-".join(f"os_{i}_x86.64" for i in range(12))  # a long matrix suffix
    spec = [("_lint", []), (long_key, ["_lint"]), ("a.b-1", [])]
    _, rid = make_run(tmp_path, jobs=spec)
    for key, _ in spec:
        write_log(tmp_path, rid, key, 1, f"log of {key}".encode())

    async def scenario(port):
        for key, _ in spec:
            code, _, body = await http(port, path=f"/api/runs/{rid}/jobs/{key}/steps/1/log")
            assert code == 200 and jget(body)["text"] == f"log of {key}", key

    run_ui(tmp_path, scenario)


@pytest.mark.parametrize(
    "key", [".hidden", "a..b", "..", ".", "a%2fb", "a%5cb", "a%00b", "x" * 201, "%2e%2e"]
)
def test_unsafe_job_keys_are_refused(tmp_path, key):
    _, rid = make_run(tmp_path)

    async def scenario(port):
        code, _, _ = await http(port, path=f"/api/runs/{rid}/jobs/{key}/steps/1/log")
        assert code in (400, 404)

    run_ui(tmp_path, scenario)


def test_unsafe_key_in_a_record_is_never_opened(tmp_path):
    store, rid = make_run(tmp_path)
    (tmp_path / "secret.log").write_text("secret")
    job = json.load(open(store.job_path(rid, "build")))
    job["key"] = "../../../secret"
    (tmp_path / "runs" / rid / "jobs" / "evil.json").write_text(json.dumps(job))

    async def scenario(port):
        code, _, body = await http(port, path=f"/api/runs/{rid}/jobs/..%2f..%2f..%2fsecret/steps/1/log")
        assert code == 400 and b"secret" not in body

    run_ui(tmp_path, scenario)


def test_huge_numbers_are_400_and_a_missing_log_says_truncated(tmp_path):
    _, rid = make_run(tmp_path)

    async def scenario(port):
        base = f"/api/runs/{rid}/jobs/build/steps/1/log"
        assert (await http(port, path=f"{base}?limit={'9' * 5000}"))[0] == 400
        assert (await http(port, path=f"{base}?offset={'9' * 5000}"))[0] == 400
        assert (await http(port, path=f"{base}?limit=3"))[0] == 400  # smaller than one character
        r = jget((await http(port, path=base))[2])  # no file written
        assert r["truncated"] is False and r["text"] == ""

    run_ui(tmp_path, scenario)
