"""The restriction invariant, checked over random operation sequences and named scenarios."""

import random
import tempfile

import pytest

from loadgov.config import BUILTIN_PROFILES, PARAMS, RANGES, loosens
from loadgov.daemon.state import StateStore
from loadgov.engines.limits import AGENT, Ceiling, Limits, LimitState, Override


class Clock:
    t = 1_762_800_000.0

    def __call__(self):
        return self.t


def make(state=None):
    clock = Clock()
    return Limits(dict(BUILTIN_PROFILES), state or LimitState("balanced"), clock), clock


def check_invariant(lim):
    if lim.restriction() is None:
        return
    base, eff = lim.baseline(), lim.effective()
    for o in lim.state.overrides:
        assert o.principal != AGENT or not loosens(o.param, o.value, base[o.param]), o
    for k in PARAMS:
        assert not loosens(k, eff[k], base[k]), (k, eff[k], base[k])


def would_loosen(lim, changes, profile):
    targets = {k: getattr(lim.profiles[profile], k) for k in PARAMS} if profile else {}
    targets.update(changes)
    eff, base = lim.effective(), lim.baseline()
    return any(v != eff[k] and loosens(k, v, base[k]) for k, v in targets.items())


def random_op(rng, lim, clock, store=None):
    kind = rng.choice(
        [
            "owner_set",
            "agent_set",
            "agent_set",
            "ceiling_set",
            "ceiling_clear",
            "flag",
            "reset",
            "reset",
            "time",
            "roundtrip",
        ]
    )
    ttl = rng.choice([None, None, 30, 600, 3600, 50_000])
    profile = rng.choice([None, None, "silent", "balanced", "boost", "drain"])
    changes = {p: _value(rng, p) for p in rng.sample(PARAMS, rng.randint(0, 2))}
    if kind in ("owner_set", "agent_set") and not changes and not profile:
        changes = {"max_cores": _value(rng, "max_cores")}
    if kind == "owner_set":
        lim.set("owner", changes, "r", ttl_s=ttl, profile=profile)
    elif kind == "agent_set":
        restricted = lim.restriction() is not None
        expect_refused = restricted and would_loosen(lim, changes, profile)
        r = lim.set("agent", changes, "r", ttl_s=ttl, profile=profile)
        if expect_refused:
            assert r.get("refused") is True, (changes, profile)
        elif restricted:
            assert r.get("ok") is True and not r.get("refused"), (
                changes,
                profile,
                r,
            )  # tightenings stay allowed
    elif kind == "ceiling_set":
        lim.ceiling_set(rng.choice(["silent", "balanced", "boost", "drain"]), "r", ttl_s=ttl)
    elif kind == "ceiling_clear":
        lim.ceiling_clear()
    elif kind == "flag":
        lim.set_flag(rng.choice(["hold", "freeze"]), rng.choice([None, "r"]))
    elif kind == "roundtrip" and store is not None:  # a daemon restart: save, load, carry on
        store.save(lim.state)
        known = set(lim.profiles)
        lim.state = store.load("balanced", known)
        lim._commit(lim.effective())
    elif kind == "reset":
        if rng.random() < 0.3:
            lim.reset("r", all_=True)
        else:
            lim.reset("r", rng.sample(PARAMS, rng.randint(1, 3)))
    else:
        clock.t += rng.choice([1, 31, 700, 4000, 60_000])
        lim.tick()


def _value(rng, param):
    lo, hi, step = RANGES[param]
    v = rng.choice([lo, (lo + hi) / 2 // step * step, hi])  # few values: collisions find the edge cases
    return int(v) if param == "max_implementers" else float(v)


def test_random_sequences_never_leave_an_agent_looser_than_the_owner_baseline():
    with tempfile.TemporaryDirectory() as d:
        store = StateStore(d)
        for seed in range(300):
            rng = random.Random(seed)
            lim, clock = make()
            for _ in range(40):
                random_op(rng, lim, clock, store)
                check_invariant(lim)
                lim.drain_events()


def test_reset_of_an_owner_override_cancels_agent_value_above_the_new_baseline():
    lim, _ = make()
    lim.set("owner", {"max_cores": 5.5}, "more")
    lim.set("owner", {"load_max": 2.0}, "quiet")  # restriction
    assert lim.set("agent", {"max_cores": 5.0}, "build")["ok"]  # tighter than 5.5
    lim.drain_events()
    lim.reset("done", ["max_cores"])  # baseline max_cores is now 3.5
    assert lim.restriction() is not None
    assert lim.effective()["max_cores"] == 3.5
    assert [e["result"] for e in lim.drain_events()] == ["cancelled", "accepted"]


def test_loaded_state_with_a_restriction_cancels_looser_agent_overrides():
    st = LimitState(
        "balanced",
        overrides=[Override("o1", AGENT, "max_cores", 5.0, "build", 0.0, None, 3.5, None)],
        ceiling=Ceiling("silent", "movie", "owner", 0.0, None),
        next_id=2,
    )
    lim, _ = make(st)
    assert st.overrides == [] and lim.effective()["max_cores"] == 2.0
    assert [e["result"] for e in lim.drain_events()] == ["cancelled"]


@pytest.mark.parametrize("flag", ["hold", "freeze"])
def test_flag_clear_and_set_go_through_the_same_rule(flag):
    lim, _ = make()
    lim.set("agent", {"max_cores": 5.0}, "build")
    lim.set_flag(flag, "night mode")
    assert lim.state.overrides == []
