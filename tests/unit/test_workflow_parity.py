"""`loadctl workflow parity`: a local run record against recorded hosted jobs (no network, no host)."""

import json
import os

import pytest

from loadgov.cli import loadctl
from loadgov.workflow import parity, records

FIX = os.path.join(os.path.dirname(__file__), "parity")
LOCAL = os.path.join(FIX, "local-run.json")
HOSTED = os.path.join(FIX, "hosted-jobs.json")
OUTPUTS = os.path.join(FIX, "outputs.json")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def docs():
    return load("local-run.json"), load("hosted-jobs.json"), load("outputs.json")


def run_cmp(local, hosted, outputs=None, tmp_path=None):
    """compare() through the file loaders, as the CLI does it."""
    d = tmp_path
    paths = []
    for name, doc in (("l.json", local), ("h.json", hosted)):
        (d / name).write_text(json.dumps(doc))
        paths.append(str(d / name))
    out = None
    if outputs is not None:
        (d / "o.json").write_text(json.dumps(outputs))
        out = parity.load_outputs(str(d / "o.json"))
    return parity.compare(parity.load_local(paths[0], None), parity.load_hosted(paths[1]), out)


def kinds(rep):
    return sorted(d["kind"] for d in rep["divergences"])


# ---- identical, host-only -----------------------------------------------------------------------------


def test_recorded_fixtures_are_identical_and_report_host_only_differences(docs, tmp_path):
    rep = run_cmp(*docs, tmp_path=tmp_path)
    assert rep["result"] == "identical" and rep["divergences"] == []
    assert rep["compared"] == {"jobs": 2, "steps": 5, "outputs": 1}
    host = {d["kind"] for d in rep["host_only"]}
    assert {"hosted_step", "runner_labels", "runner", "timing", "environment", "local_note"} <= host
    skipped = [d["where"] for d in rep["host_only"] if d["kind"] == "hosted_step"]
    assert "Check / Set up job" in skipped and "Report / Complete job" in skipped


def test_host_only_differences_alone_never_diverge(docs, tmp_path):
    local, hosted, outputs = docs
    hosted["jobs"][0]["labels"] = ["ubuntu-24.04", "x64"]
    hosted["jobs"][0]["runner_name"] = "somewhere-else"
    hosted["jobs"][0]["started_at"] = "2026-11-23T10:00:00Z"
    hosted["jobs"][0]["completed_at"] = "2026-11-23T10:59:00Z"
    hosted["jobs"][0]["steps"].insert(
        5,
        {
            "name": "Post Run actions/checkout@v4",
            "status": "completed",
            "conclusion": "success",
            "number": 99,
        },
    )
    rep = run_cmp(local, hosted, outputs, tmp_path)
    assert rep["result"] == "identical"
    assert any(d["kind"] == "hosted_step" and "Post" in d["where"] for d in rep["host_only"])


def test_tolerated_failure_is_success_on_both_sides(docs, tmp_path):
    local, hosted, _ = docs
    step = next(s for s in local["jobs"][0]["steps"] if s["name"] == "Tolerated failure")
    assert step["status"] == "failure" and step["continue_on_error"]
    assert run_cmp(local, hosted, None, tmp_path)["result"] == "identical"
    for s in hosted["jobs"][0]["steps"]:
        if s["name"] == "Tolerated failure":
            s["conclusion"] = "failure"
    assert kinds(run_cmp(local, hosted, None, tmp_path)) == ["step_status"]


# ---- every divergence kind ----------------------------------------------------------------------------


def test_recorded_divergent_fixture_is_detected(tmp_path, capsys):
    argv = ["workflow", "parity", LOCAL, "--hosted", os.path.join(FIX, "hosted-divergent.json")]
    assert loadctl.main([*argv, "--outputs", os.path.join(FIX, "outputs-divergent.json")]) == 1
    out = capsys.readouterr().out
    assert "parity: diverged" in out
    assert "step_status: Report / Final" in out and "job_status: Report" in out
    assert "output_value: Check / Compute / sum" in out and "[local '5', hosted '6']" in out
    assert "host-only (expected, not counted):" in out


def test_step_failed_hosted_but_passed_locally(docs, tmp_path):
    local, hosted, outputs = docs
    hosted["jobs"][0]["steps"][2]["conclusion"] = "failure"
    rep = run_cmp(local, hosted, outputs, tmp_path)
    assert kinds(rep) == ["step_status"]
    d = rep["divergences"][0]
    assert (d["where"], d["local"], d["hosted"]) == ("Check / Compute", "success", "failure")


def test_job_status_and_timed_out_maps_to_failure(docs, tmp_path):
    local, hosted, _ = docs
    hosted["jobs"][1]["conclusion"] = "timed_out"
    rep = run_cmp(local, hosted, None, tmp_path)
    assert kinds(rep) == ["job_status"] and rep["divergences"][0]["hosted"] == "failure"


def test_reordered_steps(docs, tmp_path):
    local, hosted, _ = docs
    steps = hosted["jobs"][0]["steps"]
    steps[2]["number"], steps[3]["number"] = steps[3]["number"], steps[2]["number"]
    rep = run_cmp(local, hosted, None, tmp_path)
    assert kinds(rep) == ["step_order"]
    assert rep["divergences"][0]["hosted"][:2] == ["Run actions/checkout@v4", "Tolerated failure"]


def test_missing_steps_and_jobs_on_both_sides(docs, tmp_path):
    local, hosted, _ = docs
    del hosted["jobs"][0]["steps"][4]  # "Always runs"
    hosted["jobs"][0]["steps"].append(
        {"name": "Extra hosted step", "status": "completed", "conclusion": "success", "number": 30}
    )
    hosted["jobs"].pop(1)
    hosted["jobs"].append({"name": "Deploy", "status": "completed", "conclusion": "success", "steps": []})
    rep = run_cmp(local, hosted, None, tmp_path)
    assert kinds(rep) == [
        "job_missing_hosted",
        "job_missing_local",
        "step_missing_hosted",
        "step_missing_local",
    ]


def test_exit_code_compared_when_the_hosted_step_records_one(docs, tmp_path):
    local, hosted, _ = docs
    hosted["jobs"][0]["steps"][3]["exit_code"] = 3  # tolerated failure: same code
    assert run_cmp(local, hosted, None, tmp_path)["result"] == "identical"
    hosted["jobs"][0]["steps"][3]["exit_code"] = 1
    rep = run_cmp(local, hosted, None, tmp_path)
    assert kinds(rep) == ["exit_code"] and rep["divergences"][0]["local"] == 3


def test_needs_order_on_the_hosted_side(docs, tmp_path):
    local, hosted, _ = docs
    hosted["jobs"][1]["started_at"] = "2026-11-23T14:50:05Z"  # before "Check" completed
    assert kinds(run_cmp(local, hosted, None, tmp_path)) == ["job_order"]


def test_unfinished_runs_are_a_state_divergence(docs, tmp_path):
    local, hosted, _ = docs
    hosted["jobs"][0]["status"] = "in_progress"
    hosted["jobs"][0]["conclusion"] = None
    local["jobs"][1]["status"] = "queued"
    assert kinds(run_cmp(local, hosted, None, tmp_path)) == ["state", "state"]


def test_deterministic_outputs_value_missing_and_unresolved(docs, tmp_path):
    local, hosted, outputs = docs
    outputs["hosted"]["Check"]["Compute"]["sum"] = "6"
    assert kinds(run_cmp(local, hosted, outputs, tmp_path)) == ["output_value"]
    outputs["hosted"] = {}
    assert kinds(run_cmp(local, hosted, outputs, tmp_path)) == ["output_missing"]
    outputs["deterministic"] = [{"job": "Check", "step": "No such step", "output": "sum"}]
    assert kinds(run_cmp(local, hosted, outputs, tmp_path)) == ["output_unresolved"]


def test_outputs_not_declared_deterministic_are_not_compared(docs, tmp_path):
    local, hosted, outputs = docs
    outputs["deterministic"] = []
    outputs["hosted"]["Check"]["Compute"]["sum"] = "anything"
    rep = run_cmp(local, hosted, outputs, tmp_path)
    assert rep["result"] == "identical" and rep["compared"]["outputs"] == 0


# ---- the CLI ------------------------------------------------------------------------------------------


def test_cli_identical_json_and_text(capsys):
    argv = ["workflow", "parity", LOCAL, "--hosted", HOSTED, "--outputs", OUTPUTS]
    assert loadctl.main(argv) == 0
    assert capsys.readouterr().out.startswith("parity: identical (2 job(s), 5 step(s), 1 output(s) compared;")
    assert loadctl.main([*argv, "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["schema"] == 1 and rep["result"] == "identical" and rep["divergences"] == []
    assert rep["local_run"] == "20261123T145551Z-c1a2db"


def test_cli_reads_a_run_from_the_runs_dir(tmp_path, capsys):
    store = records.RunStore(str(tmp_path / "runs"))
    local = load("local-run.json")
    meta = {"workflow": local["workflow"], "event": "push", "project": "web-app"}
    rid = store.create({**meta, "host_id": "h", "created_at": "t"})
    for j in local["jobs"]:
        store.write_job(rid, {"id": None, "lane": "light", "exit_code": None, "duration_s": None, **j})
    argv = ["workflow", "parity", "latest", "--runs-dir", str(tmp_path / "runs"), "--hosted", HOSTED]
    assert loadctl.main(argv) == 0
    assert "parity: identical" in capsys.readouterr().out


def test_cli_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        loadctl.main(["workflow", "parity", LOCAL])  # --hosted is required
    assert e.value.code == 2
    capsys.readouterr()
    assert (
        loadctl.main(["workflow", "parity", "no-such-run", "--hosted", HOSTED, "--runs-dir", "/nonexistent"])
        == 2
    )
    assert "no run 'no-such-run'" in capsys.readouterr().err


# ---- malformed and oversized input: a clear error, never a traceback --------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "is not valid JSON"),
        ("[]", "hosted jobs must be an object"),
        ('{"jobs": "x"}', "hosted jobs.jobs must be a list"),
        ('{"jobs": []}', "the file has no jobs"),
        ('{"jobs": [{"status": "completed"}]}', "jobs[0].name is missing"),
        ('{"jobs": [{"name": 5, "status": "completed"}]}', "jobs[0].name must be a string"),
        (
            '{"jobs": [{"name": "a", "status": "completed", "steps": [1]}]}',
            "jobs[0].steps[0] must be an object",
        ),
        (
            '{"jobs": [{"name": "a", "status": "completed", "steps": [{"name": "s", "status": "completed", "number": "1"}]}]}',
            "number must be an integer",
        ),
        (
            '{"jobs": [{"name": "a", "status": "completed", "labels": [1]}]}',
            "labels must be a list of strings",
        ),
        ("\xff\xfe", "is not valid JSON"),
        ("[" * 100000, "is not valid JSON"),
    ],
)
def test_malformed_hosted_file_is_a_clear_error(tmp_path, capsys, text, message):
    bad = tmp_path / "bad.json"
    bad.write_bytes(text.encode("utf-8", "surrogateescape") if text != "\xff\xfe" else b"\xff\xfe")
    assert loadctl.main(["workflow", "parity", LOCAL, "--hosted", str(bad)]) == 2
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err and err.startswith("loadctl workflow parity: ")


def test_malformed_local_and_outputs_files(tmp_path, capsys):
    bad = tmp_path / "local.json"
    bad.write_text('{"jobs": [{"key": "a", "status": "success", "steps": [{"name": "s"}]}]}')
    assert loadctl.main(["workflow", "parity", str(bad), "--hosted", HOSTED]) == 2
    assert "local run.jobs[0].steps[0].status is missing" in capsys.readouterr().err
    out = tmp_path / "out.json"
    out.write_text('{"deterministic": [{"job": "Check"}]}')
    assert loadctl.main(["workflow", "parity", LOCAL, "--hosted", HOSTED, "--outputs", str(out)]) == 2
    assert "outputs.deterministic[0].step is missing" in capsys.readouterr().err
    assert loadctl.main(["workflow", "parity", LOCAL, "--hosted", str(tmp_path / "gone.json")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_size_bounds_reject_huge_files_and_counts(tmp_path, monkeypatch):
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (parity.MAX_FILE_BYTES + 1))
    with pytest.raises(parity.ParityError, match="larger than 4 MB"):
        parity.load_hosted(str(big))
    monkeypatch.setattr(parity, "MAX_JOBS", 2)
    many = tmp_path / "many.json"
    many.write_text(json.dumps({"jobs": [{"name": f"j{i}", "status": "completed"} for i in range(3)]}))
    with pytest.raises(parity.ParityError, match="has 3 entries \\(at most 2\\)"):
        parity.load_hosted(str(many))
    monkeypatch.setattr(parity, "MAX_STEPS", 1)
    steps = tmp_path / "steps.json"
    steps.write_text(
        json.dumps(
            {
                "jobs": [
                    {"name": "a", "status": "completed", "steps": [{"name": "s", "status": "completed"}] * 2}
                ]
            }
        )
    )
    with pytest.raises(parity.ParityError, match="steps has 2 entries"):
        parity.load_hosted(str(steps))
    long = tmp_path / "long.json"
    long.write_text(json.dumps({"jobs": [{"name": "x" * (parity.MAX_TEXT + 1), "status": "completed"}]}))
    with pytest.raises(parity.ParityError, match="longer than"):
        parity.load_hosted(str(long))


def test_deterministic_list_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(parity, "MAX_DETERMINISTIC", 1)
    f = tmp_path / "o.json"
    f.write_text(json.dumps({"deterministic": [{"job": "a", "step": "b", "output": "c"}] * 2}))
    with pytest.raises(parity.ParityError, match="at most 1"):
        parity.load_outputs(str(f))


def test_the_parity_workflow_loads_and_matches_the_recorded_run():
    from loadgov.workflow.model import load_workflow

    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "docs", "examples", "workflows", "parity-check.yml"
    )
    wf = load_workflow(path)
    local = load("local-run.json")
    assert [i.name for i in wf.instances] == [j["name"] for j in local["jobs"]]
    for inst, rec in zip(wf.instances, local["jobs"], strict=True):
        assert [s.label() for s in inst.job.steps] == [s["name"] for s in rec["steps"]]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda r: r.update(jobs="x"), "local run.jobs must be a list"),
        (lambda r: r["jobs"][0].update(id=["a"]), "jobs[0].id must be a string"),
        (lambda r: r["jobs"][0].update(needs="check"), "jobs[0].needs must be a list"),
        (lambda r: r["jobs"][1].update(needs=[["check"]]), "jobs[1].needs must be a list of strings"),
        (lambda r: r["jobs"][0].update(notes=5), "jobs[0].notes must be a list"),
        (lambda r: r.update(notes=5), "local run.notes must be a list"),
        (lambda r: r.update(notes=[1]), "local run.notes must be a list of strings"),
        (lambda r: r["jobs"][0].update(matrix=[1]), "jobs[0].matrix must be an object"),
        (lambda r: r["jobs"][0]["steps"][0].update(index="1"), "steps[0].index must be an integer"),
        (lambda r: r["jobs"][0]["steps"][0].update(id=3), "steps[0].id must be a string"),
        (lambda r: r["jobs"][0]["steps"][0].update(outputs=[]), "steps[0].outputs must be an object"),
    ],
)
def test_malformed_local_record_exits_2_without_traceback(tmp_path, capsys, mutate, message):
    run = load("local-run.json")
    mutate(run)
    f = tmp_path / "local.json"
    f.write_text(json.dumps(run))
    assert loadctl.main(["workflow", "parity", str(f), "--hosted", HOSTED]) == 2
    err = capsys.readouterr().err
    assert message in err and "Traceback" not in err


def test_null_step_index_does_not_crash(tmp_path, docs):
    local, hosted, _ = docs
    local["jobs"][0]["steps"][0]["index"] = None
    assert run_cmp(local, hosted, None, tmp_path)["result"] == "identical"


def test_deeply_nested_run_in_the_runs_dir_is_a_clear_error(tmp_path, capsys):
    store = records.RunStore(str(tmp_path / "runs"))
    rid = store.create({"workflow": {}, "event": "push", "project": "p"})
    with open(os.path.join(store.run_dir(rid), "run.json"), "w") as f:
        f.write("[" * 100000)
    argv = ["workflow", "parity", rid, "--runs-dir", str(tmp_path / "runs"), "--hosted", HOSTED]
    assert loadctl.main(argv) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.startswith("loadctl workflow parity: ")


def test_post_prefix_is_host_only_only_for_action_steps(docs, tmp_path):
    local, hosted, _ = docs
    hosted["jobs"][0]["steps"].append(
        {"name": "Post comment", "status": "completed", "conclusion": "success", "number": 50}
    )
    assert kinds(run_cmp(local, hosted, None, tmp_path)) == ["step_missing_local"]
