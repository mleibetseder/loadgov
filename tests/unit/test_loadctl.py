import json

from loadgov.cli import loadctl
from loadgov.sample import Sampler


def status(host):
    s = Sampler(host.fs)
    s.prime(host.prev_stat)
    out = s.sample(now=1_762_800_000.0)
    out["daemon"] = {"version": "t", "pid": 1, "started_at": "t", "mode": "observe", "tick_s": 5.0}
    return out


def test_hwmon_format_is_the_legacy_five_lines(fake_host):
    text = loadctl.format_hwmon(status(fake_host("busy")))
    lines = text.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith("hwmon ") and lines[0].endswith("level BUSY")
    assert lines[1] == "  cpu   BUSY    82.0 % busy, pressure 45 %, load 5.4 on 6 cores"
    assert lines[2].startswith("  ram   OK     6.8 of 15.9 GB available")


def test_status_unreachable_exits_3(tmp_path, capsys):
    rc = loadctl.main(["--socket", str(tmp_path / "nope.sock"), "status"])
    assert rc == 3
    assert "not reachable" in capsys.readouterr().err


def test_status_json_via_fake_daemon(monkeypatch, fake_host, capsys):
    st = status(fake_host("calm"))
    monkeypatch.setattr(loadctl, "request", lambda *_a, **_k: {"ok": True, "status": st})
    assert loadctl.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["level"] == "OK"
    assert loadctl.main(["status"]) == 0
    assert "slot  heavy: free" in capsys.readouterr().out


def test_freeze_unfreeze_and_reap_daemons_send_their_ops(monkeypatch, capsys):
    sent = []

    def fake(_sock, op, args=None, **_k):
        sent.append((op, args))
        if op == "freeze":
            return {"ok": True, "frozen": ["h1", "h2"], "would": False, "expires_at": None}
        if op == "reap":
            return {"ok": True, "reaped": [{"pid": 7}], "would": True}
        return {"ok": False, "refused": True, "restrictedBy": "system", "reason": "memory is low"}

    monkeypatch.setattr(loadctl, "request", fake)
    assert loadctl.main(["freeze", "--owner", "--reason", "loud", "--ttl", "10s", "--project", "p"]) == 0
    assert capsys.readouterr().out.strip() == "froze 2 tree(s)"
    assert loadctl.main(["reap-daemons"]) == 0
    assert "would stop 1 build daemon(s) 7" in capsys.readouterr().out
    assert loadctl.main(["unfreeze", "--owner"]) == 4  # refused by the memory guard
    assert "memory guard holds the pause" in capsys.readouterr().err
    assert sent == [
        ("freeze", {"project": "p", "owner": True, "reason": "loud", "ttl": "10s"}),
        ("reap", {"reason": "loadctl reap-daemons"}),
        ("unfreeze", {"owner": True}),
    ]


def test_hwmon_prints_local_time_status_json_stays_utc(monkeypatch, fake_host):
    import time as _time

    monkeypatch.setenv("TZ", "Europe/Berlin")
    _time.tzset()
    try:
        st = status(fake_host("calm"))
        st["at"] = "2025-07-01T10:00:00Z"
        assert loadctl.format_hwmon(st).splitlines()[0].startswith("hwmon 2025-07-01 12:00:00  level ")
        assert st["at"].endswith("Z")
    finally:
        monkeypatch.undo()
        _time.tzset()


def _serve_once(path, payload):
    import socket
    import struct
    import threading

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        c.recv(65536)
        if payload is None:  # abortive close: the client sees a reset
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        else:
            c.sendall(payload)
        c.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()


def test_reset_truncated_and_invalid_replies_exit_3_without_traceback(tmp_path, capsys):
    for i, payload in enumerate([None, b'{"ok": tr', b"not json\n", b"[1]\n"]):
        sock = tmp_path / f"s{i}.sock"
        _serve_once(sock, payload)
        assert loadctl.main(["--socket", str(sock), "status"]) == 3
        assert "loadctl:" in capsys.readouterr().err
