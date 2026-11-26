"""The restricted YAML parser: supported subset, typing rules, real workflow snippets, refusals, and a
property-style round of comparisons against expected Python structures."""

import json
import math
import random

import pytest

from loadgov.workflow.yaml_subset import YamlError, parse, parse_located


def refused(text, *words, where=None):
    with pytest.raises(YamlError) as e:
        parse(text, "w.yml")
    msg = str(e.value)
    assert msg.startswith("w.yml:")
    for w in words:
        assert w in msg, msg
    if where:
        assert msg.startswith(f"w.yml:{where[0]}:{where[1]}:"), msg
    return e.value


# ---- scalars and typing ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, want",
    [
        ("a: 1", {"a": 1}),
        ("a: -7", {"a": -7}),
        ("a: 0x1F", {"a": 31}),
        ("a: 0o17", {"a": 15}),
        ("a: 1.5", {"a": 1.5}),
        ("a: 3.10", {"a": 3.1}),
        ("a: 1e3", {"a": 1000.0}),
        ("a: true", {"a": True}),
        ("a: False", {"a": False}),
        ("a: null", {"a": None}),
        ("a: ~", {"a": None}),
        ("a:", {"a": None}),
        ("a: on", {"a": "on"}),
        ("a: yes", {"a": "yes"}),
        ("a: no", {"a": "no"}),
        ("a: 1_000", {"a": "1_000"}),
        ("a: 1.2.3", {"a": "1.2.3"}),
        ("a: '1'", {"a": "1"}),
        ('a: "true"', {"a": "true"}),
        ("a: hello world", {"a": "hello world"}),
        ("a: http://x.test/y", {"a": "http://x.test/y"}),
        ("a: b#c", {"a": "b#c"}),
        ("a: b # comment", {"a": "b"}),
        ("a: 'it''s'", {"a": "it's"}),
        ('a: "x # not a comment"', {"a": "x # not a comment"}),
        ("a: ${{ matrix.os }}", {"a": "${{ matrix.os }}"}),
        ("a: echo ${{ x }}", {"a": "echo ${{ x }}"}),
        ("a: .inf", {"a": math.inf}),
        ("'quoted key': 1", {"quoted key": 1}),
        ('"k: x": 1', {"k: x": 1}),
        ("true: 1", {"true": 1}),
        ("1: a", {"1": "a"}),
    ],
)
def test_scalars(text, want):
    assert parse(text) == want


def test_nan():
    assert math.isnan(parse("a: .nan")["a"])


def test_on_key_is_the_string_on_not_true():
    got = parse("on: push\njobs: {}")
    assert list(got) == ["on", "jobs"]
    assert True not in got and got["on"] == "push"
    assert list(parse("on:\n  push:\n  pull_request:\n")["on"]) == ["push", "pull_request"]


@pytest.mark.parametrize(
    "text, want",
    [
        (r'a: "x\ty"', "x\ty"),
        (r'a: "line\nbreak"', "line\nbreak"),
        (r'a: "q\"q"', 'q"q'),
        (r'a: "back\\slash"', "back\\slash"),
        (r'a: "\x41\u00e9\U0001F600"', "A\u00e9\U0001f600"),
        (r'a: "\/\0\e"', "/\0\x1b"),
        (r'a: "\_\N"', "\xa0\x85"),
    ],
)
def test_double_quote_escapes(text, want):
    assert parse(text) == {"a": want}


def test_multiline_quoted_and_plain_fold():
    assert parse('a: "one\n  two\n\n  three"') == {"a": "one two\nthree"}
    assert parse("a: 'one\n  two'") == {"a": "one two"}
    assert parse('a: "join\\\n  ed"') == {"a": "joined"}
    assert parse("a: one\n  two\n  three\nb: 1") == {"a": "one two three", "b": 1}
    assert parse("a: one\n\n  two") == {"a": "one\ntwo"}


# ---- block scalars -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, want",
    [
        ("a: |\n  x\n  y\n", "x\ny\n"),
        ("a: |\n  x\n  y\nb: 1", "x\ny\n"),
        ("a: |-\n  x\n  y\n", "x\ny"),
        ("a: |+\n  x\n\n\nb: 1", "x\n\n\n"),
        ("a: |\n  x\n\n\nb: 1", "x\n"),
        ("a: |\n  x\n\n  y\n", "x\n\ny\n"),
        ("a: |\n  x\n    indented\n  y\n", "x\n  indented\ny\n"),
        ("a: >\n  x\n  y\n", "x y\n"),
        ("a: >-\n  x\n  y\n", "x y"),
        ("a: >+\n  x\n\nb: 1", "x\n\n"),
        ("a: >\n  x\n\n  y\n", "x\ny\n"),
        ("a: >\n  x\n    more\n  y\n", "x\n  more\ny\n"),
        ("a: |2\n    x\n  y\n", "  x\ny\n"),
        ("a: |- # why\n  x\n", "x"),
        ("a: |\n  # not a comment\n  x\n", "# not a comment\nx\n"),
        ("a: |\n", ""),
        ("a: |+\n\n\nb: 1", "\n\n"),
    ],
)
def test_block_scalars(text, want):
    assert parse(text)["a"] == want


def test_block_scalar_in_a_sequence_and_nested_indentation():
    got = parse("steps:\n  - run: |\n      echo a\n      echo b\n  - run: >-\n      c\n      d\n")
    assert got == {"steps": [{"run": "echo a\necho b\n"}, {"run": "c d"}]}
    assert parse("- |\n  x\n- y") == ["x\n", "y"]


def test_block_scalar_keeps_yaml_looking_content():
    got = parse("run: |\n  - not: a list\n  - &x *y !z ? # c\n  ---\nafter: 1")
    assert got["run"] == "- not: a list\n- &x *y !z ? # c\n---\n"
    assert got["after"] == 1


# ---- collections -------------------------------------------------------------------------------------


def test_block_collections_and_indentation_forms():
    text = "a:\n- 1\n- 2\nb:\n  - x: 1\n    y: 2\n  -\n    z: 3\n  - - n1\n    - n2\nc:\n  d:\n    e: f\n"
    assert parse(text) == {
        "a": [1, 2],
        "b": [{"x": 1, "y": 2}, {"z": 3}, ["n1", "n2"]],
        "c": {"d": {"e": "f"}},
    }


def test_comments_blank_lines_and_crlf():
    text = "# top\r\na: 1 # c\r\n\r\n  # indented comment\r\nb:\r\n  - 1 # x\r\n  # between\r\n  - 2\r\n"
    assert parse(text) == {"a": 1, "b": [1, 2]}


def test_document_start_and_empty():
    assert parse("---\na: 1") == {"a": 1}
    assert parse("--- # c\na: 1") == {"a": 1}
    assert parse("") is None
    assert parse("# only a comment\n") is None
    assert parse("\ufeffa: 1") == {"a": 1}
    assert parse("just text") == "just text"
    assert parse("[1, 2]") == [1, 2]


def test_flow_collections():
    assert parse("a: [x, 'y z', 3, true, null, ~]") == {"a": ["x", "y z", 3, True, None, None]}
    assert parse("a: []\nb: {}") == {"a": [], "b": {}}
    assert parse("a: {k: v, 'q': 1, n: }") == {"a": {"k": "v", "q": 1, "n": None}}
    assert parse("a: [[1, 2], {b: [3]}]") == {"a": [[1, 2], {"b": [3]}]}
    assert parse("a: [x, y,] # trailing") == {"a": ["x", "y"]}
    assert parse("a: [ubuntu-latest, macos-14]") == {"a": ["ubuntu-latest", "macos-14"]}
    assert parse("a: {on: push, 1: x}") == {"a": {"on": "push", "1": "x"}}
    assert parse("a: [a:b, 'c, d']") == {"a": ["a:b", "c, d"]}


def test_located_paths():
    loc = parse_located("a: 1\nb:\n  c:\n    - x\n    - y: 2\n")
    assert loc.where("a") == (1, 1)
    assert loc.where("b", "c") == (3, 3)
    assert loc.where("b", "c", 1, "y") == (5, 7)
    assert loc.where("missing") == (1, 1)


# ---- real-world workflow snippets --------------------------------------------------------------------

CI = """\
name: CI

on:
  push:
    branches: [ main ]
    paths-ignore:
      - '**.md'
  pull_request:
  workflow_dispatch:
  schedule:
    - cron: '0 3 * * 1'

env:
  CARGO_TERM_COLOR: always
  RUST_BACKTRACE: 1

defaults:
  run:
    shell: bash

jobs:
  lint:
    name: Lint (${{ matrix.python }})
    runs-on: ubuntu-latest
    timeout-minutes: 10
    strategy:
      fail-fast: false
      max-parallel: 2
      matrix:
        python: ['3.11', "3.12", 3.13]
        include:
          - python: '3.13'
            experimental: true
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - name: Install
        run: |
          python -m pip install -U pip
          pip install -e ".[dev]"
      - name: Test
        if: ${{ github.event_name == 'push' && !cancelled() }}
        env:
          MSG: "a: b # not a comment"
        run: >
          pytest -q
          --maxfail=1
      - id: out
        run: echo "name=value" >> "$GITHUB_OUTPUT"
        continue-on-error: ${{ matrix.experimental == true }}
  build:
    needs: [lint]
    runs-on: [self-hosted, loadgov-gate]
    steps:
      - run: make build
"""


def test_real_world_ci_workflow():
    w = parse(CI, ".github/workflows/ci.yml")
    assert list(w) == ["name", "on", "env", "defaults", "jobs"]
    assert w["on"]["push"] == {"branches": ["main"], "paths-ignore": ["**.md"]}
    assert w["on"]["schedule"] == [{"cron": "0 3 * * 1"}]
    assert w["env"] == {"CARGO_TERM_COLOR": "always", "RUST_BACKTRACE": 1}
    lint = w["jobs"]["lint"]
    assert lint["strategy"]["matrix"]["python"] == ["3.11", "3.12", 3.13]
    assert lint["strategy"]["matrix"]["include"] == [{"python": "3.13", "experimental": True}]
    assert lint["strategy"]["fail-fast"] is False
    steps = lint["steps"]
    assert steps[1]["with"] == {"python-version": "${{ matrix.python }}"}
    assert steps[2]["run"] == 'python -m pip install -U pip\npip install -e ".[dev]"\n'
    assert steps[3]["env"]["MSG"] == "a: b # not a comment"
    assert steps[3]["run"] == "pytest -q --maxfail=1\n"
    assert steps[3]["if"] == "${{ github.event_name == 'push' && !cancelled() }}"
    assert steps[4]["run"] == 'echo "name=value" >> "$GITHUB_OUTPUT"'
    assert w["jobs"]["build"]["runs-on"] == ["self-hosted", "loadgov-gate"]


def test_forgejo_style_snippet():
    w = parse("on: [push]\njobs:\n  t:\n    runs-on: docker\n    steps:\n      - run: echo hi\n")
    assert w == {"on": ["push"], "jobs": {"t": {"runs-on": "docker", "steps": [{"run": "echo hi"}]}}}


# ---- refusals ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, words, where",
    [
        ("a: &x 1", ("anchors",), (1, 4)),
        ("a: 1\nb: &x\n  c: 1", ("anchors",), (2, 4)),
        ("&x a: 1", ("anchors",), (1, 1)),
        ("a: *x", ("aliases",), (1, 4)),
        ("a: [*x]", ("aliases",), (1, 5)),
        ("- *x", ("aliases",), (1, 3)),
        ("a: !tag x", ("tags",), (1, 4)),
        ("a: !!str 1", ("tags",), (1, 4)),
        ("a: [!t x]", ("tags",), (1, 5)),
        ("<<: x", ("merge keys",), (1, 1)),
        ("a: {<<: x}", ("merge keys",), (1, 5)),
        ("? a\n: b", ("complex keys",), (1, 1)),
        ("a: 1\n? b\n: c", ("complex keys",), (2, 1)),
        ("a: 1\n---\nb: 2", ("multiple documents",), (2, 1)),
        ("a: 1\n...\n", ("multiple documents",), (2, 1)),
        ("--- a\nb: 1", ("document start",), (1, 5)),
        ("%YAML 1.2\n---\na: 1", ("directives",), (1, 1)),
        ("a:\n\tb: 1", ("tabs",), (2, 1)),
        ("a:\n  b: 1\n\tc: 2", ("tabs",), (3, 1)),
        ("a:\n  - 1\n\t- 2", ("tabs",), (3, 1)),
        ("a: [1,\n  2]", ("same line",), (1, 7)),
        ("a: {b: 1,\n  c: 2}", ("same line",), (1, 10)),
        ("a: [1, 2] x", ("after the flow",), (1, 11)),
        ("a: [[1], 2: 3]", ("single-pair",), None),
        ("a: {[1]: 2}", ("keys",), None),
        ("a: {b}", ("key: value",), None),
        ("a: 1\na: 2", ("duplicate key 'a'",), (2, 1)),
        ("a: {b: 1, b: 2}", ("duplicate key 'b'",), None),
        ("a: b: c", ("`: `",), (1, 5)),
        ("a: echo x: y", ("`: `",), None),
        ("a: x:", ("`: `",), None),
        ("a: @x", ("cannot start with '@'",), (1, 4)),
        ("a: `x`", ("cannot start with",), (1, 4)),
        ("a: - x", ("block sequence",), (1, 4)),
        ('a: "unterminated', ("unterminated",), (1, 4)),
        ("a: 'unterminated", ("unterminated",), (1, 4)),
        (r'a: "bad \q escape"', ("unknown escape",), (1, 9)),
        (r'a: "\xZZ"', ("bad `\\x` escape",), None),
        ('a: "x" y', ("after the closing quote",), None),
        ("a: |3x\n  x", ("block scalar header",), (1, 4)),
        ("a: |-+\n  x", ("block scalar header",), (1, 4)),
        ("a:\n  b: 1\n c: 2", ("indentation",), (3, 2)),
        ("a: 1\n  b: 2", ("`: `",), None),
        ("a: 1\njust text", ("key: value",), (2, 1)),
    ],
)
def test_refusals_name_position_and_reason(text, words, where):
    refused(text, *words, where=where)


def test_error_carries_fields():
    e = refused("a: &x 1", "anchors")
    assert (e.line, e.col, e.filename) == (1, 4, "w.yml")
    assert "not supported" in e.message


def test_refuses_what_looks_like_workflow_features():
    # the YAML tricks workflows sometimes use: all refused, none misparsed
    refused("defaults: &d\n  run: {shell: bash}\njobs:\n  a:\n    <<: *d", "anchors")
    refused("jobs:\n  a:\n    if: !cancelled()", "tags")
    refused("run: echo a: b", "`: `")
    refused("on: push\n---\non: pull_request", "multiple documents")


# ---- property-style round: emit known structures in several styles and parse them back ---------------

WORDS = ["a", "b-c", "on", "yes", "null", "true", "1", "1.5", "x y", "a: b", "a #b", "- x", "[x]", "{y}", "'q'",
         '"d"', "", " lead", "trail ", "é", "tab\tx", "multi\nline", "$X", "${{ a }}", "*", "&", "!", "%", "@", "~",
         "---", "..."]  # fmt: skip


def rand_scalar(r):
    t = r.randrange(6)
    if t == 0:
        return r.randrange(-1000, 1000)
    if t == 1:
        return r.choice([True, False, None])
    if t == 2:
        return r.choice([0.5, -2.25, 1e10, 3.1])
    return r.choice(WORDS)


def rand_value(r, depth=0):
    t = r.randrange(5 if depth < 3 else 2)
    if t <= 1:
        return rand_scalar(r)
    if t == 2:
        return {f"k{r.randrange(100)}_{i}": rand_value(r, depth + 1) for i in range(r.randrange(1, 4))}
    if t == 3:
        return [rand_value(r, depth + 1) for _ in range(r.randrange(1, 4))]
    return rand_scalar(r)


def scalar_text(v):
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON strings are valid double-quoted YAML scalars
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    return repr(v)


def emit(v, indent=0, style="block"):
    pad = " " * indent
    if isinstance(v, dict) and v:
        lines = []
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                body = emit(x, indent + 2, style)
                if isinstance(x, list) and style == "seq-flush":
                    body = emit(x, indent, style)
                lines.append(f"{pad}{k}:\n{body}")
            else:
                lines.append(f"{pad}{k}: {'{}' if x == {} else '[]' if x == [] else scalar_text(x)}")
        return "\n".join(lines)
    if isinstance(v, list) and v:
        lines = []
        for x in v:
            if isinstance(x, (dict, list)) and x:
                body = emit(x, indent + 2, style)
                lines.append(f"{pad}- " + body[indent + 2 :])
            else:
                lines.append(f"{pad}- {'{}' if x == {} else '[]' if x == [] else scalar_text(x)}")
        return "\n".join(lines)
    return pad + scalar_text(v)


def flow(v):
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {flow(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(flow(x) for x in v) + "]"
    return scalar_text(v)


def test_round_trip_block_style():
    r = random.Random(1234)
    for _ in range(300):
        v = {"root": rand_value(r)}
        for style in ("block", "seq-flush"):
            assert parse(emit(v, 0, style)) == v, emit(v, 0, style)


def test_round_trip_flow_style_on_one_line():
    r = random.Random(99)
    for _ in range(300):
        v = {"root": rand_value(r)}
        text = "root: " + flow(v["root"])
        if "\n" in text:  # json.dumps escapes newlines, so this holds; keep the guard explicit
            pytest.fail("flow text spans lines")
        assert parse(text) == v, text


def test_round_trip_block_scalars():
    r = random.Random(7)
    pieces = ["echo a", "  indented", "", "x: y", "# c", "- z", "'q'", "tab\tx"]
    for _ in range(200):
        body = [r.choice(pieces) for _ in range(r.randrange(1, 6))]
        while body and body[-1] == "":
            body.pop()
        if not body or body[0].startswith(" ") or body[0] == "":
            body.insert(0, "start")
        want = "\n".join(body) + "\n"
        text = "k: |\n" + "\n".join(("  " + x) if x else "" for x in body) + "\nnext: 1\n"
        assert parse(text) == {"k": want, "next": 1}, text


# ---- nesting is bounded ------------------------------------------------------------------------------


def test_deep_flow_collections_are_refused_not_crashed():
    for text in ("a: " + "[" * 1000 + "]" * 1000, "a: " + "{k: " * 1000 + "1" + "}" * 1000):
        e = refused(text, "nested more than 64 levels", where=None)
        assert e.line == 1 and e.col > 3
    assert parse("a: " + "[" * 60 + "]" * 60)  # within the limit


def test_deep_block_nesting_is_refused_not_crashed():
    refused("- " * 1000 + "x", "nested more than 64 levels", where=(1, None) if False else None)
    text = "".join(" " * i + "k:\n" for i in range(200)) + " " * 200 + "v: 1\n"
    e = refused(text, "nested more than 64 levels")
    assert e.line == 65
    assert parse("- " * 60 + "x")


def test_recursion_error_safety_net(monkeypatch):
    import loadgov.workflow.yaml_subset as ys

    def boom(self):
        raise RecursionError

    monkeypatch.setattr(ys._Parser, "document", boom)
    refused("a: 1", "too deep")
