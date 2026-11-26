"""Load, validate and expand a workflow file (GitHub / Forgejo Actions syntax, supported subset).

Everything outside the subset is refused at load with `file:line:col: path: reason`; nothing is skipped
silently. Host-only differences that do not stop a run are collected in `Workflow.notes`.
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass, field

from loadgov.workflow import expr
from loadgov.workflow.yaml_subset import Located, YamlError, parse_located

WORKFLOW_DIRS = (".github/workflows", ".forgejo/workflows")
SUFFIXES = (".yml", ".yaml")
MAX_FILE_BYTES = 1_000_000
MAX_INSTANCES = 256  # job instances one matrix may expand to
MAX_WORKFLOW_INSTANCES = 1024  # job instances in one workflow
MATRIX_RAW_MAX = 4096  # product of the matrix key lists, checked before anything is expanded
MATRIX_ENTRIES_MAX = 256  # include plus exclude entries
LOCAL_EVENTS = ("push", "pull_request", "workflow_dispatch")
SHELLS = ("bash", "sh", "python")
LANES = ("light", "gate", "compile", "image")
USES = {
    "checkout": "actions/checkout",
    "setup-python": "actions/setup-python",
    "setup-node": "actions/setup-node",
}
_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_UBUNTU = re.compile(r"^ubuntu-[a-z0-9.-]+$")
GENERIC_LABELS = ("self-hosted", "linux", "x64", "arm64", "docker")
LANE_LABELS = {
    "loadgov-light": ("light", None),
    "loadgov-gate": ("gate", None),
    "loadgov-compile": ("compile", None),
    "loadgov-image": ("image", None),
    "loadgov-image-beta": ("image", "beta"),
    "loadgov-image-release": ("image", "release"),
}
IGNORED = {
    "permissions": "no token exists locally",
    "concurrency": "the lanes and slots serialise work instead",
    "run-name": "runs are named by their record id",
    "environment": "no deployment environments locally",
}


class WorkflowError(ValueError):
    """A refused or invalid workflow. `str()` is `file:line:col: path: reason`."""

    def __init__(self, file: str, line: int, col: int, path: str, message: str):
        self.file, self.line, self.col, self.path, self.message = file, line, col, path, message
        super().__init__(f"{file}:{line}:{col}: {path + ': ' if path else ''}{message}")


@dataclass
class Step:
    index: int  # 1-based position in the job
    id: str | None = None
    name: str | None = None
    run: str | None = None
    uses: str | None = None  # checkout | setup-python | setup-node
    uses_ref: str | None = None  # as written, e.g. actions/checkout@v4
    with_: dict = field(default_factory=dict)
    shell: str | None = None
    working_directory: str | None = None
    env: dict = field(default_factory=dict)
    cond: str | None = None  # expression text; None = success()
    continue_on_error: bool | str = False  # bool or expression text
    timeout_minutes: float | None = None
    line: int = 0

    def label(self) -> str:
        return (
            self.name or self.uses_ref or (self.run or "").strip().split("\n")[0][:60] or f"step {self.index}"
        )


@dataclass
class Job:
    id: str
    name: str | None = None
    needs: list[str] = field(default_factory=list)
    runs_on: list[str] = field(default_factory=list)
    cond: str | None = None
    env: dict = field(default_factory=dict)
    timeout_minutes: float | None = None
    continue_on_error: bool | str = False
    matrix: dict | None = None  # raw strategy.matrix
    max_parallel: int | None = None
    shell: str | None = None
    working_directory: str | None = None
    steps: list[Step] = field(default_factory=list)
    line: int = 0


@dataclass
class Instance:
    """One runnable job: a job id plus one matrix combination."""

    job: Job
    matrix: dict
    key: str  # unique within the workflow, file-name safe
    name: str
    lane: str
    channel: str | None
    continue_on_error: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class Workflow:
    path: str
    stem: str
    name: str
    events: list[str]
    env: dict
    shell: str | None
    working_directory: str | None
    jobs: dict[str, Job]
    notes: list[str]
    instances: list[Instance] = field(default_factory=list)
    loc: Located | None = None

    def order(self) -> list[str]:
        """Job ids so that every job comes after the jobs it needs."""
        done: list[str] = []
        seen: set[str] = set()

        def visit(j: str) -> None:
            if j in seen:
                return
            seen.add(j)
            for n in self.jobs[j].needs:
                visit(n)
            done.append(j)

        for j in self.jobs:
            visit(j)
        return done


# ---- discovery ----------------------------------------------------------------------------------------


def discover(root: str) -> list[str]:
    out: list[str] = []
    for d in WORKFLOW_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            out += sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(SUFFIXES))
    return out


# ---- lane mapping ---------------------------------------------------------------------------------------


def map_lane(labels: list[str]) -> tuple[str, str | None, list[str]]:
    """(lane, image channel or None, notes) for the `runs-on` labels. The rule (docs/workflows.md):
    one loadgov-* label picks the lane; none defaults to `gate` (a host-only note); two different ones, a
    macOS/Windows label or an unknown label are refused."""
    notes: list[str] = []
    picked: set[tuple] = set()
    for lab in labels:
        low = lab.strip().lower()
        if low in LANE_LABELS:
            picked.add(LANE_LABELS[low])
        elif low in GENERIC_LABELS or _UBUNTU.match(low):
            continue
        elif low.startswith(("macos", "windows")):
            raise ValueError(f"runs-on {lab!r} has no local equivalent (this host runs Linux only)")
        elif low.startswith("loadgov-"):
            raise ValueError(f"unknown lane label {lab!r} (use loadgov-light, gate, compile or image)")
        else:
            raise ValueError(
                f"runs-on label {lab!r} has no local equivalent "
                "(use a loadgov-* label, ubuntu-latest, self-hosted, linux, x64, arm64 or docker)"
            )
    if len(picked) > 1:
        raise ValueError("runs-on names more than one loadgov lane label")
    if not picked:
        notes.append(f"host-only: runs-on {labels!r} names no loadgov-* label; the job runs in the gate lane")
        return "gate", None, notes
    lane, channel = picked.pop()
    return lane, channel, notes


# ---- loading --------------------------------------------------------------------------------------------


class _Loader:
    def __init__(self, path: str, text: str):
        self.path = path
        try:
            self.loc = parse_located(text, path)
        except YamlError as e:
            raise WorkflowError(path, e.line, e.col, "", e.message) from None
        self.notes: list[str] = []

    def fail(self, at: tuple, msg: str, shown: str | None = None) -> WorkflowError:
        line, col = self.loc.where(*at)
        return WorkflowError(self.path, line, col, shown if shown is not None else _dotted(at), msg)

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def keys(self, d, allowed, at: tuple, what: str, ignored=()) -> None:
        if not isinstance(d, dict):
            raise self.fail(at, f"expected a mapping for {what}")
        for k in d:
            if k in ignored:
                self.note(f"host-only: `{k}` is ignored ({IGNORED.get(k, 'not used locally')})")
            elif k not in allowed:
                raise self.fail((*at, k), f"`{k}` is not supported in {what}")

    def string(self, v, at: tuple, what: str) -> str:
        if not isinstance(v, str):
            raise self.fail(at, f"{what} must be a string")
        return v

    def scalar_text(self, v, at: tuple, what: str) -> str:
        if isinstance(v, (dict, list)) or v is None:
            raise self.fail(at, f"{what} must be a string, number or boolean")
        return expr.render(v) if not isinstance(v, str) else v

    def check(self, text: str, at: tuple, contexts, *, status=False, matrix_keys=None, step_ids=None) -> None:
        try:
            expr.check_text(
                text, contexts=contexts, status_functions=status, matrix_keys=matrix_keys, step_ids=step_ids
            )
        except expr.ExprError as e:
            raise self.fail(at, str(e)) from None

    def env(self, raw, at: tuple, contexts, matrix_keys=None, step_ids=None) -> dict:
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise self.fail(at, "env must be a mapping")
        out = {}
        for k, v in raw.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                raise self.fail((*at, k), f"bad environment variable name {k!r}")
            out[k] = self.scalar_text(v, (*at, k), f"env.{k}")
            self.check(out[k], (*at, k), contexts, matrix_keys=matrix_keys, step_ids=step_ids)
        return out

    def condition(self, v, at: tuple, contexts, matrix_keys, step_ids) -> str:
        try:
            text = expr.condition(v)
            node = expr.parse_expr(text)
            expr.validate(
                node, contexts=contexts, status_functions=True, matrix_keys=matrix_keys, step_ids=step_ids
            )
        except expr.ExprError as e:
            raise self.fail(at, str(e)) from None
        return text

    def minutes(self, v, at: tuple) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise self.fail(at, "timeout-minutes must be a positive number")
        return float(v)

    def coe(self, v, at: tuple, contexts, matrix_keys, step_ids=None) -> bool | str:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            try:
                text = expr.condition(v)
                expr.validate(
                    expr.parse_expr(text),
                    contexts=contexts,
                    status_functions=False,
                    matrix_keys=matrix_keys,
                    step_ids=step_ids,
                )
            except expr.ExprError as e:
                raise self.fail(at, str(e)) from None
            return text
        raise self.fail(at, "continue-on-error must be true, false or one `${{ }}` expression")

    def defaults(self, raw, at: tuple) -> tuple[str | None, str | None]:
        if raw is None:
            return None, None
        self.keys(raw, ("run",), at, "defaults")
        run = raw.get("run")
        if run is None:
            return None, None
        self.keys(run, ("shell", "working-directory"), (*at, "run"), "defaults.run")
        shell = run.get("shell")
        if shell is not None:
            self.shell(shell, (*at, "run", "shell"))
        wd = run.get("working-directory")
        if wd is not None:
            self.check(
                self.string(wd, (*at, "run", "working-directory"), "working-directory"),
                (*at, "run"),
                ("github", "runner"),
            )
        return shell, wd

    def shell(self, v, at: tuple) -> str:
        if v not in SHELLS:
            raise self.fail(
                at,
                f"shell {v!r} is not supported (supported: {', '.join(SHELLS)}; python runs python3)",
            )
        return v

    # ---- sections --------------------------------------------------------------------------------
    def events(self, raw, at: tuple) -> list[str]:
        if isinstance(raw, str):
            names = [raw]
        elif isinstance(raw, list):
            names = [self.string(x, at, "an event name") for x in raw]
        elif isinstance(raw, dict):
            names = list(raw)
        else:
            raise self.fail(at, "`on` must be an event name, a list of them or a mapping")
        for n in names:
            if n == "schedule":
                self.note("host-only: `schedule` is recorded, never triggered; run the workflow on demand")
            elif n not in LOCAL_EVENTS:
                self.note(f"host-only: event `{n}` is never triggered locally")
        return names

    def step(self, raw, i: int, at: tuple, earlier_ids: set, matrix_keys: set) -> Step:
        allowed = (
            "name",
            "id",
            "run",
            "shell",
            "working-directory",
            "env",
            "if",
            "continue-on-error",
            "timeout-minutes",
            "uses",
            "with",
        )
        self.keys(raw, allowed, at, "a step")
        s = Step(index=i, line=self.loc.where(*at)[0])
        ctx = ("github", "runner", "env", "matrix", "steps")
        kw = {"matrix_keys": matrix_keys, "step_ids": earlier_ids}
        if "id" in raw:
            sid = self.string(raw["id"], (*at, "id"), "id")
            if not _ID.match(sid):
                raise self.fail((*at, "id"), f"bad step id {sid!r} (letters, digits, - and _)")
            if sid in earlier_ids:
                raise self.fail((*at, "id"), f"duplicate step id {sid!r}")
            s.id = sid
        if "name" in raw:
            s.name = self.scalar_text(raw["name"], (*at, "name"), "name")
            self.check(s.name, (*at, "name"), ctx, **kw)
        if ("run" in raw) == ("uses" in raw):
            raise self.fail(at, "a step needs exactly one of `run` or `uses`")
        if "run" in raw:
            s.run = self.string(raw["run"], (*at, "run"), "run")
            self.check(s.run, (*at, "run"), ctx, **kw)
        else:
            ref = self.string(raw["uses"], (*at, "uses"), "uses")
            m = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(\S+)", ref)
            kind = next((k for k, a in USES.items() if m and m.group(1) == a), None)
            if kind is None:
                allowed_uses = ", ".join(f"{a}@<ref>" for a in USES.values())
                raise self.fail(
                    (*at, "uses"),
                    f"`uses: {ref}` is not supported (allowed: {allowed_uses}); "
                    "run the equivalent commands with `run:`",
                )
            s.uses, s.uses_ref = kind, ref
            if kind == "checkout":
                self.note(
                    "host-only: actions/checkout is a no-op; the steps run in the existing local checkout"
                )
            else:
                self.note(
                    f"host-only: {USES[kind]} only checks that the tool exists on this host (no download)"
                )
            if "with" in raw:
                w = raw["with"]
                if not isinstance(w, dict):
                    raise self.fail((*at, "with"), "`with` must be a mapping")
                for k, v in w.items():
                    text = self.scalar_text(v, (*at, "with", k), f"with.{k}")
                    self.check(text, (*at, "with", k), ctx, **kw)
                    s.with_[k] = text
                self.note(
                    "host-only: `with:` inputs of allowed actions are ignored except the tool version check"
                )
            if "shell" in raw or "working-directory" in raw:
                raise self.fail(at, "`shell` and `working-directory` apply to `run` steps only")
        if "run" in raw and "with" in raw:
            raise self.fail((*at, "with"), "`with` applies to `uses` steps only")
        if "shell" in raw:
            s.shell = self.shell(raw["shell"], (*at, "shell"))
        if "working-directory" in raw:
            s.working_directory = self.string(
                raw["working-directory"], (*at, "working-directory"), "working-directory"
            )
            self.check(s.working_directory, (*at, "working-directory"), ctx, **kw)
        s.env = self.env(raw.get("env"), (*at, "env"), ctx, matrix_keys, earlier_ids) if "env" in raw else {}
        if "if" in raw:
            s.cond = self.condition(raw["if"], (*at, "if"), ctx, matrix_keys, earlier_ids)
        if "continue-on-error" in raw:
            s.continue_on_error = self.coe(
                raw["continue-on-error"], (*at, "continue-on-error"), ctx, matrix_keys, earlier_ids
            )
        if "timeout-minutes" in raw:
            s.timeout_minutes = self.minutes(raw["timeout-minutes"], (*at, "timeout-minutes"))
        return s

    def strategy(self, raw, at: tuple) -> tuple[dict | None, int | None]:
        self.keys(raw, ("matrix", "fail-fast", "max-parallel"), at, "strategy")
        if "fail-fast" in raw:
            if not isinstance(raw["fail-fast"], bool):
                raise self.fail((*at, "fail-fast"), "fail-fast must be true or false")
            self.note("host-only: `fail-fast` is ignored; every matrix combination runs to its end")
        mp = raw.get("max-parallel")
        if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int) or mp < 1):
            raise self.fail((*at, "max-parallel"), "max-parallel must be a whole number >= 1")
        m = raw.get("matrix")
        if m is None:
            return None, mp
        mat = (*at, "matrix")
        if not isinstance(m, dict):
            raise self.fail(mat, "matrix must be a mapping of key: list (a `${{ }}` matrix is not supported)")
        for k, v in m.items():
            if k in ("include", "exclude"):
                if not isinstance(v, list) or not all(isinstance(x, dict) for x in v):
                    raise self.fail((*mat, k), f"{k} must be a list of mappings")
                for j, x in enumerate(v):
                    for kk, vv in x.items():
                        self.matrix_value(vv, (*mat, k, j, kk))
                continue
            if not _ID.match(k):
                raise self.fail((*mat, k), f"bad matrix key {k!r}")
            if not isinstance(v, list) or not v:
                raise self.fail(
                    (*mat, k), "a matrix key needs a non-empty list (a `${{ }}` list is not supported)"
                )
            for j, x in enumerate(v):
                self.matrix_value(x, (*mat, k, j))
        size = 1
        for k, v in m.items():
            if k not in ("include", "exclude"):
                size *= len(v)
        entries = len(m.get("include", [])) + len(m.get("exclude", []))
        if size + len(m.get("include", [])) > MATRIX_RAW_MAX:
            raise self.fail(
                mat,
                f"the matrix has {size} combinations before include/exclude "
                f"(at most {MATRIX_RAW_MAX} are looked at, and {MAX_INSTANCES} jobs run); split the job",
            )
        if entries > MATRIX_ENTRIES_MAX:
            raise self.fail(mat, f"include and exclude have {entries} entries (at most {MATRIX_ENTRIES_MAX})")
        return m, mp

    def matrix_value(self, v, at: tuple) -> None:
        if isinstance(v, (dict, list)) or v is None:
            raise self.fail(at, "matrix values must be strings, numbers or booleans")
        if isinstance(v, str) and "${{" in v:
            raise self.fail(at, "a `${{ }}` expression in a matrix is not supported")

    def job(self, jid: str, raw, wf_shell, wf_wd) -> Job:
        at = ("jobs", jid)
        if not _ID.match(jid):
            raise self.fail(at, f"bad job id {jid!r} (letters, digits, - and _)")
        for k in ("container", "services", "uses", "secrets", "outputs"):
            if isinstance(raw, dict) and k in raw:
                why = {
                    "container": "containers are not started locally (host-only runs)",
                    "services": "service containers are not started locally",
                    "uses": "reusable workflows are not supported",
                    "secrets": "secrets do not exist locally",
                    "outputs": "job outputs (the needs context) are not supported",
                }[k]
                raise self.fail((*at, k), f"`{k}` is not supported: {why}")
        self.keys(
            raw,
            (
                "name",
                "needs",
                "runs-on",
                "env",
                "if",
                "timeout-minutes",
                "continue-on-error",
                "strategy",
                "steps",
                "defaults",
            ),
            at,
            f"job {jid!r}",
            ignored=tuple(IGNORED),
        )
        j = Job(id=jid, line=self.loc.where(*at)[0])
        if "runs-on" not in raw:
            raise self.fail(at, "a job needs `runs-on`")
        ro = raw["runs-on"]
        if isinstance(ro, str):
            labels = [ro]
        elif isinstance(ro, list) and ro and all(isinstance(x, str) for x in ro):
            labels = list(ro)
        else:
            raise self.fail((*at, "runs-on"), "runs-on must be a label or a list of labels")
        for lab in labels:
            self.check(lab, (*at, "runs-on"), ("matrix", "github"))
        j.runs_on = labels
        needs = raw.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list) or not all(isinstance(x, str) for x in needs):
            raise self.fail((*at, "needs"), "needs must be a job id or a list of job ids")
        j.needs = list(dict.fromkeys(needs))
        if "strategy" in raw:
            j.matrix, j.max_parallel = self.strategy(raw["strategy"], (*at, "strategy"))
        mkeys = matrix_keys(j.matrix)
        jctx = ("github", "runner", "env", "matrix")
        if "name" in raw:
            j.name = self.scalar_text(raw["name"], (*at, "name"), "name")
            self.check(j.name, (*at, "name"), ("github", "matrix"), matrix_keys=mkeys)
        j.env = self.env(raw.get("env"), (*at, "env"), jctx, mkeys) if "env" in raw else {}
        if "if" in raw:
            j.cond = self.condition(
                raw["if"], (*at, "if"), ("github", "env", "matrix", "runner"), mkeys, None
            )
        if "timeout-minutes" in raw:
            j.timeout_minutes = self.minutes(raw["timeout-minutes"], (*at, "timeout-minutes"))
        if "continue-on-error" in raw:
            j.continue_on_error = self.coe(
                raw["continue-on-error"], (*at, "continue-on-error"), ("github", "matrix"), mkeys
            )
        j.shell, j.working_directory = self.defaults(raw.get("defaults"), (*at, "defaults"))
        j.shell = j.shell or wf_shell
        j.working_directory = j.working_directory or wf_wd
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            raise self.fail((*at, "steps"), "a job needs a non-empty `steps` list")
        ids: set[str] = set()
        for i, rs in enumerate(steps):
            st = self.step(rs, i + 1, (*at, "steps", i), set(ids), mkeys)
            if st.id:
                ids.add(st.id)
            j.steps.append(st)
        return j


def _dotted(at: tuple) -> str:
    out = ""
    for p in at:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


def matrix_keys(m: dict | None) -> set[str]:
    keys: set[str] = set()
    for k, v in (m or {}).items():
        if k == "include":
            for x in v:
                keys |= set(x)
        elif k != "exclude":
            keys.add(k)
    return keys


def expand_matrix(m: dict | None) -> list[dict]:
    """The combinations of strategy.matrix with Actions' include/exclude rules; `[{}]` without a matrix."""
    if m is None:
        return [{}]
    base = {k: v for k, v in m.items() if k not in ("include", "exclude")}
    combos = (
        [dict(zip(base, vals, strict=True)) for vals in itertools.product(*base.values())] if base else []
    )
    for ex in m.get("exclude", []):  # a partial match excludes
        combos = [c for c in combos if not all(k in c and c[k] == v for k, v in ex.items())]
    originals = [dict(c) for c in combos]
    for inc in m.get("include", []):
        hit = False
        for c, orig in zip(combos[: len(originals)], originals, strict=True):
            if all(orig[k] == v for k, v in inc.items() if k in base):
                hit = True
                for k, v in inc.items():
                    if k not in base:
                        c[k] = v
        if not hit:
            combos.append(dict(inc))
    return combos


def _suffix(combo: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_.]+", "-", "-".join(expr.render(v) for v in combo.values())).strip("-.")


def _expand(ld: _Loader, wf: Workflow) -> None:
    keys: set[str] = set()
    total = 0
    for jid in wf.order():
        job = wf.jobs[jid]
        combos = expand_matrix(job.matrix)
        if len(combos) > MAX_INSTANCES or (job.matrix is not None and not combos):
            raise ld.fail(
                ("jobs", jid, "strategy"),
                f"the matrix expands to {len(combos)} jobs (1 to {MAX_INSTANCES} allowed)",
            )
        total += len(combos)
        if total > MAX_WORKFLOW_INSTANCES:
            raise ld.fail(
                ("jobs", jid, "strategy"),
                f"the workflow expands to more than {MAX_WORKFLOW_INSTANCES} job instances",
            )
        for n, combo in enumerate(combos):
            ctx = {"matrix": combo, "github": {}}
            try:
                labels = [expr.interpolate(x, ctx) for x in job.runs_on]
                lane, channel, notes = map_lane(labels)
            except (ValueError, expr.ExprError) as e:
                raise ld.fail(("jobs", jid, "runs-on"), str(e)) from None
            for note in notes:
                ld.note(note)
            suf = _suffix(combo) if job.matrix is not None else ""
            key = f"{jid}-{suf}" if suf else jid
            base, extra = key, n + 1
            while key in keys:  # another job or combination already has this key
                key = f"{base}-{extra}"
                extra += 1
            keys.add(key)
            name = expr.interpolate(job.name, ctx) if job.name else jid
            if job.matrix is not None and not job.name and combo:
                name = f"{jid} ({', '.join(expr.render(v) for v in combo.values())})"
            coe = job.continue_on_error
            if isinstance(coe, str):
                coe = expr.truthy(expr.evaluate(expr.parse_expr(coe), ctx))
            wf.instances.append(Instance(job, combo, key, name, lane, channel, bool(coe)))


def _check_needs(ld: _Loader, wf: Workflow) -> None:
    for j in wf.jobs.values():
        for n in j.needs:
            if n not in wf.jobs:
                raise ld.fail(("jobs", j.id, "needs"), f"job {j.id!r} needs unknown job {n!r}")
    state: dict[str, int] = {}

    def visit(jid: str, trail: list[str]) -> None:
        if state.get(jid) == 2:
            return
        if state.get(jid) == 1:
            cycle = [*trail[trail.index(jid) :], jid]
            raise ld.fail(("jobs", jid, "needs"), "dependency cycle: " + " -> ".join(cycle))
        state[jid] = 1
        for n in wf.jobs[jid].needs:
            visit(n, [*trail, jid])
        state[jid] = 2

    for jid in wf.jobs:
        visit(jid, [])


def parse_workflow(text: str, path: str = "workflow.yml") -> Workflow:
    try:
        return _parse_workflow(text, path)
    except RecursionError:  # safety net behind the depth limits
        raise WorkflowError(path, 1, 1, "", "the file nests too deeply to read") from None


def _parse_workflow(text: str, path: str) -> Workflow:
    ld = _Loader(path, text)
    raw = ld.loc.value
    if not isinstance(raw, dict):
        raise ld.fail((), "a workflow must be a mapping with `on` and `jobs`", "")
    ld.keys(raw, ("name", "on", "env", "defaults", "jobs"), (), "a workflow", ignored=tuple(IGNORED))
    if "on" not in raw:
        raise ld.fail((), "a workflow needs `on`", "")
    events = ld.events(raw["on"], ("on",))
    stem = os.path.splitext(os.path.basename(path))[0]
    name = ld.scalar_text(raw["name"], ("name",), "name") if "name" in raw else stem
    env = ld.env(raw.get("env"), ("env",), ("github", "runner")) if "env" in raw else {}
    shell, wd = ld.defaults(raw.get("defaults"), ("defaults",))
    jobs_raw = raw.get("jobs")
    if not isinstance(jobs_raw, dict) or not jobs_raw:
        raise ld.fail(("jobs",), "a workflow needs a non-empty `jobs` mapping")
    wf = Workflow(path, stem, name, events, env, shell, wd, {}, ld.notes, loc=ld.loc)
    for jid, jr in jobs_raw.items():
        wf.jobs[jid] = ld.job(jid, jr, shell, wd)
    _check_needs(ld, wf)
    _expand(ld, wf)
    return wf


def load_workflow(path: str) -> Workflow:
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise WorkflowError(path, 1, 1, "", f"file is larger than {MAX_FILE_BYTES} bytes")
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise WorkflowError(path, 1, 1, "", f"cannot read: {e}") from None
    return parse_workflow(text, path)
