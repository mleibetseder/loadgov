"""SIGHUP reload: the handler only sets a flag; the main loop applies the new config (no host access)."""

import asyncio
import fcntl
import json
import os
import signal

import pytest

from loadgov.config import changed_keys, load_config, parse_config
from loadgov.daemon import main as daemon_main
from loadgov.daemon.main import Daemon
from loadgov.engines.slots import SlotManager
from loadgov.host.procfs import HostFS


@pytest.fixture
def rig(tmp_path):
    cfg = tmp_path / "loadgov.toml"
    cfg.write_text(
        "[headroom.kinds.gate]\nmem_min_gb = 6\n[slots]\nenforce = true\n[memguard]\nenforce = true\n"
    )
    (tmp_path / "proc").mkdir()
    (tmp_path / "lock").mkdir()
    fs = HostFS(
        proc_root=str(tmp_path / "proc"), cgroup_root=str(tmp_path / "cg"), lock_root=str(tmp_path / "lock")
    )
    d = Daemon(
        fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
        config=load_config(str(cfg)), config_path=str(cfg),
    )  # fmt: skip
    return d, cfg


def audits(d):
    return [json.loads(line) for line in open(d.audit.path)] if os.path.exists(d.audit.path) else []


def test_valid_reload_applies_thresholds_and_enforce_flags(rig):
    d, cfg = rig
    cfg.write_text(
        "[headroom.kinds.gate]\nmem_min_gb = 8\n[headroom]\npsi_busy_gate_pct = 70\n[slots]\nenforce = false\n[memguard]\nenforce = false\n"
    )
    d.reload_config()
    assert d.config.headroom.kinds == {"gate": {"mem_min_gb": 8}}
    assert d.sampler.psi_busy_gate_pct == 70
    assert d.slots.enforce is False
    assert d.memguard.cfg.enforce is d.memguard.signals.enforce is d.memguard.docker.enforce is False
    row = audits(d)[-1]
    assert row["op"] == "reload"
    assert "headroom.kinds.gate.mem_min_gb" in row["reason"] and "slots_enforce" in row["reason"]


def test_invalid_reload_keeps_the_running_config(rig):
    d, cfg = rig
    old = d.config
    cfg.write_text('[headroom]\nswap_max_mb = "lots"\n')
    d.reload_config()
    assert d.config is old and d.slots.enforce is True
    row = audits(d)[-1]
    assert row["op"] == "reload refused" and row["reason"]
    cfg.write_text("not [toml")
    d.reload_config()
    assert d.config is old and audits(d)[-1]["op"] == "reload refused"


def test_restart_only_keys_are_reported_and_not_applied(rig):
    d, cfg = rig
    cfg.write_text(
        "[headroom.kinds.gate]\nmem_min_gb = 6\n[slots]\nenforce = true\n[memguard]\nenforce = true\n"
        "[rest]\nport = 9999\n"
    )
    d.reload_config()
    assert d.config.rest == parse_config("").rest
    rows = audits(d)
    assert [r["op"] for r in rows] == ["needs restart"]
    assert "rest.port" in rows[0]["reason"]


def test_reload_off_backstop_is_applied_so_the_tick_rolls_back(rig):
    d, cfg = rig
    cfg.write_text("[backstop]\nenabled = true\nmemory_max_mb = 8192\nmemory_swap_max_mb = 0\n")
    d.reload_config()
    assert d.backstop.cfg.enabled is True
    cfg.write_text("")
    d.reload_config()
    assert d.backstop.cfg.enabled is False and d.config.backstop.enabled is False


def test_changed_keys_names_dotted_paths():
    a, b = parse_config(""), parse_config("[headroom]\npsi_busy_gate_pct = 5\n[governor]\nenforce = true\n")
    assert changed_keys(a, b) == ["governor_enforce", "headroom.psi_busy_gate_pct"]
    assert changed_keys(a, a) == []


def test_sighup_handler_only_sets_the_flag_and_the_loop_reloads(rig, monkeypatch, tmp_path):
    d, cfg = rig
    handlers = {}

    class Loop:
        def add_signal_handler(self, sig, fn):
            handlers[sig] = fn

    reloads = []
    monkeypatch.setattr(d, "reload_config", lambda: reloads.append(1) or setattr(d, "_reload", False))
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(signal, "signal", lambda *a: None)

    async def go():
        ticks = []
        d.tick = lambda: ticks.append(1) or ({} if len(ticks) < 2 else d._stop.set() or {})
        d.fast_tick = lambda: None
        d.sampler.prime = lambda: None
        d.tick_s = 0.01
        d.run_dir = str(tmp_path / "run")
        d.socket_path = str(tmp_path / "run" / "s.sock")
        d.compat_link = None
        task = asyncio.create_task(d.run())
        for _ in range(200):
            if signal.SIGHUP in handlers:
                break
            await asyncio.sleep(0.01)
        if task.done():
            task.result()
        handlers[signal.SIGHUP]()
        assert reloads == []  # nothing ran inside the handler
        assert d._reload is True
        await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert reloads == [1]


def test_check_config(tmp_path, capsys):
    good, bad = tmp_path / "g.toml", tmp_path / "b.toml"
    good.write_text("[headroom]\npsi_busy_gate_pct = 5\n")
    bad.write_text("[headroom]\npsi_busy_gate_pct = 'x'\n")
    assert daemon_main.main(["--check-config", str(good)]) == 0
    assert daemon_main.main(["--check-config", str(bad)]) == 1
    assert "bad config" in capsys.readouterr().err
    binary = tmp_path / "n.toml"
    binary.write_bytes(b"\xff\xfe\x00bad")
    assert daemon_main.main(["--check-config", str(binary)]) == 1
    assert "bad config" in capsys.readouterr().err


# ---- slot holds follow `slots.enforce` like a restart, memguard.projects is restart-only -------------


def _mgr(tmp_path, enforce):
    """A SlotManager on a fake proc tree and lock root (no real /proc)."""
    proc = tmp_path / "fakeproc"
    proc.mkdir(exist_ok=True)
    (proc / "locks").write_text("")
    fs = HostFS(proc_root=str(proc), lock_root=str(tmp_path))
    return SlotManager(
        fs, enforce=enforce, self_pid=4242, stat_reader=lambda pid: f"{pid} (x) S 1 " + "0 " * 17 + "10 0\n"
    )


def _locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def test_enforce_on_to_off_releases_the_real_lock_and_the_hold_continues_as_would(tmp_path):
    m = _mgr(tmp_path, True)
    lock = str(tmp_path / "host-heavy-slot.lock")
    m.hold_on(["heavy"], "owner hold")
    assert _locked(lock)
    m.drain_events()
    m.set_enforce(False)
    assert not _locked(lock)  # the daemon no longer keeps the real flock
    assert m.summary()["heavy"]["holder"]  # the hold continues in WOULD mode
    assert any(e["result"] == "would" for e in m.drain_events())


def test_reload_audits_the_slot_events_of_an_enforce_change_at_once(rig):
    d, cfg = rig
    d.slots.hold_on(["heavy"], "owner hold")
    d.slots.drain_events()
    cfg.write_text("[slots]\nenforce = false\n[memguard]\nenforce = true\n")
    d.reload_config()
    ops = [(r["op"], r["result"]) for r in audits(d)]
    assert ("slot_release", "released") in ops and ("slot_lock", "would") in ops
    assert d.slots.events == []


def test_enforce_off_to_on_takes_the_real_lock_for_the_hold(tmp_path):
    m = _mgr(tmp_path, False)
    lock = str(tmp_path / "host-heavy-slot.lock")
    m.hold_on(["heavy"], "owner hold")
    assert not _locked(lock)
    m.set_enforce(True)
    assert _locked(lock)
    assert m.summary()["heavy"]["holder"]
    m.hold_off(["heavy"])
    assert not _locked(lock)


def test_enforce_off_to_on_with_a_legacy_holder_leaves_the_hold_pending(tmp_path):
    m = _mgr(tmp_path, False)
    lock = str(tmp_path / "host-heavy-slot.lock")
    m.hold_on(["heavy"], "owner hold")
    legacy = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        m.set_enforce(True)
        assert m.slots["heavy"].holder is None and m.slots["heavy"].hold  # pending, not claimed
    finally:
        os.close(legacy)
    m.tick()
    assert _locked(lock) and m.slots["heavy"].holder is not None


def test_missing_or_non_utf8_config_is_refused(rig):
    d, cfg = rig
    old = d.config
    cfg.write_bytes(b"\xff\xfe\x00bad")
    d.reload_config()
    assert d.config is old and audits(d)[-1]["op"] == "reload refused"
    cfg.unlink()
    d.reload_config()
    assert d.config is old and audits(d)[-1]["op"] == "reload refused"


def test_memguard_projects_are_restart_only(rig):
    d, cfg = rig
    cfg.write_text("[memguard]\nenforce = true\n[memguard.projects.proj-a]\nlow_mb = 1000\nhigh_mb = 4000\n")
    d.reload_config()
    assert d.config.memguard.projects == {} and d.memguard.engine.projects == {}
    row = audits(d)[-1]
    assert row["op"] == "needs restart" and "memguard.projects.proj-a" in row["reason"]


def test_reload_audit_rows_survive_a_failing_after_change(rig, monkeypatch):
    d, cfg = rig
    cfg.write_text(
        "[headroom.kinds.gate]\nmem_min_gb = 7\n[slots]\nenforce = true\n[memguard]\nenforce = true\n"
        "[rest]\nport = 9999\n"
    )

    def boom(ctx):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(d, "_after_change", boom)
    d.reload_config()  # must not raise
    assert d.config.headroom.kinds["gate"]["mem_min_gb"] == 7
    assert [r["op"] for r in audits(d)] == ["reload", "needs restart"]


def test_reload_applies_audit_rotation_settings(rig):
    d, cfg = rig
    cfg.write_text("[audit]\nmax_mb = 7\nkeep = 2\n")
    d.reload_config()
    assert (d.audit.max_mb, d.audit.keep) == (7, 2)


def test_reload_applies_the_disk_levels_live_and_a_bad_value_keeps_the_old(rig):
    d, cfg = rig
    assert d.sampler.disk_busy.value == d.watch.disk_busy.value == 25.0
    cfg.write_text('[levels]\ndisk_busy = "80%"\ndisk_limit = 8\n')
    d.reload_config()
    for part in (d.sampler, d.watch):
        assert (part.disk_busy.value, part.disk_busy.pct) == (80.0, True)
        assert (part.disk_limit.value, part.disk_limit.pct) == (8.0, False)
    reloads = [r for r in audits(d) if r["op"] == "reload"]
    assert "levels.disk_busy.value" in reloads[-1]["reason"]
    assert "levels.disk_limit.value" in reloads[-1]["reason"]
    cfg.write_text('[levels]\ndisk_busy = "120%"\n')
    d.reload_config()
    assert audits(d)[-1]["op"] == "reload refused"
    assert (d.sampler.disk_busy.value, d.watch.disk_busy.value) == (80.0, 80.0)
