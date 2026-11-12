"""Start the real daemon on a temp socket against a fixture tree; nothing outside tmp is touched."""

import json
import os
import subprocess
import sys
import time

from loadgov.cli import loadctl
from loadgov.cli.client import request
from loadgov.protocol import validate_status


def test_daemon_serves_status_and_writes_only_its_run_dir(fake_host, tmp_path):
    host = fake_host("busy")
    run = tmp_path / "run"
    sock = run / "loadgovd.sock"
    link = tmp_path / "loadgovd.json"
    env = tmp_path / "load-governor.env"
    before = {p: os.stat(p).st_mtime_ns for p in _files(host.root)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "loadgov.daemon.main",
            "--socket",
            str(sock),
            "--run-dir",
            str(run),
            "--compat-link",
            str(link),
            "--tick",
            "0.2",
            "--proc-root",
            host.proc,
            "--cgroup-root",
            host.cgroup,
            "--lock-root",
            host.lock,
            "--config",
            str(tmp_path / "absent.toml"),
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(tmp_path / "log"),
            "--env-file",
            str(env),
        ],
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(400):  # up to 20 s on a loaded host
            if sock.exists():
                break
            time.sleep(0.05)
        reply = request(str(sock), "status")
        assert reply["ok"] is True
        assert validate_status(reply["status"]) == []
        assert reply["status"]["daemon"]["mode"] == "observe"
        assert (run / "status.json").exists()
        assert os.path.realpath(link) == str(run / "status.json")
        assert oct(os.stat(sock).st_mode & 0o777) == "0o600"
        assert request(str(sock), "nope")["error"] == "unknown_op"
        assert loadctl.main(["--socket", str(sock), "status", "--hwmon"]) == 0
        assert loadctl.main(["--socket", str(sock), "set", "--profile", "silent", "--reason", "t"]) == 0
        assert loadctl.main(["--socket", str(sock), "limits", "--json"]) == 0
        assert request(str(sock), "status")["status"]["limits"]["effective"]["max_cores"] == 2.0
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert proc.returncode == 0, proc.stderr.read().decode()
    assert not sock.exists()
    assert "MAX_CORES=2\n" in env.read_text() and "LOAD_HOLD=5\n" in env.read_text()
    audit = [json.loads(x) for x in (tmp_path / "log" / "audit.jsonl").read_text().splitlines()]
    assert audit[0]["op"] == "set" and audit[0]["result"] == "accepted" and audit[0]["reason"] == "t"
    assert (tmp_path / "state" / "state.json").exists()
    after = {p: os.stat(p).st_mtime_ns for p in _files(host.root)}
    assert before == after  # fixture /proc, cgroup and lock trees untouched


def _files(root):
    for d, _, fs in os.walk(root):
        for f in fs:
            yield os.path.join(d, f)
