"""Workflow model: loading, validation, refusals, lane mapping, needs, matrix, and the expression subset."""

import os
import textwrap

import pytest

from loadgov.workflow import expr
from loadgov.workflow.model import (
    WorkflowError,
    discover,
    expand_matrix,
    load_workflow,
    map_lane,
    parse_workflow,
)

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples", "workflows")


def wf(text, path="w.yml"):
    return parse_workflow(textwrap.dedent(text), path)


def job(extra="", steps="- run: echo hi", runs_on="ubuntu-latest"):
    body = textwrap.indent(textwrap.dedent(extra), "    ") if extra else ""
    return f"on: push\njobs:\n  a:\n    runs-on: {runs_on}\n{body}    steps:\n{textwrap.indent(steps, '      ')}\n"


def refused(text, *words):
    with pytest.raises(WorkflowError) as e:
        parse_workflow(text, "w.yml")
    msg = str(e.value)
    assert msg.startswith("w.yml:"), msg
    for w in words:
        assert w in msg, msg
    return e.value


# ---- the shipped examples ----------------------------------------------------------------------------


def test_example_light_check_loads_in_the_light_lane():
    w = load_workflow(os.path.join(EXAMPLES, "light-check.yml"))
    assert w.name == "Checks" and w.events == ["push", "pull_request", "workflow_dispatch"]
    assert [(i.key, i.lane) for i in w.instances] == [("lint", "light")]
    assert w.instances[0].job.steps[0].uses == "checkout"
    assert any("actions/checkout is a no-op" in n for n in w.notes)


def test_example_gate_pipeline_lanes_needs_and_matrix():
    w = load_workflow(os.path.join(EXAMPLES, "gate-with-needs.yml"))
    lanes = {i.key: i.lane for i in w.instances}
    assert lanes == {
        "check": "light",
        "test-unit-q": "gate",
        "test-integration": "gate",
        "build": "compile",
        "report": "gate",
    }
    assert w.order() == ["check", "test", "build", "report"]
    assert w.jobs["test"].max_parallel == 1
    assert any("no loadgov-* label" in n for n in w.notes)


def test_example_refused_uses_names_file_line_and_reason():
    with pytest.raises(WorkflowError) as e:
        load_workflow(os.path.join(EXAMPLES, "refused-uses.yml"))
    msg = str(e.value)
    assert "refused-uses.yml:13:9: jobs.build.steps[0].uses:" in msg
    assert "some-org/some-action@v1" in msg and "not supported" in msg
    assert (e.value.line, e.value.col) == (13, 9)


def test_discover_finds_github_and_forgejo_dirs(tmp_path):
    for d, n in (
        (".github/workflows", "a.yml"),
        (".github/workflows", "b.yaml"),
        (".forgejo/workflows", "c.yml"),
    ):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
        (tmp_path / d / n).write_text("on: push\n")
    (tmp_path / ".github/workflows/readme.md").write_text("x")
    found = [os.path.relpath(p, tmp_path) for p in discover(str(tmp_path))]
    assert found == [".github/workflows/a.yml", ".github/workflows/b.yaml", ".forgejo/workflows/c.yml"]
    assert discover(str(tmp_path / "nothing")) == []


# ---- structure and refusals --------------------------------------------------------------------------


def test_minimal_workflow_and_defaults():
    w = wf(job(), "dir/ci.yml")
    assert (w.stem, w.name, w.events) == ("ci", "ci", ["push"])
    j = w.jobs["a"]
    assert j.steps[0].run == "echo hi" and j.steps[0].index == 1
    assert w.instances[0].lane == "gate"


@pytest.mark.parametrize(
    "on, events",
    [
        ("push", ["push"]),
        ("[push, pull_request]", ["push", "pull_request"]),
        ("{push: , workflow_dispatch: }", ["push", "workflow_dispatch"]),
    ],
)
def test_on_forms(on, events):
    assert (
        wf(f"on: {on}\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: x\n").events
        == events
    )


def test_schedule_and_foreign_events_are_recorded_with_notes():
    w = wf(
        "on:\n  schedule:\n    - cron: '0 1 * * *'\n  release:\n    types: [published]\njobs:\n  a:\n    runs-on: x64\n    steps:\n      - run: x\n"
    )
    assert w.events == ["schedule", "release"]
    assert any("`schedule` is recorded, never triggered" in n for n in w.notes)
    assert any("event `release` is never triggered" in n for n in w.notes)


def test_ignored_keys_are_noted_not_silent():
    w = wf(job("permissions: {contents: read}\nconcurrency: x\n") + "permissions: read-all\nname: N\n")
    assert any("`permissions` is ignored" in n for n in w.notes)
    assert any("`concurrency` is ignored" in n for n in w.notes)


@pytest.mark.parametrize(
    "text, words",
    [
        ("jobs: {}\n", ("needs `on`",)),
        ("on: push\n", ("non-empty `jobs`",)),
        ("- a\n", ("must be a mapping",)),
        (
            "on: push\nfoo: 1\njobs:\n  a: {runs-on: x64, steps: [{run: x}]}\n",
            ("`foo` is not supported in a workflow",),
        ),
        (job("container: node:20\n"), ("`container` is not supported", "host-only")),
        (job("services: {db: {image: x}}\n"), ("`services` is not supported",)),
        (job("outputs: {a: b}\n"), ("`outputs` is not supported",)),
        (job("secrets: inherit\n"), ("`secrets` is not supported",)),
        (
            "on: push\njobs:\n  a:\n    uses: ./.github/workflows/x.yml\n",
            ("`uses` is not supported", "reusable"),
        ),
        ("on: push\njobs:\n  a:\n    steps:\n      - run: x\n", ("needs `runs-on`",)),
        (job(runs_on="{group: g}"), ("runs-on must be",)),
        ("on: push\njobs:\n  a:\n    runs-on: x64\n", ("`steps`",)),
        ("on: push\njobs:\n  bad id:\n    runs-on: x64\n    steps: [{run: x}]\n", ("bad job id",)),
        (job(steps="- name: x"), ("exactly one of `run` or `uses`",)),
        (job(steps="- run: x\n  uses: actions/checkout@v4"), ("exactly one of `run` or `uses`",)),
        (job(steps="- run: x\n  weird: 1"), ("`weird` is not supported in a step",)),
        (job(steps="- run: x\n  id: a b"), ("bad step id",)),
        (job(steps="- id: s\n  run: x\n- id: s\n  run: y"), ("duplicate step id 's'",)),
        (job(steps="- run: x\n  shell: pwsh"), ("shell 'pwsh' is not supported",)),
        (job(steps="- run: x\n  timeout-minutes: 0"), ("positive number",)),
        (job(steps="- run: x\n  continue-on-error: 'maybe-not'"), ("unsupported",)),
        (job(steps="- run: x\n  with: {a: b}"), ("`with` applies to `uses` steps",)),
        (job(steps="- uses: actions/checkout@v4\n  shell: bash"), ("apply to `run` steps only",)),
        (job(steps="- uses: actions/checkout"), ("not supported",)),
        (job(steps="- uses: ./local-action"), ("not supported",)),
        (
            job(steps="- uses: actions/upload-artifact@v4"),
            ("`uses: actions/upload-artifact@v4` is not supported", "allowed:"),
        ),
        (job(steps="- uses: docker://alpine:3"), ("not supported",)),
        (job(runs_on="macos-14"), ("no local equivalent",)),
        (job(runs_on="windows-latest"), ("no local equivalent",)),
        (job(runs_on="[self-hosted, gpu]"), ("label 'gpu' has no local equivalent",)),
        (job(runs_on="[loadgov-light, loadgov-gate]"), ("more than one loadgov lane",)),
        (job(runs_on="loadgov-impl"), ("unknown lane label",)),
        (job("env: {1BAD: x}\n"), ("bad environment variable name",)),
        (job("env: {A: [1]}\n"), ("env.A must be a string",)),
        (job("defaults: {run: {shell: zsh}}\n"), ("shell 'zsh' is not supported",)),
        (job("defaults: {tool: x}\n"), ("`tool` is not supported in defaults",)),
    ],
)
def test_refusals(text, words):
    refused(text, *words)


def test_yaml_errors_surface_as_workflow_errors_with_position():
    e = refused("on: push\njobs:\n  a: &x\n    runs-on: ubuntu-latest\n", "anchors")
    assert (e.line, e.col) == (3, 6)


# ---- expressions in the workflow ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra, steps, words",
    [
        ("", "- run: echo ${{ secrets.TOKEN }}", ("secrets",)),
        ("", "- run: echo ${{ vars.X }}", ("vars",)),
        ("", "- run: echo ${{ inputs.x }}", ("inputs",)),
        ("", "- run: echo ${{ github.token }}", ("github.token",)),
        ("", "- run: echo ${{ runner.arch }}", ("runner.os",)),
        ("", "- run: echo ${{ env.A.B }}", ("env.NAME",)),
        ("", "- run: echo ${{ format('{0}', 1) }}", ("format() is not supported",)),
        ("", "- run: echo ${{ hashFiles('**') }}", ("hashFiles() is not supported",)),
        ("", "- run: echo ${{ success() }}", ("only supported in `if:`",)),
        ("", "- run: echo ${{ matrix.os }}", ("the job's matrix has no key 'os'",)),
        (
            "",
            "- run: echo ${{ steps.later.outputs.x }}\n- id: later\n  run: x",
            ("no earlier step has the id 'later'",),
        ),
        ("", "- run: echo ${{ steps.s.nope }}", ("steps.ID.outputs.NAME",)),
        ("", "- run: echo ${{ 1 + 2 }}", ("unsupported",)),
        ("", "- run: echo ${{ a ", ("unterminated",)),
        ("", "- run: x\n  if: contains(github.ref, 'x')", ("contains() is not supported",)),
        ("", "- run: x\n  if: always() == github.token", ("github.token",)),
        ("", "- run: x\n  if: ${{ a }} and ${{ b }}", ("mixes text",)),
        ("", "- run: x\n  if: needs.a.result == 'success'", ("needs",)),
        ("", "- run: x\n  if: github.event.pull_request.draft", ("github.event.pull_request.draft",)),
        ("if: secrets.X\n", "- run: x", ("secrets",)),
        ("if: steps.a.outputs.b\n", "- run: x", ("steps",)),
        ("env: {A: '${{ steps.s.outputs.x }}'}\n", "- run: x", ("steps",)),
    ],
)
def test_unsupported_expressions_are_refused_at_load(extra, steps, words):
    refused(job(extra, steps), *words)


def test_supported_expressions_load():
    w = wf(
        job(
            """\
            if: github.event_name == 'push' || always()
            env:
              A: ${{ github.sha }}
              B: ${{ runner.os }}
            """,
            "- id: s\n  run: echo ${{ env.A }}\n- run: echo ${{ steps.s.outputs.x }} ${{ steps.s.outcome }}\n"
            "  if: ${{ !cancelled() && steps.s.conclusion == 'success' }}\n"
            "- run: x\n  if: failure() || github.ref != 'refs/heads/main'\n",
        )
    )
    assert w.jobs["a"].cond == "github.event_name == 'push' || always()"
    assert w.jobs["a"].steps[1].cond == "!cancelled() && steps.s.conclusion == 'success'"


def test_boolean_if_and_continue_on_error_forms():
    w = wf(
        job(
            steps="- run: x\n  if: false\n  continue-on-error: true\n- run: y\n  continue-on-error: ${{ env.A == 'x' }}"
        )
    )
    s = w.jobs["a"].steps
    assert (s[0].cond, s[0].continue_on_error) == ("false", True)
    assert s[1].continue_on_error == "env.A == 'x'"


# ---- lane mapping -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "labels, want",
    [
        (["self-hosted", "loadgov-light"], ("light", None)),
        (["loadgov-gate"], ("gate", None)),
        (["self-hosted", "linux", "x64", "loadgov-compile"], ("compile", None)),
        (["LoadGov-Light"], ("light", None)),
        (["loadgov-image-beta"], ("image", "beta")),
        (["loadgov-image-release"], ("image", "release")),
        (["loadgov-image"], ("image", None)),
        (["loadgov-gate", "loadgov-gate"], ("gate", None)),
    ],
)
def test_lane_label_picks_the_lane(labels, want):
    lane, channel, notes = map_lane(labels)
    assert (lane, channel) == want and notes == []


@pytest.mark.parametrize(
    "labels", [["ubuntu-latest"], ["ubuntu-24.04"], ["self-hosted"], ["docker"], ["ubuntu-22.04-arm"]]
)
def test_plain_labels_default_to_gate_with_a_host_only_note(labels):
    lane, channel, notes = map_lane(labels)
    assert (lane, channel) == ("gate", None)
    assert len(notes) == 1 and "host-only" in notes[0] and "gate lane" in notes[0]


def test_matrix_runs_on_maps_per_combination():
    w = wf(
        """\
        on: push
        jobs:
          a:
            runs-on: ${{ matrix.lane }}
            strategy:
              matrix:
                lane: [loadgov-light, loadgov-gate]
            steps:
              - run: x
        """
    )
    assert [(i.key, i.lane) for i in w.instances] == [
        ("a-loadgov-light", "light"),
        ("a-loadgov-gate", "gate"),
    ]


# ---- needs ------------------------------------------------------------------------------------------

_RO = "runs-on: x64\n    steps: [{run: x}]\n"


def jobs_text(**needs):
    out = "on: push\njobs:\n"
    for j, n in needs.items():
        out += f"  {j}:\n    {_RO}"
        if n:
            out += f"    needs: {n}\n"
    return out


def test_needs_string_list_and_topological_order():
    w = wf(jobs_text(c="[a, b]", b="a", a=""))
    assert w.order() == ["a", "b", "c"]
    assert w.jobs["b"].needs == ["a"] and w.jobs["c"].needs == ["a", "b"]


def test_needs_cycle_is_refused_with_the_cycle():
    e = refused(jobs_text(a="c", b="a", c="b"), "dependency cycle", "a -> c -> b -> a")
    assert e.path.startswith("jobs.")


def test_needs_self_and_unknown():
    refused(jobs_text(a="a"), "dependency cycle", "a -> a")
    refused(jobs_text(a="ghost"), "needs unknown job 'ghost'")
    refused(jobs_text(a="[1]"), "needs must be")


# ---- matrix -----------------------------------------------------------------------------------------


def test_matrix_product_order():
    assert expand_matrix({"os": ["a", "b"], "v": [1, 2]}) == [
        {"os": "a", "v": 1},
        {"os": "a", "v": 2},
        {"os": "b", "v": 1},
        {"os": "b", "v": 2},
    ]
    assert expand_matrix(None) == [{}]


def test_matrix_exclude_partial_match():
    got = expand_matrix({"os": ["a", "b"], "v": [1, 2], "exclude": [{"os": "a", "v": 2}, {"v": 9}]})
    assert got == [{"os": "a", "v": 1}, {"os": "b", "v": 1}, {"os": "b", "v": 2}]


def test_matrix_include_extends_matching_and_adds_new():
    m = {
        "os": ["a", "b"],
        "v": [1],
        "include": [{"os": "a", "extra": "x"}, {"os": "c", "v": 3}, {"v": 1, "all": True}],
    }
    assert expand_matrix(m) == [
        {"os": "a", "v": 1, "extra": "x", "all": True},
        {"os": "b", "v": 1, "all": True},
        {"os": "c", "v": 3},
    ]
    assert expand_matrix({"include": [{"x": 1}, {"x": 2}]}) == [{"x": 1}, {"x": 2}]


def test_matrix_include_does_not_overwrite_original_values():
    got = expand_matrix({"v": [1, 2], "include": [{"v": 1, "w": "a"}]})
    assert got == [{"v": 1, "w": "a"}, {"v": 2}]


def test_matrix_instances_keys_names_and_continue_on_error():
    w = wf(
        """\
        on: push
        jobs:
          t:
            name: Test ${{ matrix.py }}
            runs-on: loadgov-gate
            continue-on-error: ${{ matrix.exp == true }}
            strategy:
              max-parallel: 2
              fail-fast: false
              matrix:
                py: ['3.12', '3.13']
                exp: [false]
                include:
                  - py: '3.13'
                    exp: true
                    note: n
            steps:
              - run: echo ${{ matrix.py }} ${{ matrix.note }}
        """
    )
    assert w.jobs["t"].max_parallel == 2
    got = [(i.key, i.name, i.continue_on_error) for i in w.instances]
    assert got == [
        ("t-3.12-false", "Test 3.12", False),
        ("t-3.13-false", "Test 3.13", False),
        ("t-3.13-true-n", "Test 3.13", True),
    ]
    assert any("fail-fast" in n for n in w.notes)


def test_matrix_unique_keys_on_collision():
    w = wf(
        "on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix:\n        v: ['a b', 'a-b']\n    steps: [{run: x}]\n"
    )
    keys = [i.key for i in w.instances]
    assert len(set(keys)) == 2


@pytest.mark.parametrize(
    "matrix, words",
    [
        ("matrix: ${{ fromJSON(x) }}", ("matrix must be a mapping",)),
        ("matrix:\n        os: ${{ x }}", ("non-empty list",)),
        ("matrix: {os: []}", ("non-empty list",)),
        ("matrix: {os: [[1]]}", ("strings, numbers or booleans",)),
        ("matrix: {os: ['${{ x }}']}", ("not supported",)),
        ("matrix: {include: x}", ("list of mappings",)),
        ("matrix: {os: [a]}\n      max-parallel: 0", ("max-parallel",)),
        ("matrix: {os: [a]}\n      fail-fast: maybe", ("fail-fast",)),
        ("matrix: {os: [a]}\n      weird: 1", ("`weird` is not supported in strategy",)),
        ("matrix: {os: [a], exclude: [{os: a}]}", ("expands to 0 jobs",)),
    ],
)
def test_matrix_refusals(matrix, words):
    refused(
        f"on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      {matrix}\n    steps: [{{run: x}}]\n",
        *words,
    )


def test_matrix_too_large_is_refused():
    vals = ", ".join(str(i) for i in range(20))
    refused(
        f"on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix: {{a: [{vals}], b: [{vals}]}}\n    steps: [{{run: x}}]\n",
        "expands to 400 jobs",
    )


# ---- uses -------------------------------------------------------------------------------------------


def test_allowed_uses_are_mapped_with_notes():
    w = wf(
        job(
            steps="- uses: actions/checkout@v4\n  with: {fetch-depth: 0}\n"
            "- uses: actions/setup-python@v5\n  with: {python-version: '3.12'}\n"
            "- uses: actions/setup-node@v4\n  with: {node-version: 20}\n"
        )
    )
    s = w.jobs["a"].steps
    assert [x.uses for x in s] == ["checkout", "setup-python", "setup-node"]
    assert s[1].with_ == {"python-version": "3.12"} and s[2].with_ == {"node-version": "20"}
    assert any("only checks that the tool exists" in n for n in w.notes)
    assert any("`with:` inputs" in n for n in w.notes)


# ---- expression subset ------------------------------------------------------------------------------

CTX = {
    "github": {"event_name": "push", "ref": "refs/heads/main", "sha": "abc"},
    "env": {"A": "x", "N": "0"},
    "matrix": {"os": "Linux", "n": 3},
    "runner": {"os": "Linux"},
    "steps": {"s": {"outputs": {"v": "1"}, "outcome": "failure", "conclusion": "success"}},
}


def ev(text, ctx=CTX, **status):
    st = {"success": True, "failure": False, "cancelled": False} | status
    return expr.evaluate(expr.parse_expr(text), ctx, st)


@pytest.mark.parametrize(
    "text, want",
    [
        ("github.event_name == 'push'", True),
        ("github.event_name == 'PUSH'", True),
        ("github.event_name != 'push'", False),
        ("env.A == 'x' && matrix.os == 'linux'", True),
        ("env.A == 'y' || env.A == 'x'", True),
        ("!(env.A == 'y')", True),
        ("!env.Missing", True),
        ("matrix.n == 3", True),
        ("matrix.n == '3'", True),
        ("env.N == 0", True),
        ("steps.s.outputs.v == '1'", True),
        ("steps.s.outcome == 'failure' && steps.s.conclusion == 'success'", True),
        ("true && false", False),
        ("null == ''", True),
        ("'it''s' == 'IT''S'", True),
        ("success()", True),
        ("failure()", False),
        ("always()", True),
        ("cancelled()", False),
        ("(true || false) && !false", True),
        ("env.Missing == null", True),
    ],
)
def test_expression_evaluation(text, want):
    assert bool(ev(text)) is want


def test_and_or_return_operands_like_actions():
    assert ev("env.A || 'z'") == "x"
    assert ev("env.Missing || 'z'") == "z"
    assert ev("env.A && matrix.os") == "Linux"


def test_status_functions_follow_the_status():
    assert ev("success()", success=False) is False
    assert ev("failure()", success=False, failure=True) is True
    assert ev("cancelled()", cancelled=True) is True
    assert ev("always()", success=False) is True


def test_should_run_adds_implicit_success():
    ok, bad = {"success": True}, {"success": False, "failure": True}
    assert expr.should_run(None, CTX, ok) and not expr.should_run(None, CTX, bad)
    assert expr.should_run("env.A == 'x'", CTX, ok)
    assert not expr.should_run("env.A == 'x'", CTX, bad)  # no status function: success() && ...
    assert expr.should_run("failure() && env.A == 'x'", CTX, bad)
    assert expr.should_run("always()", CTX, bad)


@pytest.mark.parametrize(
    "text, word",
    [
        ("", "empty"),
        ("a +", "unsupported expression syntax"),
        ("(a", "missing ')'"),
        ("a b", "unexpected"),
        ("contains(a, 'b')", "not supported"),
        ("success(1)", "with arguments"),
        ("toJSON()", "not supported"),
        ("a == ", "incomplete"),
        ("a['b']", "unsupported expression syntax"),
        ("a < 1", "unsupported expression syntax"),
    ],
)
def test_expression_syntax_errors(text, word):
    with pytest.raises(expr.ExprError) as e:
        expr.parse_expr(text)
    assert word in str(e.value)


def test_interpolation_and_rendering():
    assert (
        expr.interpolate("a ${{ env.A }} b ${{ matrix.n }} ${{ true }} ${{ null }}.", CTX) == "a x b 3 true ."
    )
    assert expr.interpolate("${{ matrix.n == 3 }}", CTX) == "true"
    assert expr.interpolate("no expressions", CTX) == "no expressions"
    assert expr.interpolate("${{ 'a}}b' }}", CTX) == "a}}b"
    assert expr.render(2.0) == "2" and expr.render(2.5) == "2.5"


def test_condition_normalisation():
    assert expr.condition("${{ a == 'b' }}") == "a == 'b'"
    assert expr.condition("  a == 'b' ") == "a == 'b'"
    assert expr.condition(True) == "true"
    for bad in ("", 3, None, "x ${{ y }}"):
        with pytest.raises(expr.ExprError):
            expr.condition(bad)


# ---- bounded expressions, unique keys ----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["!" * 1000 + "a", "(" * 1000 + "a" + ")" * 1000, " && ".join(["a"] * 300)])
def test_deep_or_long_expressions_are_refused(bad):
    with pytest.raises(expr.ExprError, match="nested more than 64|more than 256 tokens"):
        expr.parse_expr(bad)
    assert expr.parse_expr("!" * 60 + "env.A")


def test_deep_expression_in_a_workflow_is_a_refusal_with_position():
    e = refused(job(steps="- run: echo ${{ " + "!" * 100 + "env.A }}"), "nested more than 64")
    assert e.line == 6 and e.path == "jobs.a.steps[0].run"
    refused(job(steps="- run: x\n  if: " + "(" * 100 + "true" + ")" * 100), "nested more than 64")


def test_deep_yaml_in_a_workflow_is_a_refusal():
    e = refused("on: push\njobs: " + "[" * 1000 + "]" * 1000 + "\n", "nested more than 64")
    assert e.line == 2


def test_recursion_error_safety_net_in_the_loader(monkeypatch):
    import loadgov.workflow.model as m

    def boom(*a):
        raise RecursionError

    monkeypatch.setattr(m, "_parse_workflow", boom)
    refused("on: push\n", "nests too deeply")


def test_matrix_job_keys_stay_unique_whatever_the_values():
    w = wf(
        "on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix:\n        v: ['a b', 'a-b-3', 'a-b', 'a-b']\n    steps: [{run: x}]\n"
    )
    keys = [i.key for i in w.instances]
    assert len(keys) == 4 and len(set(keys)) == 4
    w = wf(
        "on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix: {v: [b]}\n    steps: [{run: x}]\n  t-b:\n    runs-on: x64\n    steps: [{run: x}]\n"
    )
    assert len({i.key for i in w.instances}) == 2


# ---- matrix size ceilings are checked before expanding ----------------------------------------------


def _matrix_wf(keys, per, extra=""):
    vals = ", ".join(str(i) for i in range(per))
    body = "".join(f"        k{i}: [{vals}]\n" for i in range(keys))
    return (
        "on: push\njobs:\n  t:\n    runs-on: x64\n    strategy:\n      matrix:\n"
        + body
        + extra
        + "    steps: [{run: x}]\n"
    )


def test_huge_matrix_product_is_refused_instantly_before_expanding():
    import time

    t0 = time.monotonic()
    e = refused(_matrix_wf(8, 10), "100000000 combinations", "at most 4096")
    assert (e.line, e.path) == (6, "jobs.t.strategy.matrix") and time.monotonic() - t0 < 1.0


def test_huge_but_mostly_excluded_matrix_is_still_bounded():
    import time

    t0 = time.monotonic()
    refused(_matrix_wf(8, 10, "        exclude:\n          - {k0: 0}\n"), "combinations", "at most 4096")
    # within the raw ceiling: 4096 combinations of which 64 survive the excludes -> fine and fast
    ex = "        exclude:\n" + "".join(
        f"          - {{{k}: {i}}}\n" for k in ("k0", "k1") for i in range(1, 8)
    )
    ok = wf(_matrix_wf(4, 8, ex))
    assert len(ok.instances) == 64
    assert time.monotonic() - t0 < 2.0


def test_matrix_over_256_survivors_is_refused_and_exactly_256_is_fine():
    refused(_matrix_wf(4, 8), "expands to 4096 jobs")
    assert len(wf(_matrix_wf(2, 16)).instances) == 256


def test_include_exclude_entry_count_is_bounded():
    inc = "".join(f"          - {{k0: {i}}}\n" for i in range(300))
    refused(_matrix_wf(1, 2, "        include:\n" + inc), "300 entries")


def test_total_instances_per_workflow_is_bounded():
    jobs = "".join(
        f"  j{n}:\n    runs-on: x64\n    strategy:\n      matrix:\n        a: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]\n"
        f"        b: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]\n    steps: [{{run: x}}]\n"
        for n in range(5)
    )
    refused("on: push\njobs:\n" + jobs, "more than 1024 job instances")
    assert len(wf("on: push\njobs:\n" + jobs.split("  j4:")[0]).instances) == 1024
