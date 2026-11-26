"""The expression subset of `if:` and `${{ ... }}`: small, safe, never `eval`.

Grammar (lowest to highest precedence):  `||`  `&&`  `==` `!=`  `!`  primary
primary: 'string' | number | true | false | null | context.path | ( expr ) | success() | failure() |
         always() | cancelled()

Contexts (anything else is refused when the workflow is loaded):
  github.event_name  github.ref  github.ref_name  github.sha  github.workspace
  runner.os          env.NAME    matrix.KEY
  steps.ID.outputs.NAME   steps.ID.outcome   steps.ID.conclusion

Semantics follow Actions: `==` compares strings case-insensitively and other types as numbers; `&&` and
`||` return an operand; null, false, 0 and the empty string are false.
"""

from __future__ import annotations

import math
import re

STATUS_FUNCTIONS = ("success", "failure", "always", "cancelled")
GITHUB_KEYS = ("event_name", "ref", "ref_name", "sha", "workspace")
CONTEXTS = ("github", "runner", "env", "matrix", "steps")


MAX_DEPTH = 64  # nested `!` and parentheses
MAX_TOKENS = 256


class ExprError(ValueError):
    """A refused or malformed expression; the message says what is not supported."""


_TOKEN = re.compile(
    r"""\s*(?:
      (?P<str>'(?:[^']|'')*')
    | (?P<num>-?\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_0-9][A-Za-z0-9_-]*)*)
    | (?P<op>&&|\|\||==|!=|!|\(|\)|,)
    )""",
    re.X,
)


def _tokens(text: str) -> list[tuple[str, str, int]]:
    out, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            bad = text[pos:].lstrip()[:12]
            raise ExprError(f"unsupported expression syntax at {bad!r} (in {text.strip()!r})")
        kind = m.lastgroup
        out.append((kind, m.group(kind), m.start(kind)))
        pos = m.end()
    return out


class _P:
    def __init__(self, text: str):
        self.text, self.toks, self.i = text, _tokens(text), 0
        self.depth = 0
        if len(self.toks) > MAX_TOKENS:
            raise ExprError(f"expression has more than {MAX_TOKENS} tokens")

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None, len(self.text))

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def op(self, *ops) -> str | None:
        k, v, _ = self.peek()
        if k == "op" and v in ops:
            self.i += 1
            return v
        return None

    def parse(self):
        if not self.toks:
            raise ExprError("empty expression")
        n = self.or_()
        if self.i < len(self.toks):
            raise ExprError(f"unexpected {self.peek()[1]!r} in {self.text.strip()!r}")
        return n

    def or_(self):
        n = self.and_()
        while self.op("||"):
            n = ("or", n, self.and_())
        return n

    def and_(self):
        n = self.eq()
        while self.op("&&"):
            n = ("and", n, self.eq())
        return n

    def eq(self):
        n = self.not_()
        while True:
            o = self.op("==", "!=")
            if not o:
                return n
            n = ("eq" if o == "==" else "ne", n, self.not_())

    def deeper(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExprError(f"expression nested more than {MAX_DEPTH} levels")

    def not_(self):
        if self.op("!"):
            self.deeper()
            try:
                return ("not", self.not_())
            finally:
                self.depth -= 1
        return self.primary()

    def primary(self):
        kind, val, _ = self.take()
        if kind == "str":
            return ("lit", val[1:-1].replace("''", "'"))
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "name":
            if self.op("("):
                if val not in STATUS_FUNCTIONS:
                    raise ExprError(f"function {val}() is not supported (only the status functions)")
                if not self.op(")"):
                    raise ExprError(f"{val}() with arguments is not supported")
                return ("call", val)
            if val in ("true", "false"):
                return ("lit", val == "true")
            if val == "null":
                return ("lit", None)
            return ("ref", tuple(val.split(".")))
        if kind == "op" and val == "(":
            self.deeper()
            try:
                n = self.or_()
            finally:
                self.depth -= 1
            if not self.op(")"):
                raise ExprError(f"missing ')' in {self.text.strip()!r}")
            return n
        raise ExprError(
            f"unexpected {val!r} in {self.text.strip()!r}"
            if val
            else f"incomplete expression {self.text.strip()!r}"
        )


def parse_expr(text: str):
    try:
        return _P(text).parse()
    except RecursionError:  # safety net behind MAX_DEPTH
        raise ExprError("expression is nested too deeply") from None


def walk(node):
    yield node
    if node[0] in ("not",):
        yield from walk(node[1])
    elif node[0] in ("and", "or", "eq", "ne"):
        yield from walk(node[1])
        yield from walk(node[2])


def validate(node, *, contexts, status_functions: bool, matrix_keys=None, step_ids=None) -> None:
    """Refuse references outside the supported contexts. `contexts` is the set of context names allowed
    at this place; `matrix_keys` and `step_ids` (when given) are the names that exist."""
    for n in walk(node):
        if n[0] == "call" and not status_functions:
            raise ExprError(f"{n[1]}() is only supported in `if:`")
        if n[0] != "ref":
            continue
        p = n[1]
        name = ".".join(p)
        if p[0] not in CONTEXTS:
            raise ExprError(f"unsupported expression `{name}` (context {p[0]!r} is not supported)")
        if p[0] not in contexts:
            raise ExprError(f"`{name}`: the {p[0]} context is not available here")
        if p[0] == "github" and not (len(p) == 2 and p[1] in GITHUB_KEYS):
            raise ExprError(
                f"unsupported `{name}` (supported: " + ", ".join(f"github.{k}" for k in GITHUB_KEYS) + ")"
            )
        if p[0] == "runner" and p[1:] != ("os",):
            raise ExprError(f"unsupported `{name}` (supported: runner.os)")
        if p[0] == "env" and len(p) != 2:
            raise ExprError(f"unsupported `{name}` (use env.NAME)")
        if p[0] == "matrix":
            if len(p) != 2:
                raise ExprError(f"unsupported `{name}` (use matrix.KEY)")
            if matrix_keys is not None and p[1] not in matrix_keys:
                raise ExprError(f"`{name}`: the job's matrix has no key {p[1]!r}")
        if p[0] == "steps":
            ok = (len(p) == 4 and p[2] == "outputs") or (len(p) == 3 and p[2] in ("outcome", "conclusion"))
            if not ok:
                raise ExprError(
                    f"unsupported `{name}` (use steps.ID.outputs.NAME, steps.ID.outcome or .conclusion)"
                )
            if step_ids is not None and p[1] not in step_ids:
                raise ExprError(f"`{name}`: no earlier step has the id {p[1]!r}")


# ---- evaluation -------------------------------------------------------------------------------------


def truthy(v) -> bool:
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return not (v == 0 or (isinstance(v, float) and math.isnan(v)))
    return True


def _num(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return math.nan


def equal(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if a is None and b is None:
        return True
    return _num(a) == _num(b)


def lookup(path: tuple, ctx: dict):
    cur = ctx
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def evaluate(node, ctx: dict, status: dict | None = None):
    """`status` holds the booleans behind success()/failure()/cancelled() (default: all steps so far ok)."""
    st = status or {"success": True, "failure": False, "cancelled": False}
    k = node[0]
    if k == "lit":
        return node[1]
    if k == "ref":
        return lookup(node[1], ctx)
    if k == "call":
        return True if node[1] == "always" else bool(st.get(node[1], False))
    if k == "not":
        return not truthy(evaluate(node[1], ctx, st))
    if k == "and":
        left = evaluate(node[1], ctx, st)
        return evaluate(node[2], ctx, st) if truthy(left) else left
    if k == "or":
        left = evaluate(node[1], ctx, st)
        return left if truthy(left) else evaluate(node[2], ctx, st)
    a, b = evaluate(node[1], ctx, st), evaluate(node[2], ctx, st)
    return equal(a, b) if k == "eq" else not equal(a, b)


def should_run(text: str | None, ctx: dict, status: dict) -> bool:
    """Whether a step or job with this `if` runs: no `if` means success(); an `if` without a status
    function is `success() && <if>`, as in Actions."""
    if text is None:
        return bool(status.get("success"))
    node = parse_expr(text)
    value = truthy(evaluate(node, ctx, status))
    if any(n[0] == "call" for n in walk(node)):
        return value
    return bool(status.get("success")) and value


def render(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v == int(v) and not math.isinf(v):
        return str(int(v))
    return str(v)


# ---- `${{ }}` in text and `if:` values -------------------------------------------------------------


def spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, inner) of every `${{ ... }}` in text; `}}` inside a 'string' does not end one."""
    out, pos = [], 0
    while True:
        a = text.find("${{", pos)
        if a < 0:
            return out
        j, quote = a + 3, False
        while j < len(text):
            if text[j] == "'":
                quote = not quote
            elif not quote and text.startswith("}}", j):
                break
            j += 1
        if j >= len(text):
            raise ExprError(f"unterminated `${{{{` in {text[:60]!r}")
        out.append((a, j + 2, text[a + 3 : j]))
        pos = j + 2


def check_text(text: str, **kw) -> None:
    """Refuse a string whose `${{ }}` parts are not in the supported subset."""
    for _a, _b, inner in spans(text):
        validate(parse_expr(inner), **kw)


def interpolate(text: str, ctx: dict) -> str:
    out, pos = [], 0
    for a, b, inner in spans(text):
        out.append(text[pos:a])
        out.append(
            render(evaluate(parse_expr(inner), ctx, {"success": True, "failure": False, "cancelled": False}))
        )
        pos = b
    out.append(text[pos:])
    return "".join(out)


def condition(value) -> str:
    """The expression text of an `if:` value: a bool, a bare expression, or exactly one `${{ expr }}`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str) or not value.strip():
        raise ExprError("`if` must be a string expression or a boolean")
    s = value.strip()
    sp = spans(s)
    if sp:
        if len(sp) == 1 and sp[0][0] == 0 and sp[0][1] == len(s):
            return sp[0][2].strip()
        raise ExprError("`if` mixes text with `${{ }}`; use one expression")
    return s
