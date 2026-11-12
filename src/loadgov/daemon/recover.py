"""`loadgovd --recover --force`: thaw everything the paused registry holds.

This is the only thaw-all. The daemon never runs it on stop, crash or start (a thaw storm once let two
gates run at once); a restart re-adopts the registry instead, and `ExecStopPost` must not call this.
Run it with the daemon stopped, as root: it reads `<state-dir>/state.json`, then for each recorded entry
thaws the cgroup, SIGCONTs the recorded processes (pid and start time re-checked) and unpauses the
containers, clears the freeze flag and the registry, and prints what it did. Without `--force` it only
lists what it would thaw.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
from collections.abc import Callable

from loadgov.config import PauseMarker
from loadgov.daemon.state import atomic_write_text
from loadgov.host import cgroup
from loadgov.host.docker import Docker, run_docker
from loadgov.host.procfs import HostFS
from loadgov.host.signals import Signals


def _daemon_running(socket_path: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(socket_path)
        return True
    except OSError:
        return False


def run(
    fs: HostFS,
    state_dir: str,
    socket_path: str,
    force: bool,
    *,
    marker: PauseMarker | None = None,  # [memguard.pause_marker], removed with the registry
    runner: Callable[[list[str]], tuple[int, str]] = run_docker,
    euid: Callable[[], int] = os.geteuid,
    signals: Signals | None = None,  # test-only: a Signals over a fake pidfd layer
    out=None,
    err=None,
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    if euid() != 0:
        print("loadgovd --recover: must run as root", file=err)
        return 4
    if _daemon_running(socket_path):
        print(f"loadgovd --recover: a daemon answers on {socket_path}; stop loadgovd first", file=err)
        return 5
    path = os.path.join(state_dir, "state.json")
    try:
        with open(path) as f:
            body = json.load(f)
    except FileNotFoundError:
        print("nothing recorded: no state.json", file=out)
        return 0
    except (OSError, ValueError) as e:
        print(f"loadgovd --recover: cannot read {path}: {e}", file=err)
        return 5
    entries = (body.get("memguard") or {}).get("paused") or []
    if not entries and not body.get("freeze"):
        print("nothing paused in the registry", file=out)
        return 0
    signals = signals or Signals(fs, enforce=False)  # SIGCONT is the undo and needs no enforce
    docker = Docker(runner, enforce=False)
    for e in entries:
        what = f"{e.get('what')} ({e.get('how')}, {e.get('kind')}, since {e.get('since')})"
        if not force:
            print(f"would thaw {what}", file=out)
            continue
        for t in e.get("targets") or []:
            try:
                if e.get("how") == "freeze":
                    res = cgroup.write_freeze(fs, t["cgroup"], False, False)
                elif e.get("how") == "sigstop":
                    res = signals.send(t["pid"], t.get("start"), signal.SIGCONT)
                else:
                    res = docker.unpause(t["name"])
            except (OSError, ValueError, KeyError) as ex:
                res = f"error: {ex}"
            print(f"thaw {what}: {t} -> {res}", file=out)
    if not force:
        print("nothing changed; run again with --force to thaw", file=out)
        return 2
    if marker and any(e.get("project") == marker.project for e in entries):
        try:
            os.unlink(fs.lock_path(marker.file))
        except FileNotFoundError:
            pass
        except OSError as ex:
            print(f"cannot remove {fs.lock_path(marker.file)}: {ex}", file=err)
    body["memguard"], body["freeze"] = {}, None
    atomic_write_text(path, json.dumps(body, indent=1))
    print(
        f"recovered: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} thawed, registry cleared",
        file=out,
    )
    return 0
