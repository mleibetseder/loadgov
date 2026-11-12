"""Sample history ring (fake clock), the `history` op, `loadctl history` and tools/backstop_sizing.py."""

import argparse
import importlib.util
import json
import os
from pathlib import Path

import pytest

from loadgov.cli.history import render
from loadgov.config import Config
from loadgov.daemon.main import Daemon
from loadgov.engines.history import COLUMNS, History, row_for
from loadgov.protocol import VERSION

SPEC = importlib.util.spec_from_file_location(
    "backstop_sizing", Path(__file__).parents[2] / "tools" / "backstop_sizing.py"
)
bs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bs)


class Clock:
    t = 1_762_800_000.0

    def __call__(self):
        return self.t


def row(i):
    return [f"2026-11-11T00:00:{i % 60:02d}Z", "OK"] + [str(i)] * (len(COLUMNS) - 2)


@pytest.fixture
def hist(tmp_path):
    clock = Clock()
    return History(str(tmp_path / "state" / "history.csv"), clock, keep=100, slack=10), clock


def lines(h):
    return open(h.path).read().splitlines()


def test_one_row_per_15_s_whatever_the_tick(hist):
    h, clock = hist
    wrote = []
    for _ in range(9):  # a 5 s daemon tick for 45 s
        wrote.append(h.record(row(1)))
        clock.t += 5
    assert wrote == [True, False, False, True, False, False, True, False, False]
    assert lines(h)[0] == ",".join(COLUMNS) and len(lines(h)) == 4  # header + 3 rows


def test_trims_in_chunks_to_keep_atomically(hist):
    h, clock = hist
    for i in range(110):  # keep 100 + slack 10: the 111th row triggers the trim
        h.record(row(i))
        clock.t += 15
    assert len(lines(h)) == 111
    h.record(row(110))
    ls = lines(h)
    assert ls[0] == ",".join(COLUMNS) and len(ls) == 101
    assert ls[-1].split(",")[2] == "110" and ls[1].split(",")[2] == "11"  # newest 100 kept
    assert not os.path.exists(h.path + ".tmp")


def test_tail_returns_the_newest_n_oldest_first_and_caps_n(hist):
    h, clock = hist
    for i in range(5):
        h.record(row(i))
        clock.t += 15
    assert [r[2] for r in h.tail(2)] == ["3", "4"]
    assert [r[2] for r in h.tail(99)] == ["0", "1", "2", "3", "4"]
    assert h.tail(0) == []
    assert History(h.path + ".none", clock).tail(5) == []


def test_a_row_torn_by_a_crash_is_skipped_and_the_next_row_starts_clean(hist):
    h, clock = hist
    h.record(row(1))
    with open(h.path, "a") as f:
        f.write("2026-11-11T00:00:09Z,OK,5")  # torn: no newline, too few columns
    h2 = History(h.path, clock, keep=100, slack=10)  # a restarted daemon
    clock.t += 15
    h2.record(row(2))
    assert [r[2] for r in h2.tail(10)] == ["1", "2"]
    assert len(lines(h2)) == 4  # header, row, the torn line, the new row


def test_row_for_a_status_sample(fake_host):
    from loadgov.sample import Sampler

    host = fake_host("calm")
    sm = Sampler(host.fs)
    sm.prime(host.prev_stat)
    s = sm.sample(now=1_762_800_000.0)
    r = row_for(s, {"mem": 1200, "swap": None, "peak": 3400})
    d = dict(zip(COLUMNS, r, strict=True))
    assert d["at"] == s["at"] and d["level"] == s["level"] and d["avail_mb"] == str(s["memory"]["avail_mb"])
    assert d["bw_mem_mb"] == "1200" and d["bw_swap_mb"] == "" and d["bw_peak_mb"] == "3400"
    assert len(r) == len(COLUMNS)


def make_daemon(fake_host, tmp_path):
    host = fake_host("calm")
    clock = Clock()
    d = Daemon(host.fs, state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "log"), env_file=None,
               compat_link=None, run_dir=str(tmp_path / "run"), config=Config(), clock=clock)  # fmt: skip
    os.makedirs(d.run_dir)
    d.sampler.prime(host.prev_stat)
    return d, clock


def test_daemon_ticks_fill_history_and_the_op_serves_it(fake_host, tmp_path):
    d, clock = make_daemon(fake_host, tmp_path)
    for _ in range(7):  # 35 s of 5 s ticks: rows at 0, 15 and 30 s
        d.tick()
        clock.t += 5
    reply = d.dispatch({"v": VERSION, "op": "history", "args": {"n": 2}})
    assert reply["ok"] is True and reply["columns"] == list(COLUMNS) and len(reply["rows"]) == 2
    assert d.dispatch({"v": VERSION, "op": "history"})["rows"].__len__() == 3  # default n = 40
    for bad in (-1, "x", True, 1.5):
        assert d.dispatch({"v": VERSION, "op": "history", "args": {"n": bad}})["error"] == "invalid_args"
    assert (tmp_path / "state" / "history.csv").read_text().startswith(",".join(COLUMNS))


def test_render_table_csv_and_json(fake_host, tmp_path):
    d, clock = make_daemon(fake_host, tmp_path)
    d.tick()
    reply = d.dispatch({"v": VERSION, "op": "history", "args": {"n": 5}})
    csv_out = render(reply, argparse.Namespace(csv=True, json=False)).splitlines()
    assert csv_out[0] == ",".join(COLUMNS) and csv_out[1] == ",".join(reply["rows"][0])
    js = json.loads(render(reply, argparse.Namespace(csv=False, json=True)))
    assert js[0]["level"] == "OK" and isinstance(js[0]["avail_mb"], int) and js[0]["bw_mem_mb"] is None
    table = render(reply, argparse.Namespace(csv=False, json=False)).splitlines()
    assert table[0].split() == list(COLUMNS)


def test_loadctl_history_passes_n_prints_csv_and_exits_3_when_the_daemon_is_down(
    fake_host, tmp_path, monkeypatch, capsys
):
    from loadgov.cli import loadctl
    from loadgov.cli.client import DaemonUnavailable

    d, clock = make_daemon(fake_host, tmp_path)
    d.tick()
    asked = []

    def fake_request(_sock, op, args=None, **_k):
        asked.append((op, args))
        return d.dispatch({"v": VERSION, "op": op, "args": args or {}}, 600)

    monkeypatch.setattr(loadctl, "request", fake_request)
    assert loadctl.main(["history", "3", "--csv"]) == 0
    assert asked == [("history", {"n": 3})]
    assert capsys.readouterr().out.splitlines()[0] == ",".join(COLUMNS)
    assert loadctl.main(["history"]) == 0 and asked[-1] == ("history", {"n": 40})

    def down(*_a, **_k):
        raise DaemonUnavailable("no socket")

    monkeypatch.setattr(loadctl, "request", down)
    assert loadctl.main(["history"]) == loadctl.EXIT_UNAVAILABLE


# ---- tools/backstop_sizing.py ---------------------------------------------------------------------

HEADER = ",".join(COLUMNS)


def csv_rows(spec):
    """spec: (day, mem, swap, peak) -> one CSV row each."""
    out = [HEADER]
    for day, mem, swap, peak in spec:
        out.append(f"2026-11-{day:02d}T12:00:00Z,OK,1,1,1,1,9000,0,50,3.5,{mem},{swap},{peak}")
    return "\n".join(out) + "\n"


def run(tmp_path, text, capsys, *args, total=16000):
    p = tmp_path / "h.csv"
    p.write_text(text)
    rc = bs.main([str(p), *args], total_mb=total)
    return rc, capsys.readouterr()


def test_sizing_suggests_peak_plus_margin_rounded_up(tmp_path, capsys):
    text = csv_rows([(1, 2000, 100, 2500), (4, 6000, 700, 9000), (8, 100, 0, 9100)])
    rc, out = run(tmp_path, text, capsys, "--days", "7")
    assert rc == 0
    assert "peak        memory 9100 MB" in out.out and "swap 700 MB" in out.out
    assert "memory_max_mb = 11008" in out.out  # 9100 + max(1024, 1820) = 10920 -> 11008 (x 256)
    assert "memory_swap_max_mb = 1792" in out.out  # 700 + 1024 = 1724 -> 1792
    assert (
        "enabled = true" in out.out and "# source: backstop_sizing.py over 2026-11-01..2026-11-08" in out.out
    )


def test_sizing_never_above_memtotal_minus_2gb_nor_below_4gb(tmp_path, capsys):
    rc, out = run(tmp_path, csv_rows([(1, 100, 0, 15000), (8, 100, 0, 15000)]), capsys, total=16000)
    assert "memory_max_mb = 13952" in out.out and "WARNING     peak + 1 GB exceeds 13952" in out.out
    rc, out = run(tmp_path, csv_rows([(1, 10, 0, 20), (8, 10, 0, 20)]), capsys)
    assert "memory_max_mb = 4096" in out.out and "memory_swap_max_mb = 1024" in out.out


def test_sizing_window_warning_and_no_data(tmp_path, capsys):
    rc, out = run(tmp_path, csv_rows([(1, 2000, 0, 2000), (3, 2000, 0, 2000)]), capsys)
    assert rc == 0 and "WARNING     only 2.0 days of history" in out.out
    old = csv_rows([(1, 99999, 0, 99999), (20, 1000, 0, 1000)])
    rc, out = run(tmp_path, old, capsys, "--days", "7")
    assert "peak        memory 1000 MB" in out.out  # the old spike is outside the window
    rc, out = run(tmp_path, HEADER + "\n", capsys)
    assert rc == 2 and "no rows" in out.err
    rc, out = run(tmp_path, csv_rows([(1, "", "", "")]), capsys)
    assert rc == 2


def test_socket_op_is_capped_at_one_day(fake_host, tmp_path):
    from loadgov.engines.history import SOCKET_MAX

    d, clock = make_daemon(fake_host, tmp_path)
    assert SOCKET_MAX == 5760
    os.makedirs(os.path.dirname(d.history.path), exist_ok=True)
    with open(d.history.path, "w") as f:
        f.write(",".join(COLUMNS) + "\n")
        f.writelines(",".join(row(i)) + "\n" for i in range(SOCKET_MAX + 50))
    got = d.dispatch({"v": VERSION, "op": "history", "args": {"n": 40320}})["rows"]
    assert len(got) == SOCKET_MAX


def test_loadctl_history_reads_the_file_for_big_n_and_all(tmp_path, monkeypatch, capsys):
    from loadgov.cli import loadctl
    from loadgov.engines.history import SOCKET_MAX

    state = tmp_path / "st"
    state.mkdir()
    (state / "history.csv").write_text(
        ",".join(COLUMNS) + "\n" + "".join(",".join(row(i)) + "\n" for i in range(SOCKET_MAX + 10))
    )

    def no_socket(*_a, **_k):
        raise AssertionError("must not use the socket")

    monkeypatch.setattr(loadctl, "request", no_socket)
    assert loadctl.main(["history", "--all", "--csv", "--state-dir", str(state)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == SOCKET_MAX + 11
    assert loadctl.main(["history", str(SOCKET_MAX + 1), "--csv", "--state-dir", str(state)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == SOCKET_MAX + 2
    assert loadctl.main(["history", "--all", "--state-dir", str(tmp_path / "none")]) == 5
