import datetime as dt
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "shadow_compare", Path(__file__).parents[2] / "tools" / "shadow_compare.py"
)
sc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sc)

EP = "proj-b"  # the project whose guard logs one PAUSED/RESUMED per episode
GUARD_A = """\
2026-11-10T21:00:00Z start LOW_GB=3 HIGH_GB=6 SWAP_MB=768 avail=13598MB swap=0MB
2026-11-10T21:26:50Z PAUSE implementer 2005132 (277.1 1) 41MB; avail=4663MB swap=4338MB (calm 3462MB)
2026-11-10T21:27:45Z RESUME implementer 2005132; avail=11269MB swap=3658MB
2026-11-10T22:00:00Z HOLD only a compile is large
2026-11-11T01:00:00Z ABORT compile rustc 99 (3000MB); avail=1000MB swap=1MB
"""
GUARD_B = """\
2026-11-10T23:53:35Z STATUS avail=4214MB swap=6256MB load=6.64 mempsi=0.57 paused=1
2026-11-10T23:54:20Z RESUMED avail=10538MB swap=6007MB load=5.07 mempsi=0.05
2026-11-10T23:50:00Z PAUSED avail=1000MB swap=1MB load=1 mempsi=0 containers=[] agents=[1]
"""
REAPER = (
    "2026-11-10 21:30:00 LOW MEMORY: stopping build daemons\n2026-11-10 21:30:00 avail=1MB swap_used=1MB\n"
)
GOV = (
    "2026-11-11T00:08:11Z cap=2 cores busy=23% (room again)\n"
    "2026-11-11T00:09:41Z cap=2.5 cores busy=31% (room again)\n"
)
ALERTS = "ALERT swap 22:37:44 load=8.3 x\nALERT cpu 23:13:16 load=4\nALERT swap 00:10:00 load=1\n"


def audit(op, ts, reason, project=None):
    return json.dumps(
        {"ts": ts, "op": op, "reason": reason, "principal": "system", "result": "would", "project": project}
    )


def test_legacy_parsers():
    mem = sc.parse_legacy_mem((GUARD_A + GUARD_B + REAPER).splitlines())
    assert sorted(k for k, _, _ in mem) == sorted(
        ["PAUSE", "RESUME", "ABORT", "PAUSE episode", "RESUME episode", "REAP"]
    )
    assert [k for k, _, _ in sc.parse_governor(GOV.splitlines())] == ["CAP", "CAP"]


def test_audit_parser_takes_only_would_decisions():
    lines = [
        audit("memguard", "2026-11-10T21:26:52Z", "WOULD PAUSE implementer 5 41MB; avail=1"),
        audit("memguard", "2026-11-10T21:27:50Z", "RESUME x"),
        audit("memguard", "2026-11-10T21:28:00Z", "WOULD HOLD only a compile"),
        audit("governor", "2026-11-11T00:08:12Z", "WOULD cap=2 cores busy=23% (room again)"),
        audit("slots", "2026-11-11T00:08:12Z", "WOULD flock x"),
        "not json",
    ]
    assert [k for k, _, _ in sc.parse_audit(lines, EP)] == ["PAUSE", "RESUME", "CAP"]


def test_alert_dates_rebuilt_backwards_over_midnight():
    ev = sc.parse_alerts(ALERTS.splitlines(), dt.date(2026, 11, 11))
    assert [sc.day_of(t) for _, t, _ in ev] == ["2026-11-10", "2026-11-10", "2026-11-11"]
    assert ev[0][0] == "ALERT swap"


def test_match_and_table_pass_within_window():
    legacy = sc.parse_legacy_mem(GUARD_A.splitlines()) + sc.parse_governor(GOV.splitlines())
    shadow = sc.parse_audit(
        [
            audit("memguard", "2026-11-10T21:26:55Z", "WOULD PAUSE a"),
            audit("memguard", "2026-11-10T21:27:40Z", "WOULD RESUME a"),
            audit("memguard", "2026-11-11T01:00:30Z", "WOULD ABORT b"),
            audit("governor", "2026-11-11T00:08:10Z", "WOULD cap=2 cores busy=23% (room again)"),
            audit("governor", "2026-11-11T00:09:40Z", "WOULD cap=2.5 cores busy=31% (room again)"),
        ]
    )
    rows, mism, ok = sc.compare(legacy, shadow, 120, 0.2, 0)
    assert ok and mism == []
    assert ("2026-11-10", "PAUSE", 1, 1, 1, True) in rows


def test_missing_shadow_events_fail_and_are_listed():
    legacy = sc.parse_legacy_mem((GUARD_A + GUARD_B).splitlines())
    shadow = sc.parse_audit([audit("memguard", "2026-11-10T21:26:55Z", "WOULD PAUSE a")])
    _, mism, ok = sc.compare(legacy, shadow, 120, 0.0, 0)
    assert not ok
    assert {m[0] for m in mism} == {"legacy only"} and len(mism) == 4


def test_tolerance_absolute_and_relative_and_since_filter():
    legacy = [("PAUSE", 1_762_800_000.0 + i * 1000, "") for i in range(10)]
    shadow = legacy[:9]
    assert sc.compare(legacy, shadow, 60, 0.2, 0)[2]
    assert not sc.compare(legacy, shadow, 60, 0.0, 0)[2]
    assert sc.compare(legacy, shadow, 60, 0.0, 1)[2]
    assert sc.compare(legacy, [], 60, 0.0, 0, since="2099-01-01")[0] == []


def test_cli_exit_codes(tmp_path, capsys):
    (tmp_path / "mg.log").write_text(GUARD_A)
    a = tmp_path / "audit.jsonl"
    a.write_text(audit("memguard", "2026-11-10T21:26:55Z", "WOULD PAUSE a") + "\n")
    base = ["--mem-guard-log", str(tmp_path / "mg.log"), "--audit", str(a)]
    assert sc.main(base + ["--tolerance", "0", "--abs-tolerance", "0"]) == 1
    assert "legacy only" in capsys.readouterr().out
    assert sc.main(base + ["--abs-tolerance", "5"]) == 0
    assert sc.main(["--audit", str(tmp_path / "nope")]) == 2
    empty = tmp_path / "e"
    empty.write_text("")
    assert sc.main(["--audit", str(empty)]) == 2


def test_install_script_syntax_and_legacy_copy():
    import subprocess

    script = Path(__file__).parents[2] / "packaging" / "install.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    text = script.read_text()
    assert 'cp -a "$src/legacy/." "$prefix/legacy/"' in text


def test_orphan_reap_is_informational_not_a_failure():
    lines = [
        audit(
            "memguard",
            "2026-11-10T21:30:00Z",
            "WOULD REAP build daemons [5]; avail 1000 MB below 1536 MB; avail=1000MB",
        ),
        audit("memguard", "2026-11-10T21:31:00Z", "WOULD REAP orphaned scope x: its session is gone"),
        audit("memguard", "2026-11-10T21:32:00Z", "REAPED orphaned scope x: gone"),
    ]
    shadow = sc.parse_audit(lines, EP)
    assert [k for k, _, _ in shadow] == ["REAP", "REAP orphan"]
    legacy = sc.parse_legacy_mem(REAPER.splitlines())
    rows, mism, ok = sc.compare(legacy, shadow, 120, 0.0, 0)
    assert ok and any(m[1] == "REAP orphan" for m in mism)


def test_scoped_and_request_reaps_are_informational():
    lines = [
        audit(
            "memguard", "2026-11-10T21:30:00Z", "WOULD REAP build daemons [5]; avail 1000 MB below 1536 MB"
        ),
        audit(
            "memguard",
            "2026-11-10T21:31:00Z",
            "WOULD REAP build daemons [6]; pressure; outside every job scope",
        ),
        audit(
            "memguard",
            "2026-11-10T21:32:00Z",
            "WOULD REAP build daemons [6]; pressure; outside every job scope",
        ),
        audit("reap", "2026-11-10T21:33:00Z", "WOULD REAP build daemons [7]: manual"),
    ]
    shadow = sc.parse_audit(lines, EP)
    assert [k for k, _, _ in shadow] == ["REAP", "REAP scoped", "REAP scoped", "REAP scoped"]
    _, _, ok = sc.compare(sc.parse_legacy_mem(REAPER.splitlines()), shadow, 120, 0.0, 0)
    assert ok


def test_proj_b_pauses_collapse_into_episodes():
    a = "proj-b"
    lines = [
        audit("memguard", "2026-11-10T23:50:00Z", "WOULD PAUSE container a 10MB", a),
        audit("memguard", "2026-11-10T23:50:05Z", "WOULD PAUSE agent 5 10MB", a),
        audit("memguard", "2026-11-10T23:54:00Z", "WOULD RESUME agent 5", a),
        audit("memguard", "2026-11-10T23:54:20Z", "WOULD RESUME container a", a),
        audit("memguard", "2026-11-10T21:26:55Z", "WOULD PAUSE implementer 5 41MB", "proj-a"),
    ]
    shadow = sc.parse_audit(lines, EP)
    assert sorted(k for k, _, _ in shadow) == sorted(["PAUSE episode", "RESUME episode", "PAUSE"])
    legacy = sc.parse_legacy_mem((GUARD_B + GUARD_A).splitlines())
    rows, mism, ok = sc.compare(legacy, shadow, 120, 0.0, 0, until="2026-11-10")
    assert ("2026-11-10", "PAUSE episode", 1, 1, 1, True) in rows
    assert ("2026-11-10", "RESUME episode", 1, 1, 1, True) in rows


def test_reaped_and_released_close_an_open_proj_b_pause():
    a = "proj-b"
    lines = [
        audit("memguard", "2026-11-10T10:00:00Z", "WOULD PAUSE agent 5 10MB", a),
        audit("memguard", "2026-11-10T10:01:00Z", "REAPED orphaned implementer 5 (proj-b): gone", a),
        audit("memguard", "2026-11-10T10:05:00Z", "WOULD PAUSE agent 6 10MB", a),  # a new episode, not nested
        audit("memguard", "2026-11-10T10:06:00Z", "RELEASED agent 6", a),
        audit("memguard", "2026-11-10T10:07:00Z", "RELEASED agent 7", a),  # nothing open: ignored
        audit("memguard", "2026-11-10T10:08:00Z", "REAPED orphaned thing (proj-a): gone", "proj-a"),
    ]
    kinds = [k for k, _, _ in sc.parse_audit(lines, EP)]
    assert kinds == ["PAUSE episode", "PAUSE episode"]


def test_new_reaped_reasons_also_close_an_open_proj_b_pause():
    a = "proj-b"
    for why in (
        "every target is gone or changed",
        "giving up after 10 rounds without a signal sent (10 refused)",
    ):
        lines = [
            audit("memguard", "2026-11-10T10:00:00Z", "WOULD PAUSE agent 5 10MB", a),
            audit("memguard", "2026-11-10T10:01:00Z", f"REAPED orphaned implementer 5 (proj-b): {why}", a),
            audit("memguard", "2026-11-10T10:05:00Z", "WOULD PAUSE agent 6 10MB", a),
        ]
        assert [k for k, _, _ in sc.parse_audit(lines, EP)] == ["PAUSE episode"] * 2
