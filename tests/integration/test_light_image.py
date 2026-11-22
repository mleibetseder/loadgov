"""Host test of the light lane and the image train against real systemd, with a private loadgovd.

Runs only with LOADGOV_INTEGRATION=1 as root. The daemon has its own socket, state and log directories
under tmp_path and its own config; every job and the image runner start in scopes of `loadgov-test.slice`
(the hidden `--slice` flag), never a build-work*.slice. The light jobs are `sleep` and a small allocation."""

import datetime
import json
import os
import subprocess
import sys
import time

import pytest

from loadgov.cli.client import DaemonUnavailable
from loadgov.cli.client import request as ask

pytestmark = pytest.mark.skipif(
    os.environ.get("LOADGOV_INTEGRATION") != "1" or os.geteuid() != 0,
    reason="needs LOADGOV_INTEGRATION=1 and root",
)

SLICE = "loadgov-test.slice"
RELAXED = """
load1_max = 100000
load5_max = false
psi_max = 101
mem_min_gb = 0
swap_max_mb = 10000000
mem_psi_max = 101
confirm_s = 1
poll_s = 1
max_wait_s = 60
"""
CONFIG = (
    "".join(f"\n[headroom.kinds.{k}]{RELAXED}" for k in ("light", "image"))
    + """
[light]
cpu_quota_pct = 50
memory_max_mb = 128
max_runtime_s = 30
"""
)


def window_around_now(start_h: int, end_h: int) -> str:
    now = datetime.datetime.now(datetime.UTC)
    t = lambda h: (now + datetime.timedelta(hours=h)).strftime("%H:%M")  # noqa: E731
    return f'\n[image]\nwindow = "{t(start_h)}-{t(end_h)}"\ntimezone = "UTC"\n'


@pytest.fixture
def daemon(tmp_path, request: pytest.FixtureRequest):
    sock = str(tmp_path / "run" / "loadgovd.sock")
    cfg = tmp_path / "loadgov.toml"
    extra, tick = getattr(request, "param", ("", 1))
    cfg.write_text(CONFIG + extra)
    (tmp_path / "lock").mkdir()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "loadgov.daemon.main", "--socket", sock, "--run-dir", str(tmp_path / "run"),
            "--compat-link", "", "--config", str(cfg), "--state-dir", str(tmp_path / "state"),
            "--log-dir", str(tmp_path / "log"), "--env-file", "", "--lock-root", str(tmp_path / "lock"),
            "--tick", str(tick),
        ]
    )  # fmt: skip
    for _ in range(100):
        try:
            if ask(sock, "status").get("ok"):
                break
        except DaemonUnavailable:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("loadgovd did not come up")
    yield sock
    proc.terminate()
    proc.wait(timeout=10)


def loadctl(sock, *args):
    return subprocess.Popen(
        [sys.executable, "-m", "loadgov.cli.loadctl", "--socket", sock, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def light(sock, label, *cmd):
    return loadctl(sock, "run", "--kind", "light", "--project", "test", "--label", label, "--slice", SLICE,
                   "--", *cmd)  # fmt: skip


def show(unit, prop):
    out = subprocess.run(["systemctl", "show", unit, "-p", prop, "--value"], capture_output=True, text=True)
    return out.stdout.strip()


def wait_for(cond, seconds, what):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    pytest.fail(f"timeout: {what}")


def first_job(sock, seconds=20):
    return wait_for(
        lambda: (ask(sock, "status")["status"]["jobs"] or [None])[0], seconds, "job in status.jobs"
    )


def audit_lines(tmp_path, op):
    path = tmp_path / "log" / "audit.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [x for x in lines if x.get("op") == op]


def test_light_scope_has_the_configured_limits_and_is_gone_afterwards(daemon):
    p = light(daemon, "itest-light", "sleep", "4")
    try:
        unit = first_job(daemon)["unit"]
        assert unit.startswith("loadgov-light-itest-light-") and unit.endswith(".scope")
        assert show(unit, "Slice") == SLICE
        assert show(unit, "CPUQuotaPerSecUSec") == "500ms"  # cpu_quota_pct = 50
        assert show(unit, "MemoryMax") == str(128 * 1024 * 1024)
        assert show(unit, "MemorySwapMax") == "0"
        assert show(unit, "RuntimeMaxUSec") == "1min"  # max_runtime_s 30 + 30 s backstop grace
        _out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    listed = subprocess.run(
        ["systemctl", "list-units", "--all", "--no-legend", "--plain", unit], capture_output=True, text=True
    ).stdout
    assert listed.strip() == "", listed  # neither active nor a failed leftover (loadctl reset it)
    assert ask(daemon, "status")["status"]["jobs"] == []


def assert_overrun_audited(tmp_path, needle):
    """The daemon audits `light_overrun` once, whichever side ends the job first (loadctl's `job_end ...
    overrun` or the daemon tick finding the scope empty). Wait for that line itself: `job_end` may come first."""
    lines = wait_for(lambda: audit_lines(tmp_path, "light_overrun"), 15, "the light_overrun audit line")
    assert len(lines) == 1, lines
    assert needle in lines[0]["reason"]


def cleanup_unit(unit):
    """A killed loadctl leaves its (uncollected) light scope behind: stop and reset that one unit."""
    if unit:
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)


# A slow daemon tick, so the two sides of the overrun race both get exercised across runs.
SLOW_TICK = pytest.mark.parametrize("daemon", [("", 10)], ids=["tick-10"], indirect=True)


@SLOW_TICK
def test_light_job_running_past_max_runtime_is_killed_and_audited(daemon, tmp_path):
    t0 = time.monotonic()
    p = light(daemon, "itest-runtime", "sleep", "120")
    unit = None
    try:
        unit = first_job(daemon)["unit"]
        _out, err = p.communicate(timeout=90)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        cleanup_unit(unit)
    assert p.returncode == 76, err
    assert "LIGHT_OVERRUN itest-runtime: runtime 30 s exceeded" in err
    assert time.monotonic() - t0 < 60  # loadctl's own kill at 30 s, not the 60 s systemd backstop
    assert_overrun_audited(tmp_path, "runtime 30 s exceeded")


@SLOW_TICK
def test_light_job_above_memory_max_is_oom_killed_and_reported(daemon, tmp_path):
    # 400 MB written into a 128 MB scope with no swap: the kernel OOM-kills it (Result=oom-kill). The job
    # sleeps first so loadctl has registered it (a job killed earlier is still audited, via job_end project/label).
    p = light(
        daemon,
        "itest-memory",
        sys.executable,
        "-c",
        "import time; time.sleep(3); x = b'x' * (400 << 20); print(len(x))",
    )
    unit = None
    try:
        unit = first_job(daemon)["unit"]
        _out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        cleanup_unit(unit)
    assert p.returncode == 76, err
    assert "LIGHT_OVERRUN itest-memory: memory limit 128 MB hit" in err
    assert_overrun_audited(tmp_path, "memory limit 128 MB hit")


def image_submit(sock, cwd, channel="release"):
    r = subprocess.run(
        [sys.executable, "-m", "loadgov.cli.loadctl", "--socket", sock, "image", "submit", "--project", "test",
         "--label", "itest-image", "--channel", channel, "--cwd", str(cwd), "--", "sleep", "3"],
        capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


def image_runner(sock, unit):
    """The runner in its own scope of the test slice: it must not run inside a governed cgroup (the
    daemon refuses that), and the job it starts goes to the test slice too."""
    return subprocess.Popen(
        ["systemd-run", "--scope", "--quiet", f"--slice={SLICE}", f"--unit={unit}", "--",
         sys.executable, "-m", "loadgov.cli.loadctl", "--socket", sock, "image", "runner", "--once",
         "--slice", SLICE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


@pytest.mark.parametrize("daemon", [(window_around_now(-1, 1), 1)], ids=["window-open"], indirect=True)
def test_image_runner_runs_a_released_job_in_its_own_scope_and_reports_it_done(daemon, tmp_path):
    image_submit(daemon, tmp_path)
    assert ask(daemon, "status")["status"]["image"]["queued"] == 1
    p = image_runner(daemon, f"loadgov-test-image-runner-{os.getpid()}")
    try:
        job = first_job(daemon, 90)  # the job runs in its own scope of the test slice, under the heavy slot
        assert job["kind"] == "image" and job["slot"] == "heavy"
        assert job["unit"].startswith("loadgov-image-itest-image-") and job["unit"].endswith(".scope")
        assert job["cgroup"].startswith(f"/loadgov.slice/{SLICE}/")
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert p.returncode == 0, err
    assert "IMAGE_DONE" in out and "exit=0" in out, out
    image = ask(daemon, "status")["status"]["image"]
    assert image["queue"] == [] and image["queued"] == 0 and image["running"] == 0
    done = audit_lines(tmp_path, "image_done")
    assert done and done[-1]["request"]["exit_code"] == 0 and done[-1]["result"] != "failed", done


@pytest.mark.parametrize("daemon", [(window_around_now(2, 3), 1)], ids=["window-closed"], indirect=True)
def test_image_job_outside_the_window_is_not_started(daemon, tmp_path):
    image_submit(daemon, tmp_path)
    p = image_runner(daemon, f"loadgov-test-image-closed-{os.getpid()}")
    try:
        out, err = p.communicate(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    assert "IMAGE_DONE" not in out, (out, err)
    image = ask(daemon, "status")["status"]["image"]
    assert image["queued"] == 1 and image["running"] == 0 and not image["in_window"]
    assert not audit_lines(tmp_path, "image_start")
