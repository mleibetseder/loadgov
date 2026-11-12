"""Site values are configuration, not code: principal rules, lock name, pause marker, no built-in projects."""

import json
import os

import pytest
from conftest import parity_config

from loadgov.config import Config, ConfigError, PauseMarker, PrincipalConfig, parse_config
from loadgov.daemon import recover
from loadgov.daemon.principal import owner_denial
from loadgov.engines import slots
from loadgov.engines.headroom import paused_reasons


def proc_with(root, pid, cgroup, cmdline, ppid=1):
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


@pytest.fixture
def fs(tmp_path):
    from loadgov.host.procfs import HostFS

    root = tmp_path / "proc"
    proc_with(root, 1, "/init.scope", "init")
    proc_with(root, 10, "/user.slice/s.scope", "bash /opt/x/dispatch-1.sh run")
    proc_with(root, 11, "/user.slice/s.scope", "loadctl", 10)
    proc_with(root, 20, "/legacy-loop/x", "claude")
    proc_with(root, 30, "/build-work.slice/a.scope", "claude")
    proc_with(root, 40, "/user.slice/s.scope", "loadctl")
    return HostFS(proc_root=str(root), cgroup_root=str(tmp_path / "cg"), lock_root=str(tmp_path / "lock"))


def test_no_built_in_site_rules_by_default():
    c = Config()
    assert c.principal == PrincipalConfig((), ())
    assert c.memguard.projects == {} and c.headroom.projects == {} and c.rest.bind is None
    assert c.compat.mirror_cgroups == () and c.compile_lock is None


def test_owner_denial_defaults_deny_only_build_work_and_config_adds_more(fs):
    assert owner_denial(fs, 30) is not None  # build-work*: always refused
    assert owner_denial(fs, 40) is None and owner_denial(fs, 20) is None and owner_denial(fs, 11) is None
    cfg = PrincipalConfig(ancestor_deny=(r"dispatch-[^/ ]*\.sh",), deny_cgroups=("/legacy-loop",))
    assert "governed cgroup" in owner_denial(fs, 20, cfg)
    assert "dispatcher ancestor" in owner_denial(fs, 11, cfg)
    assert owner_denial(fs, 40, cfg) is None


def test_principal_config_validation():
    c = parse_config("[principal]\ndeny_cgroups = ['/a']\nancestor_deny = ['x+', 'y']")
    assert c.principal == PrincipalConfig(("x+", "y"), ("/a",))
    for bad in ("x = 1", "deny_cgroups = 'a'", "deny_cgroups = ['no-slash']", "ancestor_deny = ['(']",
                "ancestor_deny = [1]"):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_config(f"[principal]\n{bad}")


@pytest.fixture
def restore_slots():
    saved = dict(slots.SLOT_FILES)
    yield
    slots.SLOT_FILES.update(saved)


def test_compile_lock_comes_from_config_then_environment_then_a_generic_default(monkeypatch, restore_slots):
    monkeypatch.delenv("LOADGOV_COMPILE_LOCK", raising=False)
    slots.set_compile_lock()
    assert slots.SLOT_FILES["compile"] == ("host-compile-slot.lock", "host-compile-slot.holder")
    monkeypatch.setenv("LOADGOV_COMPILE_LOCK", "env-slot.lock")
    slots.set_compile_lock()
    assert slots.SLOT_FILES["compile"] == ("env-slot.lock", "env-slot.holder")
    slots.set_compile_lock("cfg-slot.lock")  # the config wins over the environment
    assert slots.SLOT_FILES["compile"] == ("cfg-slot.lock", "cfg-slot.holder")
    assert slots.SLOT_FILES["heavy"] == ("host-heavy-slot.lock", "host-heavy-slot.holder")  # generic, fixed
    for bad in ("../x.lock", "x", "a b.lock", "/abs.lock"):
        with pytest.raises(ValueError):
            slots.set_compile_lock(bad)
        with pytest.raises(ConfigError):
            parse_config(f'[slots]\ncompile_lock = "{bad}"')
    assert parse_config('[slots]\ncompile_lock = "c.lock"').compile_lock == "c.lock"


def test_pause_marker_is_read_only_when_configured(fs):
    os.makedirs(fs.lock_root, exist_ok=True)
    with open(os.path.join(fs.lock_root, "guard.paused"), "w") as f:
        f.write("paused\n")
    assert paused_reasons(fs, "impl", None, None) == []  # no marker configured: the file means nothing
    assert paused_reasons(fs, "impl", None, None, "guard.paused")[0].startswith("host guard pause file")


def test_recover_removes_the_configured_marker_only(fs, tmp_path):
    os.makedirs(fs.lock_root, exist_ok=True)
    marker_path = os.path.join(fs.lock_root, "guard.paused")
    state = tmp_path / "state"
    state.mkdir()
    entry = {"what": "x", "how": "sigstop", "kind": "legacy", "project": "proj", "targets": [], "since": "t"}
    (state / "state.json").write_text(json.dumps({"memguard": {"paused": [entry]}}))
    for marker, removed in ((None, False), (PauseMarker("guard.paused", "other"), False),
                            (PauseMarker("guard.paused", "proj"), True)):  # fmt: skip
        open(marker_path, "w").close()
        (state / "state.json").write_text(json.dumps({"memguard": {"paused": [entry]}}))
        rc = recover.run(fs, str(state), str(tmp_path / "no.sock"), True, euid=lambda: 0, marker=marker)
        assert rc == 0 and os.path.exists(marker_path) is not removed, marker
    assert parity_config().memguard.pause_marker.file == "proj-b-guard.paused"


def test_a_removed_compat_key_gets_a_targeted_error():
    with pytest.raises(ConfigError, match="mirror_cgroups"):
        parse_config("[compat]\nsync_anything = true")


def run_recover_main(monkeypatch, tmp_path, config_arg):
    from loadgov.daemon import main as dmain

    seen = {}
    monkeypatch.setattr(dmain.recover, "run", lambda *a, **kw: seen.update(kw) or 0)
    argv = ["loadgovd", "--recover", "--force", "--config", config_arg, "--state-dir", str(tmp_path),
            "--proc-root", str(tmp_path), "--cgroup-root", str(tmp_path), "--lock-root", str(tmp_path)]  # fmt: skip
    monkeypatch.setattr("sys.argv", argv)
    assert dmain.main() == 0
    return seen


def test_recover_never_depends_on_a_valid_config(monkeypatch, tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("[compat]\nbogus = 1\n")
    assert run_recover_main(monkeypatch, tmp_path, str(bad))["marker"] is None
    assert "not usable" in capsys.readouterr().err
    wrong = tmp_path / "wrong.toml"
    wrong.write_text("profiles = 1\n")  # structurally wrong: not a ConfigError
    assert run_recover_main(monkeypatch, tmp_path, str(wrong))["marker"] is None
    assert "not usable" in capsys.readouterr().err
    unreadable = tmp_path / "dir.toml"
    unreadable.mkdir()  # open() raises OSError
    assert run_recover_main(monkeypatch, tmp_path, str(unreadable))["marker"] is None
    good = tmp_path / "good.toml"
    good.write_text('[memguard]\npause_marker = { file = "m.paused", project = "p" }\n')
    assert run_recover_main(monkeypatch, tmp_path, str(good))["marker"] == PauseMarker("m.paused", "p")
