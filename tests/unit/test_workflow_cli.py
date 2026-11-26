"""`loadctl workflow list|validate|run` and `loadctl runs`: rendering, --json, exit codes and the wiring to
`loadctl run`/`loadctl image submit` through a fake loadctl executable."""

import json
import os
import shutil
import socket
import stat
import textwrap
import time

import pytest

from loadgov.cli import loadctl
from loadgov.config import derive_host_id
from loadgov.workflow import cli, records

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples", "workflows")

SIMPLE = """\
name: Simple
on: [push, workflow_dispatch]
jobs:
  one:
    runs-on: [self-hosted, loadgov-light]
    steps:
      - id: s
        run: echo "out=hello" >> "$GITHUB_OUTPUT"
      - run: echo "got ${{ steps.s.outputs.out }} on $GITHUB_EVENT_NAME"
  two:
    needs: one
    runs-on: loadgov-gate
    steps:
      - run: exit 5
"""

FAKE_LOADCTL = """\
#!/bin/bash
echo "$@" >> "$FAKE_LOG"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / ".github/workflows").mkdir(parents=True)
    (p / ".forgejo/workflows").mkdir(parents=True)
    (p / ".github/workflows/simple.yml").write_text(SIMPLE)
    (p / ".github/workflows/bad.yml").write_text(
        "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: x/y@v1\n"
    )
    (p / ".forgejo/workflows/gate.yaml").write_text(
        open(os.path.join(EXAMPLES, "gate-with-needs.yml")).read()
    )
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "fake-loadctl"
    fake.write_text(FAKE_LOADCTL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("LOADGOV_LOADCTL", str(fake))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("LOADGOV_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("LOADGOV_HOST_ID", "build-host")
    monkeypatch.delenv("LOADGOV_KEY", raising=False)
    monkeypatch.delenv("LOADGOV_PROJECT", raising=False)
    return tmp_path


def fake_calls(env):
    return [line.split() for line in (env / "fake.log").read_text().splitlines()]


# ---- list and validate -------------------------------------------------------------------------------


def test_workflow_list_text_and_invalid_entries(project, capsys):
    assert loadctl.main(["workflow", "list", "--path", str(project)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(".github/workflows/bad.yml  INVALID  ") and "uses" in out[0]
    assert (
        out[1]
        == ".github/workflows/simple.yml  Simple  on: push,workflow_dispatch  jobs: one[light], two[gate]"
    )
    assert out[2].startswith(".forgejo/workflows/gate.yaml  Pipeline  on: push,workflow_dispatch")
    assert "test[gate]" in out[2] and "build[compile]" in out[2]


def test_workflow_list_json_and_empty(project, tmp_path, capsys):
    assert loadctl.main(["workflow", "list", "--path", str(project), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["valid"] for r in rows] == [False, True, True]
    assert rows[1]["jobs"][1]["needs"] == ["one"] and rows[1]["jobs"][0]["instances"][0]["lane"] == "light"
    assert loadctl.main(["workflow", "list", "--path", str(tmp_path / "empty")]) == 0
    assert "no workflows under" in capsys.readouterr().out


def test_validate_ok_prints_jobs_lanes_and_notes(capsys):
    assert loadctl.main(["workflow", "validate", os.path.join(EXAMPLES, "gate-with-needs.yml")]) == 0
    out = capsys.readouterr().out
    assert "OK " in out and "Pipeline (on: push, workflow_dispatch; 4 job(s), 5 instance(s))" in out
    assert "job test: lane gate, 4 step(s), needs check, 2 matrix instances" in out
    assert "job build: lane compile, 1 step(s), needs check, test" in out
    assert "note: host-only:" in out


def test_validate_refused_names_file_line_and_exits_1(capsys):
    assert loadctl.main(["workflow", "validate", os.path.join(EXAMPLES, "refused-uses.yml")]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "refused: " in cap.err and "refused-uses.yml:13:9: jobs.build.steps[0].uses:" in cap.err


def test_validate_json_and_missing_file(capsys):
    assert loadctl.main(["workflow", "validate", os.path.join(EXAMPLES, "light-check.yml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["id"] == "lint"
    assert loadctl.main(["workflow", "validate", "/nonexistent/x.yml"]) == 1
    assert "cannot read" in capsys.readouterr().err


# ---- run ---------------------------------------------------------------------------------------------


def test_run_goes_through_loadctl_run_per_job_and_records(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    rc = loadctl.main(["workflow", "run", f, "--event", "push", "--key", "PROJ-42", "--project", "web-app"])
    out = capsys.readouterr().out
    assert rc == 1  # job two exits 5
    calls = fake_calls(env)
    assert len(calls) == 2
    assert (
        calls[0][:3] == ["--socket", loadctl.DEFAULT_SOCKET, "run"]
        and "light" in calls[0]
        and "simple-one" in calls[0]
    )
    assert (
        calls[0][calls[0].index("--key") + 1] == "PROJ-42"
        and calls[0][calls[0].index("--project") + 1] == "web-app"
    )
    assert "gate" in calls[1] and "simple-two" in calls[1]
    assert "job one: success" in out and "job two: failure" in out and ": failure" in out.splitlines()[-1]
    store = records.RunStore(str(env / "runs"))
    run = store.load_run("latest")
    assert (run["host_id"], run["workspace"]) == ("build-host", str(project))
    assert run["jobs"][0]["steps"][1]["status"] == "success"
    assert run["jobs"][1]["exit_code"] == 5


def test_run_project_defaults_to_the_workspace_name(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    loadctl.main(["workflow", "run", f, "--job", "one"])
    capsys.readouterr()
    c = fake_calls(env)[0]
    assert c[c.index("--project") + 1] == "proj"


def test_run_job_option_event_and_exit_codes(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    assert loadctl.main(["workflow", "run", f, "--job", "one", "--event", "push"]) == 0
    assert loadctl.main(["workflow", "run", f, "--job", "zz"]) == 2
    assert "no job 'zz'" in capsys.readouterr().err
    assert loadctl.main(["workflow", "run", f, "--event", "pull_request", "--job", "one"]) == 2
    assert "does not trigger on 'pull_request'" in capsys.readouterr().err
    assert loadctl.main(["workflow", "run", f, "--max-parallel", "0"]) == 2
    bad = str(project / ".github/workflows/bad.yml")
    assert loadctl.main(["workflow", "run", bad]) == 2
    assert "refused: " in capsys.readouterr().err


def test_run_dry_run_prints_commands_and_records_nothing(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    assert loadctl.main(["workflow", "run", f, "--dry-run", "--project", "web-app"]) == 0
    out = capsys.readouterr().out
    assert "--kind light --project web-app --label simple-one --" in out
    assert "--kind gate --project web-app --label simple-two --" in out
    assert not (env / "runs").exists() and not (env / "fake.log").exists()


def test_run_json_prints_the_record_and_progress_goes_to_stderr(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    loadctl.main(["workflow", "run", f, "--job", "one", "--json"])
    cap = capsys.readouterr()
    rec = json.loads(cap.out)
    assert rec["schema"] == 1 and rec["status"] == "success" and rec["jobs"][0]["key"] == "one"
    assert "job one: success" in cap.err


def test_image_job_needs_a_channel_option_when_the_label_has_none(project, env, capsys):
    f = project / ".github/workflows/img.yml"
    f.write_text("on: push\njobs:\n  i:\n    runs-on: loadgov-image\n    steps: [{run: echo x}]\n")
    assert loadctl.main(["workflow", "run", str(f)]) == 2
    assert "channel" in capsys.readouterr().err
    assert (
        loadctl.main(["workflow", "run", str(f), "--channel", "beta"]) == 0
    )  # the fake runs the script of `image submit` at once
    calls = fake_calls(env)
    assert calls[0][2:4] == ["image", "submit"] and "--channel" in calls[0]


def test_workspace_of_and_host_id(tmp_path, monkeypatch):
    assert cli.workspace_of(str(tmp_path / "p/.github/workflows/a.yml"), None) == str(tmp_path / "p")
    assert cli.workspace_of(str(tmp_path / "p/.forgejo/workflows/a.yml"), None) == str(tmp_path / "p")
    assert cli.workspace_of("elsewhere/a.yml", None) == os.getcwd()
    assert cli.workspace_of("elsewhere/a.yml", "rel") == os.path.abspath("rel")
    monkeypatch.setenv("LOADGOV_HOST_ID", "fixed")
    assert cli.host_id("/nonexistent.sock") == "fixed"
    monkeypatch.delenv("LOADGOV_HOST_ID")
    assert cli.host_id(str(tmp_path / "no.sock")) == derive_host_id(socket.gethostname())


# ---- runs --------------------------------------------------------------------------------------------


@pytest.fixture
def ran(project, env, capsys):
    f = str(project / ".github/workflows/simple.yml")
    loadctl.main(["workflow", "run", f, "--event", "push", "--key", "PROJ-42"])
    capsys.readouterr()
    return records.RunStore(str(env / "runs")).load_run("latest")


def test_runs_list_text_and_json(ran, capsys):
    assert loadctl.main(["runs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["RUN", "STATUS", "WORKFLOW", "EVENT", "PROJECT", "JOBS", "CREATED"]
    assert out[1].split()[:6] == [ran["id"], "failure", "simple", "push", "proj", "2"]
    assert loadctl.main(["runs", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["id"] == ran["id"] and rows[0]["job_count"] == 2 and "jobs" not in rows[0]


def test_runs_empty(env, capsys):
    assert loadctl.main(["runs"]) == 0
    assert capsys.readouterr().out.strip() == "no runs"


def test_runs_show_text_job_step_and_log(ran, capsys):
    assert loadctl.main(["runs", "show", "latest"]) == 0
    out = capsys.readouterr().out
    assert f"run {ran['id']}: failure" in out and "key PROJ-42" in out and "host build-host" in out
    assert "job one [light]: success" in out and "job two [gate]: failure" in out and "exit 5" in out
    assert "  1. success" in out and 'echo "got hello on $GITHUB_EVENT_NAME"' in out
    assert loadctl.main(["runs", "show", ran["id"][:12], "--job", "one", "--step", "2", "--log"]) == 0
    out = capsys.readouterr().out
    assert "job two" not in out and "  1. " not in out
    assert "== one step 2:" in out and "got hello on push" in out
    assert loadctl.main(["runs", "show", "latest", "--job", "one", "--step", "s", "--log"]) == 0
    assert "== one step 1:" in capsys.readouterr().out


def test_runs_show_json_is_filtered_but_same_shape(ran, capsys):
    assert loadctl.main(["runs", "show", "latest", "--json", "--job", "two"]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert [j["key"] for j in rec["jobs"]] == ["two"] and rec["status"] == "failure" and rec["schema"] == 1
    assert (
        loadctl.main(["runs", "--runs-dir", str(os.environ["LOADGOV_RUNS_DIR"]), "show", ran["id"], "--json"])
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["jobs"]) == 2


def test_runs_show_errors(ran, capsys):
    assert loadctl.main(["runs", "show", "nope"]) == 2
    assert "no run 'nope'" in capsys.readouterr().err
    assert loadctl.main(["runs", "show", "latest", "--job", "ghost"]) == 2
    assert "no such job or step" in capsys.readouterr().err
    assert loadctl.main(["runs", "show", "latest", "--step", "99"]) == 2
    assert "no such job or step" in capsys.readouterr().err


HOSTED = os.path.join(os.path.dirname(__file__), "parity", "hosted-jobs.json")


def _edit(doc, path, value=None, drop=False):
    for p in path[:-1]:
        doc = doc[p]
    if drop:
        doc.pop(path[-1])
    else:
        doc[path[-1]] = value


def _mut(path, value=None, drop=False):
    def go(doc):
        _edit(doc, path, value, drop)
        return doc

    return go


# (record file, how to damage it, text the one-line error must contain)
DAMAGE = [
    ("run.json", lambda d: [d], "must be an object"),
    ("run.json", _mut(("event",), drop=True), "event is missing"),
    ("run.json", _mut(("id",), "20250101T000000Z-abcdef"), "does not match its directory"),
    ("run.json", _mut(("project",), 5), "project must be a string"),
    ("run.json", _mut(("workflow",), "x"), "workflow must be an object"),
    ("run.json", _mut(("workflow", "stem"), drop=True), "workflow.stem is missing"),
    ("run.json", lambda d: b"\xff\xfe{", "not valid UTF-8"),
    ("run.json", lambda d: b'{"id": "a",', "not valid JSON"),
    ("run.json", lambda d: b"[" * 100000, "nested too deeply"),
    ("jobs/one.json", _mut(("status",), drop=True), "status is missing"),
    ("jobs/one.json", _mut(("lane",), 7), "lane must be a string"),
    ("jobs/one.json", _mut(("duration_s",), "0"), "duration_s must be a number"),
    ("jobs/one.json", _mut(("duration_s",), float("nan")), "duration_s must be a number"),
    ("jobs/one.json", _mut(("duration_s",), 10**400), "duration_s must be a number"),
    ("jobs/one.json", _mut(("order",), None), "order must not be null"),
    ("jobs/one.json", _mut(("order",), "1"), "order must be a number"),
    ("jobs/one.json", _mut(("steps",), {}), "steps must be a list"),
    ("jobs/one.json", _mut(("steps",), ["x"]), "steps[0] must be an object"),
    ("jobs/one.json", _mut(("steps", 0, "index"), drop=True), "steps[0].index is missing"),
    ("jobs/one.json", _mut(("steps", 0, "exit_code"), True), "steps[0].exit_code must be an integer"),
    ("jobs/one.json", lambda d: [d], "must be an object"),
    ("jobs/one.json", lambda d: b'{"key": "one", "st', "not valid JSON"),
]


@pytest.fixture(params=DAMAGE, ids=[f"{r}-{m}" for r, _, m in DAMAGE])
def damaged(request, ran, env):
    rel, fn, message = request.param
    path = os.path.join(str(env / "runs"), ran["id"], rel)
    with open(path, encoding="utf-8") as f:
        new = fn(json.load(f))
    if isinstance(new, bytes):
        with open(path, "wb") as f:
            f.write(new)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(new, f)
    return path, message


def _one_line_error(capsys, argv, path, message):
    assert loadctl.main(argv) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Traceback" not in err
    assert path in err and message in err


def test_runs_show_damaged_record_is_a_one_line_error(damaged, capsys):
    path, message = damaged
    _one_line_error(capsys, ["runs", "show", "latest"], path, message)


def test_workflow_parity_damaged_record_in_the_runs_dir_is_a_one_line_error(damaged, env, capsys):
    path, message = damaged
    argv = ["workflow", "parity", "latest", "--runs-dir", str(env / "runs"), "--hosted", HOSTED]
    _one_line_error(capsys, argv, path, message)


def test_runs_list_skips_a_damaged_run_and_warns_on_stderr(damaged, ran, env, capsys):
    path, message = damaged
    meta = {k: ran[k] for k in ("workflow", "event", "project", "host_id", "created_at")}
    good = records.RunStore(str(env / "runs")).create(meta, now=time.time() + 5)
    assert loadctl.main(["runs"]) == 0
    cap = capsys.readouterr()
    assert good in cap.out and ran["id"] not in cap.out
    assert cap.err.count("\n") == 1 and path in cap.err and message in cap.err and "Traceback" not in cap.err


def test_runs_list_with_only_a_damaged_run_prints_no_runs(ran, env, capsys):
    with open(os.path.join(str(env / "runs"), ran["id"], "run.json"), "w", encoding="utf-8") as f:
        f.write("[]")
    assert loadctl.main(["runs"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "no runs" and "skipped a damaged run" in cap.err


def test_unreadable_record_is_a_one_line_error(ran, env, capsys, monkeypatch):
    path = os.path.join(str(env / "runs"), ran["id"], "run.json")
    real = open

    def deny(p, *a, **k):
        if p == path:
            raise PermissionError(13, "Permission denied", p)
        return real(p, *a, **k)

    monkeypatch.setattr("builtins.open", deny)
    _one_line_error(capsys, ["runs", "show", "latest"], path, "unreadable (Permission denied)")


def test_unreadable_jobs_dir_is_a_one_line_error(ran, env, capsys, monkeypatch):
    jd = os.path.join(str(env / "runs"), ran["id"], "jobs")
    real = os.listdir

    def deny(p="."):
        if p == jd:
            raise PermissionError(13, "Permission denied", p)
        return real(p)

    monkeypatch.setattr(os, "listdir", deny)
    _one_line_error(capsys, ["runs", "show", "latest"], jd, "unreadable (Permission denied)")


def test_workflow_run_with_a_damaged_record_in_flight_is_a_one_line_error(project, env, capsys, monkeypatch):
    def boom(*a, **k):
        raise records.RunRecordError("/x/run.json: event is missing")

    monkeypatch.setattr(cli.runner, "run_workflow", boom)
    f = str(project / ".github/workflows/simple.yml")
    assert loadctl.main(["workflow", "run", f, "--event", "push"]) == 2
    assert capsys.readouterr().err.strip() == "loadctl workflow run: /x/run.json: event is missing"


def test_retention_keeps_a_damaged_run_until_it_is_stale(ran, env):
    store = records.RunStore(str(env / "runs"), keep=1)
    with open(os.path.join(str(env / "runs"), ran["id"], "jobs", "one.json"), "w", encoding="utf-8") as f:
        f.write('{"key": "one", "st')
    meta = {k: ran[k] for k in ("workflow", "event", "project", "host_id", "created_at")}
    newer = [store.create(meta, now=time.time() + i) for i in (1, 2)]
    job = {"key": "j", "id": "j", "lane": "light", "status": "success", "steps": []}
    for r in newer:
        store.write_job(r, {**job, "exit_code": None, "duration_s": None})
    assert store.active(ran["id"]) and store.prune() == [newer[0]]  # the damaged run is kept, not counted
    assert ran["id"] in store.ids()
    old = store.create(meta, now=time.time() - 40 * 86400)
    with open(os.path.join(store.run_dir(old), "run.json"), "w", encoding="utf-8") as f:
        f.write("[]")
    assert not store.active(old) and old in store.prune()


def test_a_job_file_pruned_during_the_load_is_skipped(ran, env, monkeypatch):
    real = records.read_record

    def gone(p):
        if p.endswith("one.json"):
            os.unlink(p)
        return real(p)

    monkeypatch.setattr(records, "read_record", gone)
    assert [j["key"] for j in records.RunStore(str(env / "runs")).load_run("latest")["jobs"]] == ["two"]


def test_format_runs_alignment():
    rows = [
        {
            "id": "a",
            "status": "success",
            "workflow": {"stem": "ci"},
            "event": "push",
            "project": "p",
            "job_count": 1,
            "created_at": "t",
        },
        {
            "id": "bbbb",
            "status": "waiting for slot",
            "workflow": {"stem": "x"},
            "event": "push",
            "project": "p",
            "job_count": 12,
            "created_at": "t",
        },
    ]
    lines = cli.format_runs(rows).splitlines()
    assert lines[1].index("success") == lines[2].index("waiting for slot") == lines[0].index("STATUS")
    assert textwrap.dedent(cli.format_runs([])) == "no runs"


def test_deeply_nested_files_are_refused_cleanly_and_list_continues(project, capsys):
    deep = project / ".github/workflows/deep.yml"
    deep.write_text("on: push\njobs: " + "[" * 1000 + "]" * 1000 + "\n")
    expr_deep = project / ".github/workflows/deepexpr.yml"
    expr_deep.write_text(
        "on: push\njobs:\n  a:\n    runs-on: x64\n    steps:\n      - run: echo ${{ "
        + "!" * 100
        + "env.A }}\n"
    )
    assert loadctl.main(["workflow", "validate", str(deep)]) == 1
    err = capsys.readouterr().err
    assert "refused: " in err and "nested more than 64" in err and "Traceback" not in err
    assert loadctl.main(["workflow", "validate", str(expr_deep)]) == 1
    assert "nested more than 64" in capsys.readouterr().err
    assert loadctl.main(["workflow", "run", str(deep)]) == 2
    assert "nested more than 64" in capsys.readouterr().err
    assert loadctl.main(["workflow", "list", "--path", str(project)]) == 0
    out = capsys.readouterr().out
    assert "deep.yml  INVALID" in out and "deepexpr.yml  INVALID" in out
    assert "simple.yml  Simple" in out and "gate.yaml  Pipeline" in out


def test_huge_matrix_is_refused_fast_with_clean_exit_codes(project, capsys):
    import time

    vals = ", ".join(str(i) for i in range(10))
    body = "".join(f"        k{i}: [{vals}]\n" for i in range(8))
    f = project / ".github/workflows/huge.yml"
    f.write_text(
        "on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix:\n"
        + body
        + "    steps: [{run: echo x}]\n"
    )
    t0 = time.monotonic()
    assert loadctl.main(["workflow", "validate", str(f)]) == 1
    assert "100000000 combinations" in capsys.readouterr().err
    assert loadctl.main(["workflow", "run", str(f), "--dry-run"]) == 2
    assert "refused: " in capsys.readouterr().err
    assert loadctl.main(["workflow", "list", "--path", str(project)]) == 0
    assert "huge.yml  INVALID" in capsys.readouterr().out
    assert time.monotonic() - t0 < 3.0


def test_a_run_pruned_between_ids_and_resolve_is_skipped_not_damaged(ran, env, monkeypatch):
    store = records.RunStore(str(env / "runs"))
    meta = {k: ran[k] for k in ("workflow", "event", "project", "host_id", "created_at")}
    newer = store.create(meta, now=time.time() + 5)
    real = records.RunStore.resolve

    def vanish(self, ref):
        if ref == newer:
            shutil.rmtree(self.run_dir(newer))  # pruned by another process after ids() listed it
        return real(self, ref)

    monkeypatch.setattr(records.RunStore, "resolve", vanish)
    skipped = []
    rows = store.list_runs(on_skip=skipped.append)
    assert [r["id"] for r in rows] == [ran["id"]] and skipped == []  # vanished is not damaged


def test_a_lookup_error_from_a_run_that_still_exists_is_not_swallowed(ran, env, monkeypatch):
    store = records.RunStore(str(env / "runs"))

    def bug(self, ref):
        raise KeyError("a bug, not the prune race")

    monkeypatch.setattr(records.RunStore, "load_run", bug)
    with pytest.raises(KeyError):
        store.list_runs()


def test_a_run_whose_id_is_not_its_directory_is_kept_by_retention(ran, env):
    store = records.RunStore(str(env / "runs"))
    p = os.path.join(store.run_dir(ran["id"]), "run.json")
    doc = json.load(open(p))
    doc["id"] = "20250101T000000Z-abcdef"
    json.dump(doc, open(p, "w"))
    with pytest.raises(records.RunRecordError, match="id .* does not match its directory"):
        store.load_run(ran["id"])
    assert store.active(ran["id"])  # damaged: kept like any other damaged record
