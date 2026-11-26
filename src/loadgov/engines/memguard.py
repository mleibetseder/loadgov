"""Memory/swap guard: the decisions of the two legacy memory guards in one pure engine.

Pure: it reads nothing and signals nothing. The daemon (`daemon/memguard_ops.py`) hands in the latest
sample and two lazy lists (candidate trees, JVM daemons), executes the returned actions and reports
failures back with `drop`. The clocks are injectable.

Thresholds (global rule): LOW = the effective `ram_floor_gb`, RESUME (HIGH) = max(6 GB, floor + 2 GB)
(HIGH is always at least 2 GB above LOW, so the level rules never pause right after a resume; the trend
rule does when avail falls fast),
CRIT = 1500 MB. A project in `[memguard.projects]` (config only, e.g. 2048 / 4096 MB and swap > 2048 MB)
has its own rule for its own work.

Every sample (a daemon tick, 5 s):
- pressure = avail < LOW, or swap grew more than 768 MB over the calm baseline, or (trend) avail minus the
  fall since the last sample times `horizon_samples` (2) is below LOW, or (swap headroom) free swap
  (SwapTotal - used) is below `swap_free_min_mb` (1024) while avail < HIGH (global rule; the project
  rule: avail < its LOW, or swap above its limit while avail < its HIGH);
- deficit cover: under pressure with avail falling by at least 512 MB since the last sample, one sample
  pauses several eligible trees (largest first, the same exclusions) until their memory covers LOW minus
  the projected avail, and the quiet period below does not hold the guard while the fall goes on;
- under pressure, in this order: reap JVM build daemons outside every job scope; else pause the
  largest eligible tree of the first class that has one (registered job scopes by `memory.current`, then
  legacy implementer trees, then project containers), skipping trees whose largest rustc is >= 2048 MB
  (a stopped rustc keeps its memory); only compiling trees left and avail < 1500 MB: abort (SIGTERM) the
  largest rustc, never anything else; above 1500 MB: HOLD;
- the abort is also taken in the same sample as the system reap (avail < 1536 MB) when nothing else can be
  paused, and when avail fell by at least 512 MB since the last sample so that the same fall again would
  cross 1500 MB before the next sample (a compile of several GB kills the host faster than 5 s);
- one action per sample (except the deficit cover); 15 s of quiet after a pause or abort, 30 s after a
  resume;
- LIFO resume after `resume_samples` (3) samples with avail >= HIGH and no pressure;
- the swap baseline only moves down, or follows swap after 120 calm samples (10 min), and is reset to the
  current swap by a pause, an abort and a resume.
Guard pauses are `system` principal; human freezes are entries of kind `human` that no rule resumes.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from loadgov.config import DENY_CONTAINERS, GuardProject
from loadgov.engines.jvm import REAP_ALL_MB, Jvm
from loadgov.engines.limits import iso

CRIT_MB = 1500
FALL_MIN_MB = 512  # a fall of at least this much since the last sample counts as a fast fall
FALL_WINDOW_S = 15.0  # the last sample is no older than this
SAMPLE_S = 5.0  # the next evaluation (the daemon tick) is this far away
COMPILE_MB = 2048
HIGH_MIN_MB = 6144
SWAP_SURGE_MB = 768
RESUME_SAMPLES = 3
CALM_RESET = 120
PAUSE_QUIET_S = 15.0
RESUME_QUIET_S = 30.0
HOLD_EVERY = 12
LOW_MEMORY_EVERY_S = 300.0
REAPED_KEEP_S = 60.0
FLOOR_RANGE = (2.0, 6.0)
CLASS_ORDER = {"scope": 0, "legacy": 1, "container": 2}
SWAP_FREE_MIN_MB = 1024  # [memguard] swap_free_min_mb
HORIZON_SAMPLES = 2  # [memguard] horizon_samples


def denied(name: str, globs: tuple[str, ...] = DENY_CONTAINERS) -> bool:
    """Containers that are never paused, whatever a project's regex says ([memguard] deny_containers)."""
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def thresholds(floor_gb: float) -> tuple[int, int]:
    """(LOW, HIGH) in MB for an effective `ram_floor_gb`; the floor is clamped to its valid 2.0-6.0."""
    floor = min(max(float(floor_gb), FLOOR_RANGE[0]), FLOOR_RANGE[1])
    low = round(floor * 1024)
    high = max(HIGH_MIN_MB, low + 2048)
    assert low < high
    return low, high


@dataclass(frozen=True)
class Rule:
    name: str
    low_mb: int
    high_mb: int
    surge_mb: int | None  # swap growth over the calm baseline that counts as pressure
    swap_mb: int | None  # absolute swap that counts as pressure while avail < high_mb
    resume_samples: int
    calm_reset: int
    swap_free_min_mb: int = 0  # free swap below this while avail < high_mb counts as pressure (0 = off)
    horizon: int = 0  # avail - fall per sample * horizon below low_mb counts as pressure (0 = off)

    def pressure(
        self, avail: int, swap: int, baseline: int, swap_free: int | None = None, fall: int | None = None
    ) -> str | None:
        if avail < self.low_mb:
            return f"avail {avail} MB below {self.low_mb} MB"
        if self.horizon and fall is not None and fall > 0 and avail - fall * self.horizon < self.low_mb:
            return (f"avail {avail} MB falling {fall} MB per sample, {avail - fall * self.horizon} MB "
                    f"projected within {self.horizon} samples, below {self.low_mb} MB")  # fmt: skip
        if self.surge_mb is not None and swap - baseline > self.surge_mb:
            return f"swap grew {swap - baseline} MB over the calm {baseline} MB"
        if self.swap_mb is not None and swap > self.swap_mb and avail < self.high_mb:
            return f"swap {swap} MB above {self.swap_mb} MB with avail below {self.high_mb} MB"
        if (
            self.swap_free_min_mb and swap_free is not None and swap_free < self.swap_free_min_mb
            and avail < self.high_mb
        ):  # fmt: skip
            return (f"swap free {swap_free} MB below {self.swap_free_min_mb} MB with avail below "
                    f"{self.high_mb} MB")  # fmt: skip
        return None

    def projected(self, avail: int, fall: int | None) -> int | None:
        """avail `horizon` samples from now at the current fall (None when not falling or off)."""
        if not self.horizon or fall is None or fall <= 0:
            return None
        return avail - fall * self.horizon

    def calm(self, avail: int) -> bool:
        return avail >= self.high_mb


@dataclass
class Tree:
    """One pausable candidate, described by the collector."""

    cls: str  # "scope" | "legacy" | "container"
    key: str
    how: str  # "freeze" | "sigstop" | "docker"
    what: str
    project: str | None
    rss_mb: int
    targets: list[dict]  # {"cgroup"} | {"pid", "start"} | {"name"}
    session: dict | None = None  # {"pid", "start"} of the owning agent session / `loadctl run`
    big_rustc_mb: int = 0
    rustc: tuple[int, int] | None = None  # (pid, start) of the largest rustc


@dataclass
class Entry:
    """A paused tree in the registry (persisted when `real`)."""

    id: str
    kind: str  # "guard" (system) | "human"
    rule: str | None
    key: str
    how: str
    what: str
    project: str | None
    targets: list[dict]
    session: dict | None
    since: float
    reason: str
    principal: str
    real: bool = True
    expires: float | None = None
    term_at: float | None = None  # orphan reaper: when SIGTERM went out
    killed: bool = False  # orphan reaper: SIGKILL was sent
    reap_tries: int = 0  # orphan reaper: SIGTERM rounds in which no target was signalled


@dataclass
class Action:
    op: str  # "pause" | "resume" | "abort" | "reap"
    text: str
    would: bool
    entry: Entry | None = None
    tree: Tree | None = None
    jvm: list[Jvm] = field(default_factory=list)
    released: bool = False  # a resume of a real pause while `enforce` is off


@dataclass
class RuleState:
    baseline: int | None = None
    good: int = 0


class MemGuard:
    def __init__(
        self,
        clock: Callable[[], float],
        monotonic: Callable[[], float],
        projects: dict[str, GuardProject] | None = None,
        override: tuple[int, int] | None = None,  # test-only: (LOW, HIGH) MB replace the global rule
        deny_containers: tuple[str, ...] = DENY_CONTAINERS,
        swap_free_min_mb: int = SWAP_FREE_MIN_MB,
        horizon_samples: int = HORIZON_SAMPLES,
    ):
        self.clock, self.monotonic = clock, monotonic
        self.deny = tuple(deny_containers)
        self.swap_free_min_mb, self.horizon_samples = swap_free_min_mb, horizon_samples
        self.swap_total: int | None = None
        self._fall: int | None = None  # avail fall since the last sample (MB), whatever its age
        self._swap_free: int | None = None  # SwapTotal - used of the last sample (None: unknown or no swap)
        self.projects = projects or {}
        self.override = override
        self.paused: list[Entry] = []
        self.human: dict | None = None  # {"reason", "since", "expires", "project"} of the freeze flag
        self.events: list[dict] = []
        self.state: dict[str, RuleState] = {}
        self.rule_map: dict[str, Rule] = {}
        self.cooldown_until = 0.0
        self.reaped: dict[tuple[int, int], float] = {}
        self.avail = self.swap = None
        self.floor = 3.0
        self.next_id = 1
        self.last: dict | None = None  # {"decision", "reason", "at"}
        self._hold_n = 0
        self._low_memory_at: float | None = None
        self._prev: tuple[float, int] | None = None  # (monotonic, avail) of the last sample
        self._projected: int | None = None  # avail one sample from now at the current fall, if falling fast

    # ---- rules -----------------------------------------------------------------------------------

    def rules(self, floor_gb: float) -> dict[str, Rule]:
        low, high = self.override or thresholds(floor_gb)
        out = {"global": Rule("global", low, high, SWAP_SURGE_MB, None, RESUME_SAMPLES, CALM_RESET,
                              self.swap_free_min_mb, self.horizon_samples)}  # fmt: skip
        for name, p in self.projects.items():
            out[name] = Rule(name, p.low_mb, p.high_mb, None, p.swap_mb, p.resume_samples, CALM_RESET)
        return out

    def rule_name(self, project: str | None) -> str:
        return project if project in self.projects else "global"

    # ---- driving ---------------------------------------------------------------------------------

    def step(
        self,
        avail_mb: int,
        swap_mb: int,
        floor_gb: float,
        enforce: bool,
        trees: Callable[[], list[Tree]],
        jvm: Callable[[], list[Jvm]],
        swap_total_mb: int | None = None,
    ) -> list[Action]:
        mono = self.monotonic()
        self._projected = None
        prev = self._prev
        self._fall = prev[1] - avail_mb if prev else None
        if prev and 0 < mono - prev[0] <= FALL_WINDOW_S and prev[1] - avail_mb >= FALL_MIN_MB:
            rate = (prev[1] - avail_mb) / (mono - prev[0])
            self._projected = round(avail_mb - rate * SAMPLE_S)
        self._prev = (mono, avail_mb)
        self.avail, self.swap, self.floor = avail_mb, swap_mb, floor_gb
        self.swap_total = swap_total_mb
        swap_free = swap_total_mb - swap_mb if swap_total_mb else None  # no swap configured: no headroom rule
        self._swap_free = swap_free
        fast = self._fall is not None and self._fall >= FALL_MIN_MB
        self.rule_map = self.rules(floor_gb)
        self.reaped = {k: t for k, t in self.reaped.items() if mono - t < REAPED_KEEP_S}
        actions: list[Action] = []
        jvms: list[Jvm] | None = None

        def fresh_jvms() -> list[Jvm]:
            nonlocal jvms
            if jvms is None:
                jvms = [j for j in jvm() if j.key not in self.reaped]
            return jvms

        if avail_mb < REAP_ALL_MB and fresh_jvms():  # the system backstop, whatever the quiet period
            actions.append(self._reap(fresh_jvms(), enforce, f"avail {avail_mb} MB below {REAP_ALL_MB} MB"))
        quiet = mono < self.cooldown_until
        if quiet and not fast:  # while avail keeps falling fast, the quiet period does not hold the guard
            return actions
        acted, pressured = bool(actions), False
        reaped_only, abort_done = bool(actions), False  # after the system reap only an abort may follow
        for name, rule in self.rule_map.items():
            st = self.state.setdefault(name, RuleState())
            if st.baseline is None:
                st.baseline = swap_mb
            why = rule.pressure(avail_mb, swap_mb, st.baseline, swap_free, self._fall)
            if quiet and not why:
                continue  # inside the quiet period only pressure acts: no resume, no baseline move
            if why:
                pressured = True
                st.good = 0
                if not acted or (reaped_only and not abort_done):
                    got = self._pressure(rule, st, why, enforce, trees, fresh_jvms, reaped_only)
                    actions += got
                    acted = acted or bool(got)
                    abort_done = any(a.op == "abort" for a in got)
            elif rule.calm(avail_mb):
                st.good += 1
                mine = [e for e in self.paused if e.kind == "guard" and e.rule == name]
                if not mine:
                    if swap_mb < st.baseline or st.good >= rule.calm_reset:
                        st.baseline = swap_mb
                elif st.good >= rule.resume_samples and not acted:
                    actions.append(self._resume(mine[-1], st, enforce))
                    acted = True
            else:
                st.good = 0
        if not pressured:
            self._hold_n = 0
        return actions

    def _line(self, kind: str, text: str, would: bool, project: str | None = None, **extra) -> None:
        self.events.append(
            {"kind": kind, "text": text, "would": would, "project": project, "at": self.clock(), **extra}
        )
        self.last = {"decision": kind, "reason": text, "at": self.clock()}

    def _tail(self) -> str:
        return f"avail={self.avail}MB swap={self.swap}MB"

    def _reap(self, targets: list[Jvm], enforce: bool, why: str) -> Action:
        now = self.monotonic()
        for j in targets:
            self.reaped[j.key] = now
        pids = ",".join(str(j.pid) for j in targets)
        text = f"REAP build daemons {pids}; {why}; {self._tail()}"
        self._line("REAP", text, not enforce, reap=[j.pid for j in targets])
        return Action("reap", text, not enforce, jvm=targets)

    def _critical(self) -> str | None:
        """Why memory is critical: below CRIT, or falling so fast that it crosses CRIT by the next sample."""
        if self.avail < CRIT_MB:
            return f"below {CRIT_MB}MB"
        if self._projected is not None and self._projected < CRIT_MB:
            return f"falling fast, {self._projected}MB projected below {CRIT_MB}MB within {SAMPLE_S:g}s"
        return None

    def _deficit(self, rule: Rule) -> int | None:
        """MB to take out of the race when avail falls fast (>= FALL_MIN_MB since the last sample): LOW minus
        the projected avail (at least 1, so the first pick always counts); None when not falling fast."""
        if self._fall is None or self._fall < FALL_MIN_MB:
            return None
        projected = rule.projected(self.avail, self._fall)
        if projected is None:
            projected = self.avail - self._fall  # horizon off: one more sample at the same fall
        return max(1, rule.low_mb - projected)

    def _pressure(self, rule, st, why, enforce, trees, jvms, reaped=False) -> list[Action]:
        mono = self.monotonic()
        out = [] if reaped else [j for j in jvms() if not j.in_scope]  # daemons outside every job scope first
        if out:
            return [self._reap(out, enforce, f"{why}; outside every job scope")]
        held = {e.key for e in self.paused}
        mine = [
            t for t in trees()
            if self.rule_name(t.project) == rule.name and t.key not in held
            and not (t.cls == "container" and denied(t.key, self.deny))
        ]  # fmt: skip
        pausable = sorted(
            (t for t in mine if t.big_rustc_mb < COMPILE_MB),
            key=lambda t: (CLASS_ORDER.get(t.cls, 9), -t.rss_mb, t.key),
        )
        compiling = [t for t in mine if t.big_rustc_mb >= COMPILE_MB and t.rustc]
        if pausable and not reaped:
            picks, deficit = [pausable[0]], self._deficit(rule)
            if deficit is not None:  # falling fast: pause until the trees' memory covers the deficit
                why = f"{why}; deficit {deficit} MB"
                for t in pausable[1:]:
                    if sum(p.rss_mb for p in picks) >= deficit:
                        break
                    picks.append(t)
            calm, acts = st.baseline, []
            for t in picks:
                e = Entry(
                    f"g{self.next_id}", "guard", rule.name, t.key, t.how, t.what, t.project, t.targets,
                    t.session, self.clock(), why, "system", real=enforce,
                )  # fmt: skip
                self.next_id += 1
                self.paused.append(e)
                text = f"PAUSE {t.what} {t.rss_mb}MB; {self._tail()} (calm {calm}MB); {why}"
                self._line("PAUSE", text, not enforce, t.project, entry=e.id)
                acts.append(Action("pause", text, not enforce, entry=e))
            st.baseline = self.swap  # swap does not drain by itself: measure growth from here
            self.cooldown_until = mono + PAUSE_QUIET_S
            return acts
        if pausable:  # the system reap took this sample; the pause waits for the next one
            return []
        if compiling:
            crit = self._critical()
            if crit:
                t = max(compiling, key=lambda c: c.big_rustc_mb)
                text = (
                    f"ABORT compile rustc {t.rustc[0]} ({t.big_rustc_mb}MB) of {t.what}; {self._tail()}"
                    f" - {crit}, nothing else to pause"
                )
                st.baseline = self.swap
                self.cooldown_until = mono + PAUSE_QUIET_S
                self._line(
                    "ABORT", text, not enforce, t.project, **({"unit": t.key} if t.cls == "scope" else {})
                )
                return [Action("abort", text, not enforce, tree=t)]
            if reaped:
                return []
            if self._hold_n % HOLD_EVERY == 0:
                names = ", ".join(t.what for t in compiling)
                self._line("HOLD", f"HOLD only a compile is large ({names}); not pausing it; {self._tail()}",
                           not enforce)  # fmt: skip
            self._hold_n += 1
            return []
        if (
            not reaped
            and self.avail < CRIT_MB
            and (self._low_memory_at is None or mono - self._low_memory_at >= LOW_MEMORY_EVERY_S)
        ):
            self._low_memory_at = mono
            self._line("LOW_MEMORY", f"LOW_MEMORY nothing left to pause or abort; {self._tail()}; {why}",
                       not enforce)  # fmt: skip
        return []

    def _resume(self, e: Entry, st: RuleState, enforce: bool) -> Action:
        self.paused.remove(e)
        st.good = 0
        st.baseline = self.swap
        self.cooldown_until = self.monotonic() + RESUME_QUIET_S
        released = e.real and not enforce
        word = "RELEASED" if released else "RESUME"
        text = f"{word} {e.what}; {self._tail()}"
        self._line(word, text, not e.real, e.project, entry=e.id)
        return Action("resume", text, not e.real, entry=e, released=released)

    # ---- registry --------------------------------------------------------------------------------

    def add_human(self, t: Tree, reason: str, expires: float | None, enforce: bool) -> Entry:
        e = Entry(
            f"h{self.next_id}", "human", None, t.key, t.how, t.what, t.project, t.targets, t.session,
            self.clock(), reason, "owner", real=enforce, expires=expires,
        )  # fmt: skip
        self.next_id += 1
        self.paused.append(e)
        return e

    def drop(self, entry_id: str) -> Entry | None:
        """Remove an entry (a pause that failed, a tree that is gone, a released human freeze)."""
        for e in self.paused:
            if e.id == entry_id:
                self.paused.remove(e)
                return e
        return None

    def note(self, kind: str, text: str, would: bool, project: str | None = None, **extra) -> None:
        self._line(kind, text, would, project, **extra)

    def may_release(self, e: Entry) -> bool:
        """The owner may unfreeze a guard pause only once its own resume condition holds."""
        rule = self.rule_map.get(e.rule or "")
        st = self.state.get(e.rule or "")
        if rule is None or st is None or self.avail is None or st.baseline is None:
            return False
        why = rule.pressure(self.avail, self.swap, st.baseline, self._swap_free, self._fall)
        return rule.calm(self.avail) and why is None  # the same rules as a sample, the trend included

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    # ---- persistence and status ------------------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "next_id": self.next_id,
            "paused": [asdict(e) for e in self.paused if e.real],
            "human": self.human,
        }

    def restore(self, raw) -> list[Entry]:
        """Load saved entries (unreadable ones are skipped); the daemon verifies them and ADOPTs."""
        if not isinstance(raw, dict):
            return []
        self.next_id = raw.get("next_id", 1) if isinstance(raw.get("next_id"), int) else 1
        self.human = raw.get("human") if isinstance(raw.get("human"), dict) else None
        out = []
        for item in raw.get("paused") or []:
            try:
                e = Entry(**item)
            except TypeError:
                continue
            e.real = True
            out.append(e)
        self.paused = list(out)
        return out

    def thresholds_view(self) -> tuple[int, int]:
        g = self.rule_map.get("global") or self.rules(self.floor)["global"]
        return g.low_mb, g.high_mb

    def status(self, enforce: bool) -> dict:
        low, high = self.thresholds_view()
        st = self.state.get("global")
        left = max(0.0, self.cooldown_until - self.monotonic())
        last = self.last
        return {
            "enforce": enforce,
            "low_mb": low,
            "resume_mb": high,
            "crit_mb": CRIT_MB,
            "baseline_swap_mb": st.baseline if st and st.baseline is not None else 0,
            "paused": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "how": e.how,
                    "what": e.what,
                    "project": e.project,
                    "since": iso(e.since),
                    "reason": e.reason,
                    "principal": e.principal,
                    "real": e.real,
                }
                for e in self.paused
            ],  # fmt: skip
            "last_decision": last["decision"] if last else None,
            "last_reason": last["reason"] if last else None,
            "last_at": iso(last["at"]) if last else None,
            "cooldown_s": round(left, 1),
        }
