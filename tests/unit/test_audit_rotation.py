"""Size rotation of audit.jsonl (tmp_path only)."""

import json
import os

import pytest

from loadgov.config import ConfigError, parse_config
from loadgov.daemon.audit import AuditLog


def row(i: int, pad: int = 0) -> dict:
    return {"op": "set", "reason": f"{i}" + "x" * pad}


def reasons(rows):
    return [int(r["reason"].rstrip("x")) for r in rows]


def small(tmp_path, keep=3, size_bytes=1):
    a = AuditLog(str(tmp_path), keep=keep)
    a.max_mb = size_bytes / 1048576  # a byte-sized limit keeps the tests small
    return a


def test_rotates_at_the_size_boundary(tmp_path):
    probe = AuditLog(str(tmp_path / "probe"))
    probe.append(row(0))
    n = os.path.getsize(probe.path)  # every test line has the same length
    a = small(tmp_path, size_bytes=2 * n)  # exactly two lines fit
    for i in range(3):
        a.append(row(i))
    assert os.path.getsize(a.path) == n  # the third line started a new file
    with open(a.path + ".1") as f:
        assert reasons(json.loads(x) for x in f) == [0, 1]  # no line split or lost
    assert reasons(a.tail(10)) == [0, 1, 2]


def test_shifts_and_deletes_beyond_keep(tmp_path):
    a = small(tmp_path, keep=2)
    for i in range(5):
        a.append(row(i))
    assert sorted(os.listdir(tmp_path)) == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2"]
    assert reasons(a.tail(10)) == [2, 3, 4]  # 0 and 1 were deleted


def test_zero_disables_rotation(tmp_path):
    a = AuditLog(str(tmp_path), max_mb=0)
    for i in range(50):
        a.append(row(i, 200))
    assert os.listdir(tmp_path) == ["audit.jsonl"]
    assert len(a.tail(100)) == 50


def test_tail_reads_across_a_rotation(tmp_path):
    a = small(tmp_path, keep=3)
    for i in range(4):
        a.append(row(i))
    assert reasons(a.tail(3)) == [1, 2, 3]
    assert reasons(a.tail(0)) == []
    assert reasons(a.tail(2)) == [2, 3]


def test_tail_with_no_file(tmp_path):
    assert AuditLog(str(tmp_path)).tail(5) == []


def test_survives_external_removal_and_move(tmp_path):
    a = AuditLog(str(tmp_path))
    a.append(row(0))
    os.unlink(a.path)
    a.append(row(1))
    os.rename(a.path, a.path + ".moved")
    a.append(row(2))
    assert reasons(a.tail(5)) == [2]


def test_copytruncate_style_truncation(tmp_path):
    a = AuditLog(str(tmp_path))
    a.append(row(0))
    open(a.path, "w").close()
    a.append(row(1))
    assert reasons(a.tail(5)) == [1]


def test_rename_error_is_logged_once_and_appending_continues(tmp_path, monkeypatch, caplog):
    a = small(tmp_path)
    a.append(row(0))

    def boom(src, dst):
        raise PermissionError("no")

    monkeypatch.setattr(os, "replace", boom)
    for i in range(1, 4):
        a.append(row(i))  # must not raise
    assert reasons(a.tail(10)) == [0, 1, 2, 3]
    assert [r.levelname for r in caplog.records].count("ERROR") == 1


def test_configure_changes_the_limit(tmp_path):
    a = AuditLog(str(tmp_path))
    a.configure(0, 2)
    assert (a.max_mb, a.keep) == (0, 2)


def test_config_defaults_and_values():
    c = parse_config("").audit
    assert (c.max_mb, c.keep) == (50, 5)
    c = parse_config("[audit]\nmax_mb = 0\nkeep = 1\n").audit
    assert (c.max_mb, c.keep) == (0, 1)


@pytest.mark.parametrize(
    "body",
    ["max_mb = -1", "max_mb = 1.5", 'max_mb = "9"', "max_mb = true", "keep = 0", "keep = true", "bogus = 1"],
)
def test_config_rejects_bad_values(body):
    with pytest.raises(ConfigError):
        parse_config(f"[audit]\n{body}\n")


def test_a_failing_rename_of_the_current_file_shifts_nothing(tmp_path, monkeypatch):
    a = small(tmp_path, keep=3)
    for i in range(3):
        a.append(row(i))  # audit.jsonl.1 and .2 exist now
    before = {n: open(tmp_path / n).read() for n in sorted(os.listdir(tmp_path))}
    real = os.replace

    def deny_current(src, dst):
        if src == a.path:
            raise PermissionError("append-only")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", deny_current)
    for i in range(3, 8):
        a.append(row(i))  # rotation keeps failing; the rotated files must stay
    for n in ("audit.jsonl.1", "audit.jsonl.2"):
        assert open(tmp_path / n).read() == before[n]
    assert reasons(a.tail(100)).count(7) == 1
    monkeypatch.setattr(os, "replace", real)
    a.append(row(8))  # rename works again: rotation resumes
    assert reasons(a.tail(100))[-1] == 8 and not os.path.exists(a.path + ".new")


def test_a_failure_while_shifting_puts_the_current_file_back(tmp_path, monkeypatch):
    a = small(tmp_path, keep=3)
    a.append(row(0))
    a.append(row(1))  # .1 exists
    real = os.replace

    def deny_shift(src, dst):
        if src.endswith(".1") and dst.endswith(".2"):
            raise OSError("boom")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", deny_shift)
    a.append(row(2))
    assert reasons(a.tail(10)) == [0, 1, 2] and not os.path.exists(a.path + ".new")


def test_tail_skips_a_torn_or_undecodable_line(tmp_path):
    a = AuditLog(str(tmp_path))
    a.append(row(1))
    with open(a.path, "ab") as f:
        f.write(b'{"op":"se\n\xff\xfe broken\n')
    a.append(row(2))
    assert reasons(a.tail(10)) == [1, 2]


def test_new_audit_files_are_created_with_mode_0640(tmp_path):
    old = os.umask(0)
    try:
        a = AuditLog(str(tmp_path))
        a.append(row(1))
    finally:
        os.umask(old)
    assert os.stat(a.path).st_mode & 0o777 == 0o640


def test_a_leftover_new_file_is_folded_in_never_overwritten(tmp_path):
    a = small(tmp_path, keep=6)
    a.append(row(0))
    a.append(row(1))  # .1 = [0]
    os.replace(a.path, a.path + ".new")  # a kill between the two renames: [1] is in .new
    a.append(row(2))
    b = small(tmp_path, keep=6)  # the restarted daemon
    assert reasons(b.tail(10)) == [0, 1, 2]  # visible before any fold
    b.append(row(3))  # rotation: .new must be folded first
    b.append(row(4))
    assert not os.path.exists(b.path + ".new")
    assert reasons(b.tail(10)) == [0, 1, 2, 3, 4]


def test_a_leftover_new_file_is_folded_at_start_and_tail_reads_it(tmp_path):
    a = small(tmp_path, keep=3)
    a.append(row(0))
    os.replace(a.path, a.path + ".new")
    a.append(row(1))
    assert reasons(a.tail(10)) == [0, 1]  # .new sits between the current file and .1
    b = AuditLog(str(tmp_path), keep=3)  # init folds it into .1
    assert os.path.exists(b.path + ".1") and not os.path.exists(b.path + ".new")
    assert reasons(b.tail(10)) == [0, 1]
