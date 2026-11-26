"""The workflow runner end to end with a fake `loadctl` launcher: lanes and argv, needs ordering and
parallelism, if / continue-on-error / timeouts, matrix, outputs and GITHUB_ENV, records, retention, log caps.

The fake launcher stands in for `loadctl run`: it records the argv and then runs the job script (the real
`python -m loadgov.workflow.jobexec`, real bash steps) in a child process, or returns a chosen exit code."""

import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from loadgov.workflow import jobexec, records, runner
from loadgov.workflow.model import load_workflow, parse_workflow
from loadgov.workflow.records import RunStore

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples", "workflows")


class Fake:
    """A fake `loadctl` launcher. `rc[label]` = exit code returned WITHOUT running the job script;
    `after[label]` = exit code returned after the job script ran."""

    def __init__(self, rc=None, after=None, hold=0.0):
        self.rc, self.after, self.hold = rc or {}, after or {}, hold
        self.calls: list[list[str]] = []
        self.order: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.per_label_peak: dict[str, int] = {}

    @staticmethod
    def label(argv):
        return argv[argv.index("--label") + 1]

    def __call__(self, argv, env):
        label = self.label(argv)
        with self.lock:
            self.calls.append(argv)
            self.order.append(("start", label))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if label in self.rc:
                return self.rc[label]
            cmd = argv[argv.index("--") + 1 :]
            rc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True).returncode
            return self.after.get(label, rc)
        finally:
            with self.lock:
                self.order.append(("end", label))
                self.active -= 1

    def argv_for(self, label):
        return next(a for a in self.calls if self.label(a) == label)


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


@pytest.fixture
def runs(tmp_path):
    return str(tmp_path / "runs")


def deps(fake, **kw):
    return runner.Deps(
        launch=fake,
        loadctl=["loadctl"],
        script=[sys.executable, "-m", "loadgov.workflow.jobexec"],
        host_id="build-host",
        git=lambda _w: ("0123abc", "refs/heads/main"),
        environ=dict(os.environ),
        out=io.StringIO(),
        **kw,
    )


def run(text_or_wf, ws, runs, fake=None, event="workflow_dispatch", explicit=False, **opts):
    fake = fake or Fake()
    if isinstance(text_or_wf, str):
        path = ws.parent / "wf" / "ci.yml"
        path.parent.mkdir(exist_ok=True)
        text = textwrap.dedent(text_or_wf)
        path.write_text(text)
        wf = load_workflow(str(path))
    else:
        wf = text_or_wf
    o = runner.Options(
        project=opts.pop("project", "web-app"), workspace=str(ws), runs_dir=runs, event=event, **opts
    )
    d = deps(fake)
    code, rid, result = runner.run_workflow(wf, o, d, explicit)
    return code, result, fake, d


def job(result, key):
    return next(j for j in result["jobs"] if j["key"] == key)


def log(runs, result, key, step):
    s = job(result, key)["steps"][step - 1]
    with open(os.path.join(runs, result["id"], s["log"]), encoding="utf-8") as f:
        return f.read()


def statuses(result, key):
    return [s["status"] for s in job(result, key)["steps"]]


# ---- examples end to end -----------------------------------------------------------------------------


def test_light_example_on_push_runs_every_step_through_loadctl_run(ws, runs):
    wf = load_workflow(os.path.join(EXAMPLES, "light-check.yml"))
    code, result, fake, _ = run(wf, ws, runs, event="push", key="PROJ-42")
    assert code == 0 and result["status"] == "success"
    argv = fake.argv_for("light-check-lint")
    assert argv[:9] == [
        "loadctl",
        "run",
        "--kind",
        "light",
        "--project",
        "web-app",
        "--label",
        "light-check-lint",
        "--key",
    ]
    assert argv[9] == "PROJ-42" and argv[10] == "--" and argv[11:13] == [sys.executable, "-m"]
    assert statuses(result, "lint") == ["success"] * 5
    j = job(result, "lint")
    assert j["steps"][2]["outputs"] == {"msg": "checked on push"}
    assert log(runs, result, "lint", 4).strip() == "checked on push (plain)"
    assert log(runs, result, "lint", 5).strip() == "pushed to refs/heads/main"
    assert "no-op" in log(runs, result, "lint", 1)


def test_light_example_on_dispatch_skips_the_push_only_step(ws, runs):
    wf = load_workflow(os.path.join(EXAMPLES, "light-check.yml"))
    code, result, _, _ = run(wf, ws, runs)
    assert code == 0
    assert statuses(result, "lint") == ["success"] * 4 + ["skipped"]
    assert log(runs, result, "lint", 4).strip() == "checked on workflow_dispatch (plain)"


def test_gate_pipeline_lanes_ordering_and_slot_serialisation(ws, runs):
    wf = load_workflow(os.path.join(EXAMPLES, "gate-with-needs.yml"))
    fake = Fake()
    code, result, fake, d = run(wf, ws, runs, fake=fake, event="push")
    assert code == 0 and result["status"] == "success"
    kinds = {fake.label(a): a[a.index("--kind") + 1] for a in fake.calls}
    assert kinds == {
        "gate-with-needs-check": "light",
        "gate-with-needs-test-unit-q": "gate",
        "gate-with-needs-test-integration": "gate",
        "gate-with-needs-build": "compile",
        "gate-with-needs-report": "gate",
    }
    pos = {e: i for i, e in enumerate(fake.order)}
    assert pos[("end", "gate-with-needs-check")] < pos[("start", "gate-with-needs-test-unit-q")]
    assert pos[("end", "gate-with-needs-check")] < pos[("start", "gate-with-needs-test-integration")]
    assert pos[("end", "gate-with-needs-test-unit-q")] < pos[("start", "gate-with-needs-build")]
    assert pos[("end", "gate-with-needs-test-integration")] < pos[("start", "gate-with-needs-build")]
    assert pos[("end", "gate-with-needs-build")] < pos[("start", "gate-with-needs-report")]
    assert fake.peak == 1  # max-parallel: 1 on the matrix job, and the rest is a chain
    t = job(result, "test-unit-q")
    assert [s["status"] for s in t["steps"]] == ["success", "success", "failure", "success"]
    assert t["steps"][2]["continue_on_error"] is True and t["status"] == "success"
    assert log(runs, result, "test-unit-q", 2).strip() == "running unit -q"
    assert (
        log(runs, result, "test-integration", 2).strip() == "running integration"
    )  # matrix.flags unset: empty
    assert "building" in log(runs, result, "build", 1)


def test_run_record_shape_is_stable(ws, runs):
    wf = load_workflow(os.path.join(EXAMPLES, "light-check.yml"))
    _, result, _, _ = run(wf, ws, runs, event="push", key="PROJ-42")
    assert {
        "schema",
        "id",
        "workflow",
        "event",
        "project",
        "key",
        "host_id",
        "sha",
        "ref",
        "workspace",
        "created_at",
        "finished_at",
        "status",
        "jobs",
    } <= set(result)
    assert result["schema"] == 1 and result["host_id"] == "build-host" and result["sha"] == "0123abc"
    assert result["workflow"] == {"name": "Checks", "file": wf.path, "stem": "light-check"}
    assert (result["event"], result["project"], result["key"]) == ("push", "web-app", "PROJ-42")
    j = result["jobs"][0]
    assert {
        "key",
        "id",
        "name",
        "matrix",
        "lane",
        "runs_on",
        "label",
        "needs",
        "status",
        "reason",
        "exit_code",
        "loadctl_exit",
        "started_at",
        "finished_at",
        "duration_s",
        "host_id",
        "continue_on_error",
        "steps",
    } <= set(j)
    assert (j["lane"], j["host_id"], j["loadctl_exit"], j["exit_code"]) == ("light", "build-host", 0, 0)
    s = j["steps"][2]
    assert {
        "index",
        "id",
        "name",
        "kind",
        "status",
        "exit_code",
        "started_at",
        "finished_at",
        "duration_s",
        "timed_out",
        "continue_on_error",
        "log",
        "log_truncated",
        "outputs",
        "note",
    } <= set(s)
    assert s["log"] == "logs/lint-3.log" and s["kind"] == "run" and j["steps"][0]["kind"] == "uses"
    json.dumps(result)  # plain JSON


# ---- needs, if, failures ------------------------------------------------------------------------------

DAG = """
    on: push
    jobs:
      a:
        runs-on: loadgov-light
        steps:
          - run: exit 3
      b:
        needs: a
        runs-on: loadgov-light
        steps:
          - run: echo never
      c:
        needs: a
        if: always()
        runs-on: loadgov-light
        steps:
          - run: echo cleanup
      d:
        runs-on: loadgov-light
        steps:
          - run: echo free
      e:
        needs: a
        if: failure()
        runs-on: loadgov-light
        steps:
          - run: echo only on failure
"""


def test_failed_job_skips_dependents_unless_always_or_failure(ws, runs):
    code, result, fake, d = run(DAG, ws, runs, event="push")
    assert code == 1 and result["status"] == "failure"
    by = {j["key"]: j["status"] for j in result["jobs"]}
    assert by == {"a": "failure", "b": "skipped", "c": "success", "d": "success", "e": "success"}
    assert job(result, "a")["exit_code"] == 3 and job(result, "a")["loadctl_exit"] == 3
    assert job(result, "b")["reason"] == "a needed job did not succeed"
    assert statuses(result, "b") == ["skipped"]
    assert "ci-b" not in [Fake.label(a) for a in fake.calls]  # a skipped job never reaches loadctl
    assert "job a: failure" in d.out.getvalue()


def test_job_if_event_and_matrix_conditions(ws, runs):
    text = """
        on: [push, workflow_dispatch]
        jobs:
          p:
            if: github.event_name == 'push'
            runs-on: loadgov-light
            steps: [{run: echo p}]
          q:
            if: ${{ github.event_name != 'push' }}
            runs-on: loadgov-light
            steps: [{run: echo q}]
    """
    _, result, _, _ = run(text, ws, runs, event="push", explicit=True)
    assert {j["key"]: j["status"] for j in result["jobs"]} == {"p": "success", "q": "skipped"}
    assert job(result, "q")["reason"] == "if: github.event_name != 'push' is false"
    assert result["status"] == "success"


def test_job_level_continue_on_error_does_not_fail_the_run_or_dependents(ws, runs):
    text = """
        on: push
        jobs:
          flaky:
            continue-on-error: true
            runs-on: loadgov-light
            steps: [{run: exit 1}]
          after:
            needs: flaky
            runs-on: loadgov-light
            steps: [{run: echo ok}]
    """
    code, result, _, _ = run(text, ws, runs)
    assert code == 0 and result["status"] == "success"
    assert job(result, "flaky")["status"] == "failure" and job(result, "flaky")["continue_on_error"] is True
    assert job(result, "after")["status"] == "success"


def test_all_jobs_skipped_is_skipped_and_ok(ws, runs):
    text = "on: push\njobs:\n  a:\n    if: false\n    runs-on: loadgov-light\n    steps: [{run: x}]\n"
    code, result, fake, _ = run(text, ws, runs)
    assert code == 0 and result["status"] == "skipped" and fake.calls == []


def test_step_if_failure_always_and_default_success(ws, runs):
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            steps:
              - id: bad
                run: exit 7
              - name: skipped by default success()
                run: echo no
              - name: runs on failure
                if: failure()
                run: echo yes
              - name: always
                if: always()
                run: echo always
              - name: cond without status function is ANDed with success()
                if: github.event_name == 'workflow_dispatch'
                run: echo no
              - if: steps.bad.outcome == 'failure' && always()
                run: echo saw ${{ steps.bad.conclusion }}
    """
    code, result, _, _ = run(text, ws, runs)
    assert code == 1
    assert statuses(result, "a") == ["failure", "skipped", "success", "success", "skipped", "success"]
    assert job(result, "a")["exit_code"] == 7
    assert log(runs, result, "a", 6).strip() == "saw failure"


def test_step_continue_on_error_keeps_the_job_green(ws, runs):
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            steps:
              - id: soft
                run: exit 2
                continue-on-error: true
              - run: echo next ${{ steps.soft.outcome }}/${{ steps.soft.conclusion }}
    """
    code, result, _, _ = run(text, ws, runs)
    assert code == 0 and statuses(result, "a") == ["failure", "success"]
    assert log(runs, result, "a", 2).strip() == "next failure/success"


# ---- parallelism --------------------------------------------------------------------------------------


def test_independent_jobs_run_in_parallel_but_needs_are_respected(ws, runs):
    text = """
        on: push
        jobs:
          a: {runs-on: loadgov-light, steps: [{run: sleep 0.6}]}
          b: {runs-on: loadgov-light, steps: [{run: sleep 0.6}]}
          c: {runs-on: loadgov-light, needs: [a, b], steps: [{run: echo c}]}
    """
    t0 = time.monotonic()
    code, result, fake, _ = run(text, ws, runs)
    assert code == 0
    assert fake.peak == 2
    pos = {e: i for i, e in enumerate(fake.order)}
    assert pos[("end", "ci-a")] < pos[("start", "ci-c")] and pos[("end", "ci-b")] < pos[("start", "ci-c")]
    assert time.monotonic() - t0 < 3.0


def test_max_parallel_of_a_matrix_job_and_global_cap(ws, runs):
    text = """
        on: push
        jobs:
          m:
            runs-on: loadgov-light
            strategy:
              max-parallel: 2
              matrix: {n: [1, 2, 3, 4]}
            steps: [{run: sleep 0.3}]
    """
    _, result, fake, _ = run(text, ws, runs)
    assert fake.peak == 2 and len(fake.calls) == 4
    _, _, fake2, _ = run(text.replace("max-parallel: 2", "max-parallel: 4"), ws, runs, max_parallel=3)
    assert fake2.peak == 3


def test_matrix_instances_get_labels_env_and_names(ws, runs):
    text = """
        on: push
        jobs:
          t:
            runs-on: loadgov-gate
            strategy:
              matrix:
                v: [a, b]
            env:
              V: ${{ matrix.v }}
            steps:
              - name: show ${{ matrix.v }}
                run: echo "v=$V ${{ matrix.v }}"
    """
    _, result, fake, _ = run(text, ws, runs)
    assert sorted(Fake.label(a) for a in fake.calls) == ["ci-t-a", "ci-t-b"]
    assert [j["key"] for j in result["jobs"]] == ["t-a", "t-b"]
    assert job(result, "t-b")["matrix"] == {"v": "b"} and job(result, "t-b")["name"] == "t (b)"
    assert log(runs, result, "t-b", 1).strip() == "v=b b"
    assert job(result, "t-b")["steps"][0]["name"] == "show b"


# ---- environment, outputs, shells --------------------------------------------------------------------


def test_env_layers_github_vars_outputs_and_github_env(ws, runs):
    text = """
        on: push
        env:
          LAYER: workflow
          WF: ${{ github.sha }}
        defaults:
          run:
            working-directory: sub
        jobs:
          a:
            runs-on: loadgov-light
            env:
              LAYER: job
              FROM_ENV: ${{ env.WF }}-x
            steps:
              - id: one
                run: |
                  pwd
                  echo "LAYER=$LAYER FROM_ENV=$FROM_ENV"
                  echo "ref=$GITHUB_REF sha=$GITHUB_SHA ev=$GITHUB_EVENT_NAME os=$RUNNER_OS ci=$CI job=$GITHUB_JOB"
                  echo "ws=$GITHUB_WORKSPACE run=$LOADGOV_RUN_ID wf=$GITHUB_WORKFLOW"
                  echo "plain=a b" >> "$GITHUB_OUTPUT"
                  printf 'multi<<EOF\\nline1\\nline2\\nEOF\\n' >> "$GITHUB_OUTPUT"
                  echo "LATER=from-github-env" >> "$GITHUB_ENV"
                  printf 'BLOCK<<X\\na\\nb\\nX\\n' >> "$GITHUB_ENV"
              - env:
                  LAYER: step
                run: echo "$LATER|$BLOCK|$LAYER|${{ steps.one.outputs.plain }}|${{ steps.one.outputs.multi }}"
              - run: echo "${{ env.LATER }} ${{ env.LAYER }}"
    """
    (ws / "sub").mkdir()
    code, result, _, _ = run(text, ws, runs, event="push")
    assert code == 0, result
    first = log(runs, result, "a", 1).splitlines()
    assert first[0] == str(ws / "sub")
    assert first[1] == "LAYER=job FROM_ENV=0123abc-x"
    assert first[2] == "ref=refs/heads/main sha=0123abc ev=push os=Linux ci=true job=a"
    assert first[3] == f"ws={ws} run={result['id']} wf=ci"
    assert job(result, "a")["steps"][0]["outputs"] == {"plain": "a b", "multi": "line1\nline2"}
    assert log(runs, result, "a", 2).splitlines() == ["from-github-env|a", "b|step|a b|line1", "line2"]
    assert log(runs, result, "a", 3).strip() == "from-github-env job"


def test_shells_and_working_directory(ws, runs):
    (ws / "d").mkdir()
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            defaults:
              run:
                shell: sh
            steps:
              - run: echo "sh $0" | cut -c1-2
              - shell: python
                run: |
                  import os
                  print("py", os.path.basename(os.getcwd()))
                working-directory: d
              - shell: bash
                run: |
                  false
                  echo not-reached
              - run: echo after
    """
    code, result, _, _ = run(text, ws, runs)
    assert statuses(result, "a") == ["success", "success", "failure", "skipped"]
    assert log(runs, result, "a", 1).strip() == "sh"
    assert log(runs, result, "a", 2).strip() == "py d"
    assert "not-reached" not in log(runs, result, "a", 3)


def test_missing_working_directory_fails_the_step(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps:\n      - run: echo x\n        working-directory: nope\n"
    code, result, _, _ = run(text, ws, runs)
    assert code == 1 and "working directory" in log(runs, result, "a", 1)


def test_uses_checkout_noop_and_setup_tools(ws, runs):
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
                with: {python-version: '3.99'}
    """
    code, result, _, _ = run(text, ws, runs)
    assert code == 0 and statuses(result, "a") == ["success", "success"]
    assert "using " in log(runs, result, "a", 2) and "3.99" in log(runs, result, "a", 2)


def test_setup_tool_missing_on_the_host_fails_with_a_clear_message(ws, runs):
    text = """
        on: push
        env:
          PATH: /nonexistent-dir
        jobs:
          a:
            runs-on: loadgov-light
            steps:
              - uses: actions/setup-node@v4
                with: {node-version: 20}
              - run: echo after
    """
    code, result, _, _ = run(text, ws, runs)
    assert code == 1 and statuses(result, "a") == ["failure", "skipped"]
    assert job(result, "a")["steps"][0]["exit_code"] == 127
    msg = log(runs, result, "a", 1)
    assert "needs node on this host" in msg and "Nothing is downloaded" in msg


# ---- timeouts and signals ----------------------------------------------------------------------------


def test_step_timeout_kills_the_step_and_records_it(ws, runs):
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            steps:
              - run: sleep 30
                timeout-minutes: 0.01
              - if: always()
                run: echo cleanup
    """
    t0 = time.monotonic()
    code, result, _, _ = run(text, ws, runs)
    assert time.monotonic() - t0 < 15
    s = job(result, "a")["steps"]
    assert (
        code == 1 and s[0]["status"] == "failure" and s[0]["timed_out"] is True and s[0]["exit_code"] == 124
    )
    assert s[1]["status"] == "success"
    assert "timed out" in log(runs, result, "a", 1)
    assert job(result, "a")["exit_code"] == 124


def test_job_timeout_cancels_the_rest(ws, runs):
    text = """
        on: push
        jobs:
          a:
            runs-on: loadgov-light
            timeout-minutes: 0.02
            steps:
              - run: sleep 30
              - if: always()
                run: echo never
    """
    t0 = time.monotonic()
    code, result, _, _ = run(text, ws, runs)
    assert time.monotonic() - t0 < 15
    j = job(result, "a")
    assert code == 1 and j["status"] == "failure" and j["reason"] == "job timeout-minutes exceeded"
    assert [s["status"] for s in j["steps"]] == ["failure", "cancelled"] and j["exit_code"] == 124


def test_sigterm_to_the_job_script_cancels_it_and_kills_the_step(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps:\n      - run: sleep 30\n      - run: echo no\n"
    holder = {}

    def launcher(argv, env):
        p = subprocess.Popen(argv[argv.index("--") + 1 :], env=env, stdout=subprocess.DEVNULL)
        rid = env["LOADGOV_RUN_ID"]
        rec = os.path.join(runs, rid, "jobs", "a.json")
        for _ in range(100):
            try:
                if json.load(open(rec))["steps"][0]["status"] == "running":
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        time.sleep(0.3)
        p.send_signal(signal.SIGTERM)
        holder["rc"] = p.wait(20)
        return holder["rc"]

    code, result, _, _ = run(text, ws, runs, fake=launcher)
    j = job(result, "a")
    assert holder["rc"] == 143 and code == 1
    assert j["status"] == "cancelled" and j["reason"] == "terminated (SIGTERM)"
    assert [s["status"] for s in j["steps"]] == ["cancelled", "cancelled"]


# ---- loadctl outcomes ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rc, word",
    [
        (76, "light lane overrun"),
        (3, "daemon is not reachable"),
        (75, "no room or slot busy"),
        (1, "timed out"),
        (77, "start-by"),
        (9, "loadctl run exited 9"),
    ],
)
def test_loadctl_exit_without_a_started_job_is_a_failure_with_the_reason(ws, runs, rc, word):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo x}, {run: echo y}]\n"
    code, result, _, _ = run(text, ws, runs, fake=Fake(rc={"ci-a": rc}))
    j = job(result, "a")
    assert code == 1 and j["status"] == "failure" and j["loadctl_exit"] == rc and j["exit_code"] == rc
    assert word in j["reason"]
    assert [s["status"] for s in j["steps"]] == ["skipped", "skipped"]


def test_light_overrun_after_the_script_passed_still_fails_the_job(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo x}]\n"
    code, result, _, _ = run(text, ws, runs, fake=Fake(after={"ci-a": 76}))
    j = job(result, "a")
    assert code == 1 and j["status"] == "failure" and "light lane overrun" in j["reason"]
    assert j["steps"][0]["status"] == "success"


def test_light_overrun_kill_is_a_failure_with_the_overrun_reason_not_a_cancel(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: sleep 30}]\n"

    def launcher(argv, env):  # loadctl terminated the job script (SIGTERM, 143) and exits 76
        p = subprocess.Popen(argv[argv.index("--") + 1 :], env=env, stdout=subprocess.DEVNULL)
        rec = os.path.join(runs, env["LOADGOV_RUN_ID"], "jobs", "a.json")
        for _ in range(100):
            try:
                if json.load(open(rec))["steps"][0]["status"] == "running":
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        p.send_signal(signal.SIGTERM)
        p.wait(20)
        return 76

    code, result, _, _ = run(text, ws, runs, fake=launcher)
    j = job(result, "a")
    assert (
        code == 1
        and j["status"] == "failure"
        and "light lane overrun" in j["reason"]
        and j["loadctl_exit"] == 76
    )


def test_a_failing_step_whose_exit_code_equals_a_loadctl_code_keeps_its_own_story(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: exit 5}]\n"
    _, result, _, _ = run(text, ws, runs)
    j = job(result, "a")
    assert j["status"] == "failure" and j["exit_code"] == 5 and j["reason"] is None


def test_killed_job_script_marks_running_step_failed_and_rest_cancelled(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo x}, {run: echo y}]\n"
    wf = parse_workflow(textwrap.dedent(text), "ci.yml")

    def launcher(argv, env):  # the job script died mid-step: a record is left "running"
        rid = env["LOADGOV_RUN_ID"]
        p = os.path.join(runs, rid, "jobs", "a.json")
        rec = json.load(open(p))
        rec["status"] = "running"
        rec["steps"][0]["status"] = "running"
        json.dump(rec, open(p, "w"))
        return 143

    code, result, _, _ = run(wf, ws, runs, fake=launcher)
    j = job(result, "a")
    assert j["status"] == "cancelled" and [s["status"] for s in j["steps"]] == ["failure", "cancelled"]


# ---- image lane -------------------------------------------------------------------------------------


IMAGE = """
    on: push
    jobs:
      img:
        runs-on: [self-hosted, loadgov-image-beta]
        steps: [{run: echo build}]
      after:
        needs: img
        runs-on: loadgov-light
        steps: [{run: echo x}]
      other:
        runs-on: loadgov-light
        steps: [{run: echo y}]
"""


def test_image_jobs_are_submitted_to_the_train_not_run(ws, runs):
    fake = Fake(rc={"ci-img": 0})
    code, result, fake, _ = run(IMAGE, ws, runs, fake=fake, key="PROJ-42")
    argv = fake.argv_for("ci-img")
    assert argv[:10] == [
        "loadctl",
        "image",
        "submit",
        "--project",
        "web-app",
        "--label",
        "ci-img",
        "--channel",
        "beta",
        "--key",
    ]
    assert argv[10:13] == ["PROJ-42", "--cwd", str(ws)] and argv[13] == "--"
    j = job(result, "img")
    assert (
        j["status"] == "queued"
        and "image train" in j["reason"]
        and j["lane"] == "image"
        and j["channel"] == "beta"
    )
    assert job(result, "after")["status"] == "skipped" and "image train" in job(result, "after")["reason"]
    assert job(result, "other")["status"] == "success"
    assert code == 0 and result["status"] == "queued"


def test_image_submit_refused_fails_the_job(ws, runs):
    code, result, _, _ = run(IMAGE, ws, runs, fake=Fake(rc={"ci-img": 4}))
    assert code == 1 and job(result, "img")["status"] == "failure" and job(result, "img")["exit_code"] == 4


def test_image_channel_must_be_named(ws, runs):
    text = IMAGE.replace("loadgov-image-beta", "loadgov-image")
    with pytest.raises(runner.RunnerError, match="channel"):
        run(text, ws, runs)
    code, result, fake, _ = run(text, ws, runs, fake=Fake(rc={"ci-img": 0}), image_channel="release")
    assert "release" in fake.argv_for("ci-img") and code == 0


# ---- options, dry run ---------------------------------------------------------------------------------


def test_job_option_runs_one_job_without_its_needs(ws, runs):
    _, result, fake, _ = run(DAG, ws, runs, job="b")
    assert [j["key"] for j in result["jobs"]] == ["b"] and result["jobs"][0]["status"] == "success"
    with pytest.raises(runner.RunnerError, match="no job 'zz'"):
        run(DAG, ws, runs, job="zz")


def test_explicit_event_must_be_in_on(ws, runs):
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo x}]\n"
    with pytest.raises(runner.RunnerError, match="does not trigger on 'pull_request'"):
        run(text, ws, runs, event="pull_request", explicit=True)
    code, *_ = run(text, ws, runs, event="pull_request", explicit=False)  # not explicit: run on demand
    assert code == 0
    with pytest.raises(runner.RunnerError, match="--event must be"):
        run(text, ws, runs, event="release")


def test_dry_run_prints_the_plan_and_records_nothing(ws, runs):
    wf = load_workflow(os.path.join(EXAMPLES, "gate-with-needs.yml"))
    code, result, fake, d = run(wf, ws, runs, dry_run=True, key="PROJ-42")
    out = d.out.getvalue()
    assert code == 0 and result is None and fake.calls == []
    assert not os.path.exists(runs)
    assert "job test-unit-q: lane gate, needs check" in out
    assert (
        "$ loadctl run --kind compile --project web-app --label gate-with-needs-build --key PROJ-42 --" in out
    )
    assert "note: host-only:" in out and "[if: always()]" in out


# ---- records ----------------------------------------------------------------------------------------


def test_status_derivation():
    d = records.derive_status
    assert d([]) == "queued"
    assert d([{"status": "success"}, {"status": "running"}]) == "running"
    assert d([{"status": "waiting for slot"}, {"status": "queued"}]) == "waiting for slot"
    assert d([{"status": "success"}, {"status": "queued"}]) == "queued"
    assert d([{"status": "success"}, {"status": "failure"}]) == "failure"
    assert d([{"status": "failure", "continue_on_error": True}, {"status": "success"}]) == "success"
    assert d([{"status": "cancelled"}, {"status": "success"}]) == "cancelled"
    assert d([{"status": "skipped"}, {"status": "skipped"}]) == "skipped"
    assert d([{"status": "skipped"}, {"status": "success"}]) == "success"


def test_retention_prunes_oldest_runs_and_only_run_directories(tmp_path):
    store = RunStore(str(tmp_path / "r"), keep=3)
    ids = [store.create({"workflow": {"name": "w"}}, now=1_762_800_000 + i) for i in range(5)]
    assert store.ids() == ids[2:]
    stray = tmp_path / "r" / "not-a-run"
    stray.mkdir()
    (tmp_path / "r" / "20200101T000000Z-aaaaaa").mkdir()  # run-id shaped but no run.json: left alone
    store.create({"workflow": {"name": "w"}}, now=1_762_800_100)
    assert len(store.ids()) == 3 and stray.exists() and (tmp_path / "r" / "20200101T000000Z-aaaaaa").exists()
    assert store.ids()[0] == ids[3]


def test_retention_through_the_runner_keeps_the_last_runs(ws, runs, monkeypatch):
    monkeypatch.setattr(records, "KEEP_RUNS", 2)
    monkeypatch.setattr(records.RunStore.__init__, "__defaults__", (2,))
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo x}]\n"
    for _ in range(4):
        run(text, ws, runs)
        time.sleep(1.05)  # run ids start with the second
    assert len(RunStore(runs).ids()) == 2


def test_resolve_prefix_latest_and_errors(tmp_path):
    store = RunStore(str(tmp_path))
    a = store.create({"workflow": {"name": "w"}}, now=1_762_800_000)
    b = store.create({"workflow": {"name": "w"}}, now=1_762_800_005)
    assert store.resolve("latest") == b and store.resolve(a) == a and store.resolve(b[:17]) == b
    with pytest.raises(LookupError, match="ambiguous"):
        store.resolve("2025")
    with pytest.raises(LookupError, match="no run"):
        store.resolve("nope")


def test_default_runs_dir_rules(tmp_path):
    f = records.default_runs_dir
    assert f({"LOADGOV_RUNS_DIR": "/x/y"}, euid=0) == "/x/y"
    assert f({}, euid=0) == "/var/lib/loadgov/runs"
    assert f({"XDG_STATE_HOME": "/s", "HOME": "/h"}, euid=1000) == "/s/loadgov/runs"
    assert f({"HOME": "/h"}, euid=1000) == "/h/.local/state/loadgov/runs"


def test_log_writer_caps_and_marks_truncation(tmp_path):
    p = str(tmp_path / "l.log")
    w = records.LogWriter(p, cap=100)
    w.write(b"a" * 60)
    w.write(b"b" * 60)
    w.write(b"c" * 10)
    w.close()
    data = open(p, "rb").read()
    assert w.truncated and data.startswith(b"a" * 60 + b"b" * 40)
    assert data.endswith(b"[loadgov: log truncated at 100 bytes]\n") and len(data) == 100 + len(
        records.TRUNCATED.format(n=100)
    )


def test_step_log_is_capped_end_to_end(ws, runs, monkeypatch):
    monkeypatch.setattr(records, "STEP_LOG_MAX", 2000)
    text = "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps:\n      - run: yes line | head -n 5000\n      - run: echo after\n"
    wf = parse_workflow(text, "ci.yml")
    o = runner.Options(project="p", workspace=str(ws), runs_dir=runs, event="push")

    def in_process(argv, env):  # run the job script here so the patched cap applies
        spec = json.load(open(argv[-1]))
        return jobexec.Executor(spec, out=io.StringIO()).run()

    d = deps(in_process)
    code, _, result = runner.run_workflow(wf, o, d)
    s = job(result, "a")["steps"]
    assert code == 0 and s[0]["log_truncated"] is True and s[1]["log_truncated"] is False
    text = open(os.path.join(runs, result["id"], s[0]["log"])).read()
    assert text.endswith("[loadgov: log truncated at 2000 bytes]\n") and len(text) < 2100


@pytest.mark.parametrize(
    "text, want",
    [
        ("a=1\nb=two words\n", {"a": "1", "b": "two words"}),
        ("a<<EOF\nx\ny\nEOF\nb=2\n", {"a": "x\ny", "b": "2"}),
        ("a=\n\nb=x=y\n", {"a": "", "b": "x=y"}),
        ("", {}),
    ],
)
def test_parse_env_file(text, want):
    assert jobexec.parse_env_file(text) == want


@pytest.mark.parametrize("text", ["novalue\n", "a<<EOF\nx\n"])
def test_parse_env_file_rejects_bad_input(text):
    with pytest.raises(ValueError):
        jobexec.parse_env_file(text)


def test_bad_output_file_is_reported_in_the_log_not_fatal(ws, runs):
    text = 'on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps:\n      - run: echo garbage >> "$GITHUB_OUTPUT"\n'
    code, result, _, _ = run(text, ws, runs)
    assert code == 0 and "bad $GITHUB_OUTPUT" in log(runs, result, "a", 1)


def test_interrupt_cancels_pending_jobs_and_exits_130(ws, runs, monkeypatch):
    text = (
        "on: push\njobs:\n  a:\n    runs-on: loadgov-light\n    steps: [{run: echo a}]\n"
        "  b:\n    needs: a\n    runs-on: loadgov-light\n    steps: [{run: echo b}]\n"
    )
    calls = {"n": 0}
    real_wait = runner.cf.wait

    def wait(futs, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_wait(futs, **kw)

    monkeypatch.setattr(runner.cf, "wait", wait)
    code, result, _, _ = run(text, ws, runs)
    assert code == 130
    assert job(result, "b")["status"] == "cancelled" and job(result, "b")["reason"] == "interrupted"
    assert job(result, "a")["status"] in ("success", "queued", "waiting for slot", "running")


def test_docs_name_the_shipped_examples_and_lane_labels():
    doc = open(os.path.join(EXAMPLES, "..", "..", "workflows.md"), encoding="utf-8").read()
    for name in sorted(os.listdir(EXAMPLES)):
        assert f"`{name}`" in doc
    for label in (
        "loadgov-light",
        "loadgov-gate",
        "loadgov-compile",
        "loadgov-image-beta",
        "loadgov-image-release",
    ):
        assert label in doc


def test_colliding_matrix_keys_do_not_overwrite_records_specs_or_logs(ws, runs):
    text = """
        on: push
        jobs:
          t:
            runs-on: loadgov-light
            strategy:
              matrix:
                v: ['a b', 'a-b-3', 'a-b']
            steps:
              - run: echo "v=${{ matrix.v }}"
    """
    code, result, fake, _ = run(text, ws, runs)
    assert code == 0 and len(result["jobs"]) == 3 and len({j["key"] for j in result["jobs"]}) == 3
    seen = sorted(log(runs, result, j["key"], 1).strip() for j in result["jobs"])
    assert seen == ["v=a b", "v=a-b", "v=a-b-3"]
    assert len(os.listdir(os.path.join(runs, result["id"], "specs"))) == 3


def _fake_run(store, now, job_status, finished=True):
    meta = {"workflow": {"name": "w", "file": "w.yml", "stem": "w"}, "event": "push", "project": "p"}
    rid = store.create({**meta, "host_id": "h", "created_at": "t"}, now=now)
    job = {"key": "j", "id": "j", "lane": "light", "order": 0, "status": job_status, "steps": []}
    store.write_job(rid, {**job, "exit_code": None, "duration_s": None})
    if finished:
        store.finish(rid, "t")
    return rid


def test_retention_never_deletes_a_run_with_a_non_final_job(tmp_path):
    store = RunStore(str(tmp_path / "r"), keep=2)
    t0 = int(time.time()) - 1000
    queued = _fake_run(store, t0, "queued")  # an image job still waiting in the train, oldest of all
    running = _fake_run(store, t0 + 1, "running")
    done = [_fake_run(store, t0 + 2 + i, "success") for i in range(4)]
    store.prune()
    left = store.ids()
    assert queued in left and running in left  # protected, and not counted
    assert left == [queued, running, *done[-2:]]  # keep=2 finished runs


def test_retention_stops_protecting_a_stale_unfinished_run(tmp_path):
    store = RunStore(str(tmp_path / "r"), keep=1)
    old = _fake_run(store, int(time.time()) - 40 * 86400, "queued")
    new = [_fake_run(store, int(time.time()) - 10 + i, "success") for i in range(2)]
    store.prune()
    assert store.ids() == [new[1]] and old not in store.ids()


def test_prune_survives_a_run_removed_concurrently(tmp_path, monkeypatch):
    store = RunStore(str(tmp_path / "r"), keep=1)
    for i in range(3):
        store.create({"workflow": {"name": "w"}}, now=int(time.time()) - 10 + i)

    def gone(self, ref):
        raise LookupError("no run")

    monkeypatch.setattr(RunStore, "load_run", gone)
    assert store.active(store.ids()[0]) is False
    store.create({"workflow": {"name": "w"}}, now=int(time.time()))  # prune runs inside create()
