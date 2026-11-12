"""Alert engine: pure evaluation with an injected clock, no host."""

import pytest

from loadgov.engines.watch import LegacyTee, Watch, interval_top3, legacy_line

T0 = 1_762_800_000.0
TARGET = 65


class Clock:
    t = T0

    def __call__(self):
        return self.t


def sample(avail=12000, swap=0, disk=48.0, slots=None, heavy_stuck=False):
    free = {"state": "free", "holder": None, "held_since": None, "stuck": False}
    return {
        "memory": {"avail_mb": avail},
        "swap": {"used_mb": swap},
        "disk": {"free_gb": disk},
        "slots": slots or {"heavy": dict(free), "compile": dict(free)},
        "heavy_slot": {"stuck": heavy_stuck},
    }


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def watch(clock):
    return Watch(clock)


def ev(watch, clock, busy=10, s=None, step=30, top3=()):
    clock.t += step
    return watch.evaluate(s or sample(), busy, TARGET, "balanced", None, list(top3))


def kinds(events):
    return [(e["type"], e["key"]) for e in events]


def test_three_busy_evaluations_alert_once_dedupe_and_resolve_once(watch, clock):
    out = []
    for busy in (82, 83, 75):
        out += ev(watch, clock, busy)
    assert kinds(out) == [("alert", "cpu")]
    a = out[0]
    assert a["level"] == "warning" and a["actions"] == ["profile=silent", "freeze"]
    assert a["profile"] == "balanced" and a["ceiling"] is None and a["avail_mb"] == 12000
    assert set(a) >= {"title", "message", "top3", "swap_mb", "at"}
    for busy in (90, 90, 90, 90):  # still busy, inside the 10-minute window
        assert ev(watch, clock, busy) == []
    assert [e["key"] for e in watch.summary()["active"]] == ["cpu"]
    assert ev(watch, clock, 20) == []
    assert kinds(ev(watch, clock, 20)) == [("resolved", "cpu")]
    assert ev(watch, clock, 20) == []  # resolved once
    assert watch.summary()["active"] == []


def test_cpu_needs_three_in_a_row_and_uses_the_effective_target(watch, clock):
    assert ev(watch, clock, 82) == [] and ev(watch, clock, 83) == []
    assert ev(watch, clock, 60) == []  # the run is broken
    assert ev(watch, clock, 71) == [] and ev(watch, clock, 71) == []
    assert ev(watch, clock, 70) == []  # 70 is not above target 65 + 5
    clock.t += 30
    assert watch.evaluate(sample(), 61, 50, "silent", "silent", []) == []
    assert watch.evaluate(sample(), 61, 50, "silent", "silent", []) == []
    third = watch.evaluate(sample(), 61, 50, "silent", "silent", [])
    assert third[0]["ceiling"] == "silent" and third[0]["profile"] == "silent"


def test_alert_repeats_after_ten_minutes_while_the_condition_holds(watch, clock):
    out = []
    for _ in range(3):
        out += ev(watch, clock, 90)
    assert len(out) == 1
    assert ev(watch, clock, 90, step=570) == []
    assert kinds(ev(watch, clock, 90, step=30)) == [("alert", "cpu")]


def test_memory_below_3000_alerts_once(watch, clock):
    out = ev(watch, clock, s=sample(avail=2900))
    assert kinds(out) == [("alert", "memory")] and out[0]["level"] == "critical"
    assert ev(watch, clock, s=sample(avail=2800)) == []
    assert ev(watch, clock, s=sample(avail=3000)) == []
    assert kinds(ev(watch, clock, s=sample(avail=3000))) == [("resolved", "memory")]
    assert watch.summary()["active"] == []


def test_swap_growth_over_a_calm_baseline(watch, clock):
    ev(watch, clock, s=sample(avail=9000, swap=500))  # baseline 500
    assert ev(watch, clock, s=sample(avail=9000, swap=1200)) == []  # growth 700 and calm: no alert
    assert ev(watch, clock, s=sample(avail=5000, swap=1200)) == []  # not above 768
    out = ev(watch, clock, s=sample(avail=5000, swap=1300))  # growth 800, avail < 6000
    assert kinds(out) == [("alert", "swap")] and out[0]["level"] == "warning"


def calm(watch, clock, n, swap):
    for _ in range(n):
        ev(watch, clock, s=sample(avail=9000, swap=swap))


def test_swap_baseline_only_moves_down(watch, clock):
    calm(watch, clock, 1, 2000)
    calm(watch, clock, 1, 900)  # lower: baseline 900
    calm(watch, clock, 1, 1500)  # higher while calm: baseline stays 900
    assert kinds(ev(watch, clock, s=sample(avail=5000, swap=1700))) == [("alert", "swap")]  # growth 800


@pytest.mark.parametrize(("calm_evals", "alerts"), [(19, True), (20, False)])
def test_swap_baseline_resets_after_20_calm_evaluations(watch, clock, calm_evals, alerts):
    calm(watch, clock, 1, 900)  # baseline 900 (calm evaluation 1)
    calm(watch, clock, calm_evals - 1, 1500)
    out = ev(watch, clock, s=sample(avail=5000, swap=2200))  # growth 1300, or 700 after the reset to 1500
    assert bool(out) is alerts


def test_non_calm_evaluation_restarts_the_calm_count(watch, clock):
    calm(watch, clock, 1, 900)
    calm(watch, clock, 15, 1500)
    ev(watch, clock, s=sample(avail=5000, swap=1500))  # not calm: the count restarts
    calm(watch, clock, 15, 1500)  # 15 calm since: still no reset, baseline stays 900
    assert kinds(ev(watch, clock, s=sample(avail=5000, swap=2200))) == [("alert", "swap")]


def test_baseline_keeps_following_swap_once_calm_is_reached(watch, clock):
    calm(watch, clock, 1, 900)
    calm(watch, clock, 19, 1500)  # the 20th calm evaluation: baseline follows to 1500
    calm(watch, clock, 1, 3000)  # and keeps following while calm
    assert ev(watch, clock, s=sample(avail=5000, swap=3700)) == []  # growth 700 over 3000


def mem(avail):
    return sample(avail=avail)


def test_one_clear_evaluation_is_not_a_resolve_and_the_dedupe_still_holds(watch, clock):
    assert kinds(ev(watch, clock, s=mem(2900))) == [("alert", "memory")]
    assert ev(watch, clock, s=mem(3000)) == []  # clear once: nothing sent, still active
    assert [a["key"] for a in watch.summary()["active"]] == ["memory"]
    assert ev(watch, clock, s=mem(2800)) == []  # back: no resolved and no new alert


def test_two_clear_evaluations_resolve_and_a_return_alerts_at_once(watch, clock):
    ev(watch, clock, s=mem(2900))
    assert ev(watch, clock, s=mem(3000)) == []
    assert kinds(ev(watch, clock, s=mem(3000))) == [("resolved", "memory")]
    assert kinds(ev(watch, clock, s=mem(2800))) == [("alert", "memory")]  # inside the old 10 minutes


def test_a_flapping_condition_does_not_spam(watch, clock):
    out = []
    for i in range(20):  # 10 minutes of alternating bad/good
        out += ev(watch, clock, s=mem(2900 if i % 2 == 0 else 3100))
    assert kinds(out) == [("alert", "memory")]


def test_level_flapping_sends_one_alert_plus_one_real_escalation(watch, clock):
    out = []
    for free in (20, 9, 20, 9, 20, 9, 20, 9):
        out += ev(watch, clock, s=sample(disk=free))
    assert [(e["key"], e["level"]) for e in out] == [("disk", "warning"), ("disk", "critical")]


def test_stuck_holder_alert_carries_cancel_action_and_ignores_holds(watch, clock):
    stuck = {"state": "held", "holder": "proj-a gate pid=7 since=x", "held_since": "2026-11-10T09:00:00Z",
             "stuck": True}  # fmt: skip
    slots = {"heavy": stuck, "compile": {**stuck, "state": "held-by-hold"}}
    out = ev(watch, clock, s=sample(slots=slots))
    assert kinds(out) == [("alert", "stuck_holder:heavy")]
    assert out[0]["actions"] == ["cancel_holder"] and "proj-a gate" in out[0]["message"]
    assert ev(watch, clock, s=sample()) == []
    assert kinds(ev(watch, clock, s=sample())) == [("resolved", "stuck_holder:heavy")]


def test_stuck_via_heavy_slot_status_for_a_legacy_holder(watch, clock):
    legacy = {"state": "held", "holder": None, "held_since": None, "stuck": False}
    free = {"state": "free", "holder": None, "held_since": None, "stuck": False}
    out = ev(watch, clock, s=sample(slots={"heavy": legacy, "compile": free}, heavy_stuck=True))
    assert kinds(out) == [("alert", "stuck_holder:heavy")]


def test_disk_busy_then_limit_escalates_immediately(watch, clock):
    out = ev(watch, clock, s=sample(disk=20))
    assert kinds(out) == [("alert", "disk")] and out[0]["level"] == "warning"
    assert out[0]["actions"] == ["prune_caches"]
    assert ev(watch, clock, s=sample(disk=18)) == []
    out = ev(watch, clock, s=sample(disk=9.5))
    assert kinds(out) == [("alert", "disk")] and out[0]["level"] == "critical"
    assert ev(watch, clock, s=sample(disk=40)) == []
    assert kinds(ev(watch, clock, s=sample(disk=40))) == [("resolved", "disk")]
    assert ev(watch, clock, s=sample(disk=25)) == []  # 25 GB is not below the BUSY limit


def test_interval_top3_names_the_process_that_used_cpu_in_the_interval():
    prev = {1: (10, 900_000, "service", 3000), 2: (20, 100, "rustc", 50)}
    cur = {
        1: (10, 900_300, "service", 3000),  # 3 s in 30 s: 10 %
        2: (20, 2_100, "rustc", 4000),  # 20 s in 30 s: 66.7 %
        3: (990, 600, "cc1", 100),  # new: 6 s -> 20 %
        4: (30, 5, "idle", 1),
    }
    top = interval_top3(prev, cur, 30.0, 100)
    assert [t["comm"] for t in top] == ["rustc", "cc1", "service"]
    assert top[0] == {"pid": 2, "comm": "rustc", "pcpu": 66.7, "rss_mb": 4000}
    reused = interval_top3({2: (21, 99_999, "old", 1)}, {2: (22, 500, "new", 1)}, 10.0, 100)
    assert reused[0]["comm"] == "new" and reused[0]["pcpu"] == 50.0  # a reused pid starts from zero


def test_emit_event_hook_shape(watch, clock):
    watch.evaluate(sample(avail=5000, swap=300), 10, TARGET, "balanced", "silent", [])
    e = watch.emit_event("pause", {"message": "paused implementer 4242", "top3": [{"pid": 4242}]})
    assert e["type"] == "alert" and e["key"] == "pause" and e["level"] == "warning"
    assert e["actions"] == ["unfreeze"] and e["message"] == "paused implementer 4242"
    assert e["avail_mb"] == 5000 and e["swap_mb"] == 300 and e["ceiling"] == "silent"
    assert e["top3"] == [{"pid": 4242}]
    assert watch.emit_event("abort")["level"] == "critical"
    assert watch.emit_event("low_memory")["key"] == "low_memory"
    assert watch.emit_event("pause")["actions"] == ["unfreeze"]  # not deduped
    with pytest.raises(ValueError):
        watch.emit_event("nonsense")
    assert [x["key"] for x in watch.summary()["events"]][-1] == "pause"


def test_only_the_last_20_events_are_kept(watch, clock):
    for _ in range(30):
        watch.emit_event("low_memory")
    assert len(watch.summary()["events"]) == 20


def test_legacy_tee_conditions_and_line_format(clock):
    tee = LegacyTee(clock)
    assert tee.evaluate(71, 9000, 100) == [] and tee.evaluate(71, 9000, 100) == []
    assert tee.evaluate(71, 9000, 100) == ["cpu"]
    assert tee.evaluate(99, 2000, 2100) == ["memory", "swap"]
    assert tee.evaluate(99, 2000, 2100) == []  # 10-minute dedupe per condition
    clock.t += 600
    assert tee.evaluate(99, 2000, 2100) == ["cpu", "memory", "swap"]
    assert LegacyTee(clock).evaluate(70, 9000, 2049) == []  # swap needs avail < 6000 too
    top = [
        {"pid": 1, "comm": "rustc", "pcpu": 97.14, "rss_mb": 6800},
        {"pid": 2, "comm": "node", "pcpu": 3.0, "rss_mb": 900},
    ]
    line = legacy_line("cpu", T0, 5.4, 48.0, 7000, 120, 82.6, top)
    assert line == (
        "ALERT cpu 18:40:00 load=5.40 cpu_some10=48.00 mem_avail_mb=7000 swap_used_mb=120 busy=82% "
        "top: rustc(97.1%,6800MB) node(3.0%,900MB) "
    )
