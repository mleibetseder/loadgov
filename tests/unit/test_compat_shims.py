"""compat/ shims, run as real bash against a fake `loadctl` (records argv) and a fake legacy dir."""

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

COMPAT = Path(__file__).resolve().parents[2] / "compat"

FAKE_LOADCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
if [ "$1" = status ] && [ "$2" = --json ]; then exit "${FAKE_STATUS_RC:-0}"; fi
[ -n "${FAKE_OUT:-}" ] && printf '%s\\n' "$FAKE_OUT"
exit "${FAKE_RC:-0}"
"""
LEGACY = '#!/bin/sh\necho "legacy $0 $*"\nexit 42\n'
LEGACY_REL = [
    "proj-a/rustc-slot.sh",
    "proj-a/host-wait.sh",
    "proj-b/wait-headroom.sh",
    "hwmon/guarded",
    "hwmon/hwmon",
]


def script(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def env(tmp_path):
    bindir, legacy = tmp_path / "bin", tmp_path / "legacy"
    script(bindir / "loadctl", FAKE_LOADCTL)
    for rel in LEGACY_REL:
        script(legacy / rel, LEGACY)
    script(
        legacy / "heavy-slot/heavy-slot.sh",
        'heavy_slot_acquire() { echo "legacy heavy $1"; return 0; }\n',
    )
    sock_path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(sock_path))
    log = tmp_path / "log"
    log.write_text("")
    e = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "LOADGOV_SOCKET": str(sock_path),
        "LOADGOV_LEGACY_DIR": str(legacy),
        "FAKE_LOG": str(log),
        "HOME": str(tmp_path),
        "HEAVY_SLOT_LOCK": str(tmp_path / "heavy.lock"),
        "LOADGOV_HEAVY_CRATES": "proj_a_backend,proj_a_worker proj_a_main",
        "LOADGOV_COMPILE_PROJECT": "proj-a",
        "LOADGOV_WAIT_PROJECT": "proj-a",
    }
    yield e, log, sock_path
    srv.close()


def sh(e, name, *args, cwd=None, extra=None, text=None):
    cmd = ["bash", str(COMPAT / name), *args] if text is None else ["bash", "-c", text]
    return subprocess.run(
        cmd, env={**e, **(extra or {})}, capture_output=True, text=True, cwd=cwd, timeout=20
    )


def calls(log):
    return [x for x in log.read_text().splitlines() if x != "status --json"]


def test_rustc_slot_heavy_crate_goes_through_loadctl_run(env):
    e, log, _ = env
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "proj_a_backend", "-O", extra={"FAKE_RC": "7"})
    assert r.returncode == 7
    assert calls(log) == [
        "run --kind compile --project proj-a --label proj_a_backend --headroom none -- /bin/echo --crate-name proj_a_backend -O"
    ]


def test_rustc_slot_other_crates_run_at_once(env):
    e, log, _ = env
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "serde")
    assert r.stdout == "--crate-name serde\n" and log.read_text() == ""


def test_rustc_slot_room_bypass(env):
    e, log, _ = env
    r = sh(
        e, "rustc-slot.sh", "/bin/echo", "--crate-name", "proj_a_worker", extra={"RUSTC_SLOT_ROOM_GB": "0"}
    )
    assert r.stdout == "--crate-name proj_a_worker\n" and log.read_text() == ""


@pytest.mark.parametrize("how", ["socket-missing", "exit-3", "no-loadctl"])
def test_rustc_slot_falls_back_to_legacy_with_identical_args(env, tmp_path, how):
    e, log, sock = env
    extra = {}
    if how == "socket-missing":
        sock.unlink()
    elif how == "exit-3":
        extra["FAKE_STATUS_RC"] = "3"
    else:
        (tmp_path / "bin" / "loadctl").unlink()
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "proj_a_worker", "x y", extra=extra)
    assert r.returncode == 42
    assert (
        r.stdout.strip()
        == f"legacy {e['LOADGOV_LEGACY_DIR']}/proj-a/rustc-slot.sh /bin/echo --crate-name proj_a_worker x y"
    )
    assert not any(c.startswith("run") for c in calls(log))


def test_a_job_exit_3_is_not_mistaken_for_a_dead_daemon(env):
    e, log, _ = env
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "proj_a_worker", extra={"FAKE_RC": "3"})
    assert r.returncode == 3 and "legacy" not in r.stdout


def test_guarded_runs_compile_with_floor_and_keeps_137(env, tmp_path):
    e, log, _ = env
    proj = tmp_path / "my proj"
    proj.mkdir()
    r = sh(e, "guarded", "./gradlew", "build", cwd=proj, extra={"FAKE_RC": "137"})
    assert r.returncode == 137
    assert calls(log) == [
        "run --kind compile --project my proj --label guarded --headroom level --floor-mb 2560 --on-floor kill -- ./gradlew build"
    ]


def test_guarded_falls_back(env):
    e, _, sock = env
    sock.unlink()
    r = sh(e, "guarded", "make", "-j2")
    assert r.returncode == 42 and r.stdout.strip().endswith("hwmon/guarded make -j2")


def test_hwmon_status_gate_wait_and_legacy_commands(env):
    e, log, _ = env
    assert sh(e, "hwmon", "status", extra={"FAKE_OUT": "hwmon t  level OK"}).stdout == "hwmon t  level OK\n"
    assert sh(e, "hwmon", extra={"FAKE_OUT": "hwmon t  level OK"}).returncode == 0
    ok = sh(e, "hwmon", "gate", extra={"FAKE_OUT": "hwmon t  level BUSY\n  cpu x"})
    assert ok.returncode == 0 and "level BUSY" in ok.stdout
    limit = sh(e, "hwmon", "gate", extra={"FAKE_OUT": "hwmon t  level LIMIT\n  cpu x"})
    assert limit.returncode == 1 and "level LIMIT" in limit.stdout
    sh(e, "hwmon", "wait", "120")
    sh(e, "hwmon", "wait")
    assert calls(log)[-2:] == ["level-wait 120", "level-wait 1800"]
    sh(e, "hwmon", "history", "7")
    sh(e, "hwmon", "history")
    assert calls(log)[-2:] == ["history 7 --csv", "history 40 --csv"]
    r = sh(e, "hwmon", "watch", "5")
    assert r.returncode == 42 and r.stdout.strip().endswith("hwmon/hwmon watch 5")


def test_hwmon_falls_back_when_down(env):
    e, _, sock = env
    sock.unlink()
    for cmd in ("status", "gate", "wait", "history"):
        assert sh(e, "hwmon", cmd).returncode == 42


def test_host_wait_translates_the_env_and_prints_nothing_on_stdout(env):
    e, log, _ = env
    r = sh(e, "host-wait.sh", extra={"LOAD_MAX": "6", "PSI_MAX": "50", "MEM_MIN_GB": "3", "SWAP_MAX_MB": "512",
                                     "MEMPSI_MAX": "7", "FAKE_OUT": "HEADROOM_OK host-wait"})  # fmt: skip
    assert r.returncode == 0 and r.stdout == ""
    assert "HEADROOM_OK" in r.stderr
    assert calls(log) == [
        "wait --kind impl --project proj-a --label host-wait --timeout 0 --load1-max 6 --psi-max 50 "
        "--mem-min-gb 3 --swap-max-mb 512 --mem-psi-max 7"
    ]


def test_host_wait_without_overrides_and_fallback(env):
    e, log, sock = env
    sh(e, "host-wait.sh")
    assert calls(log) == ["wait --kind impl --project proj-a --label host-wait --timeout 0"]
    sock.unlink()
    assert sh(e, "host-wait.sh").returncode == 42


def test_wait_headroom_translates_env_label_and_exit_codes(env):
    e, log, _ = env
    e = {**e, "LOADGOV_WAIT_PROJECT": "proj-b"}
    r = sh(e, "wait-headroom.sh", "dispatch-9", extra={
        "HEADROOM_LOAD1": "4", "HEADROOM_LOAD5": "4.5", "HEADROOM_PSI": "20", "HEADROOM_MEM_GB": "4",
        "HEADROOM_SWAP_MB": "1024", "HEADROOM_MEM_PSI": "5", "HEADROOM_CONFIRM": "1", "HEADROOM_POLL": "2",
        "HEADROOM_MAX_WAIT": "9", "FAKE_OUT": "HEADROOM_OK dispatch-9 x", "FAKE_RC": "1"})  # fmt: skip
    assert r.returncode == 1 and r.stdout == "HEADROOM_OK dispatch-9 x\n"
    assert calls(log) == [
        "wait --kind impl --project proj-b --label dispatch-9 --load1-max 4 --load5-max 4.5 --psi-max 20 "
        "--mem-min-gb 4 --swap-max-mb 1024 --mem-psi-max 5 --confirm 1 --poll 2 --timeout 9"
    ]


def test_wait_headroom_default_label_fallback_and_race_to_exit_3(env):
    e, log, sock = env
    e = {**e, "LOADGOV_WAIT_PROJECT": "proj-b"}
    sh(e, "wait-headroom.sh")
    assert calls(log) == ["wait --kind impl --project proj-b --label job"]
    r = sh(e, "wait-headroom.sh", "x", extra={"FAKE_RC": "3"})  # the daemon died after the check
    assert r.returncode == 42 and r.stdout.strip().endswith("proj-b/wait-headroom.sh x")
    sock.unlink()
    assert sh(e, "wait-headroom.sh", "y").returncode == 42


def test_heavy_slot_function_uses_loadctl_with_fd_9(env):
    e, log, _ = env
    r = sh(
        e, "", text=f'. {COMPAT}/heavy-slot.sh; heavy_slot_acquire "proj-a gate job-1"; echo rc=$?; echo $$'
    )
    assert r.returncode == 0 and "rc=0" in r.stdout
    pid = r.stdout.split()[-1]
    assert calls(log) == [f"slot acquire heavy --project proj-a --label gate job-1 --fd 9 --wait --pid {pid}"]
    assert Path(e["HEAVY_SLOT_LOCK"]).exists()


def test_heavy_slot_function_falls_back_and_restores_itself(env):
    e, log, sock = env
    sock.unlink()
    r = sh(
        e,
        "",
        text=f'. {COMPAT}/heavy-slot.sh; heavy_slot_acquire "proj-b seam"; echo rc=$?; declare -f heavy_slot_acquire',
    )
    assert "legacy heavy proj-b seam" in r.stdout and "rc=0" in r.stdout
    assert "__loadgov_up" in r.stdout  # our function is back after the legacy file replaced it


def test_heavy_slot_passes_the_busy_exit_code(env):
    e, _, _ = env
    r = sh(
        e, "", text=f'. {COMPAT}/heavy-slot.sh; heavy_slot_acquire "p l"; echo rc=$?', extra={"FAKE_RC": "1"}
    )
    assert "rc=1" in r.stdout


def test_all_shims_are_executable_bash_with_nounset():
    for name in ("rustc-slot.sh", "guarded", "hwmon", "host-wait.sh", "wait-headroom.sh"):
        p = COMPAT / name
        assert os.access(p, os.X_OK), name
        text = p.read_text()
        assert text.startswith("#!/usr/bin/env bash\n") and "\nset -u\n" in text
        assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_defaults_are_generic_and_the_legacy_fallback_is_optional(env, tmp_path):
    e, log, sock = env
    e = {k: v for k, v in e.items() if not k.startswith("LOADGOV_") or k == "LOADGOV_SOCKET"}
    e["LOADGOV_LEGACY_DIR"] = str(tmp_path / "no-legacy")
    sh(e, "host-wait.sh")
    sh(e, "wait-headroom.sh", "x")
    assert calls(log) == [
        "wait --kind impl --project default --label host-wait --timeout 0",
        "wait --kind impl --project default --label x",
    ]
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "anything")  # no crate list: none is heavy
    assert r.stdout == "--crate-name anything\n"
    sock.unlink()
    for name in ("host-wait.sh", "wait-headroom.sh"):
        r = sh(e, name)
        assert r.returncode == 3 and "no legacy script" in r.stderr
    e["LOADGOV_HEAVY_CRATES"] = "anything"
    r = sh(e, "rustc-slot.sh", "/bin/echo", "--crate-name", "anything")  # down, no legacy: rustc itself
    assert r.returncode == 0 and r.stdout == "--crate-name anything\n"


def test_each_wait_shim_has_its_own_project_variable_with_a_shared_fallback(env):
    e, log, _ = env
    sh(e, "host-wait.sh", extra={"LOADGOV_HOSTWAIT_PROJECT": "proj-x"})
    sh(e, "wait-headroom.sh", "l", extra={"LOADGOV_HEADROOM_PROJECT": "proj-y"})
    sh(e, "wait-headroom.sh", "m")  # the shared LOADGOV_WAIT_PROJECT of the fixture
    assert [c.split()[4] for c in calls(log)] == ["proj-x", "proj-y", "proj-a"]
