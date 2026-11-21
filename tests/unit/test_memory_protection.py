"""The daemon's effective memory protection is the least memory.min along its cgroup path."""

from pathlib import Path

import pytest

from loadgov.host.cgroup import effective_memory_min
from loadgov.host.procfs import HostFS

MB = 1024 * 1024
PATH = "/system.slice/loadgovd.service"


@pytest.fixture
def fs(tmp_path):
    (tmp_path / "proc" / "self").mkdir(parents=True)
    (tmp_path / "proc" / "self" / "cgroup").write_text(f"0::{PATH}\n")
    return HostFS(proc_root=str(tmp_path / "proc"), cgroup_root=str(tmp_path / "cg"))


def put(fs, cgroup, value):
    d = Path(fs.cgroup_root + cgroup)
    d.mkdir(parents=True, exist_ok=True)
    if value is not None:
        (d / "memory.min").write_text(f"{value}\n")


def test_least_value_along_the_path_wins(fs):
    put(fs, "/system.slice/loadgovd.service", 96 * MB)
    put(fs, "/system.slice", 64 * MB)
    assert effective_memory_min(fs) == 64 * MB


def test_a_parent_at_zero_caps_the_child_at_zero(fs):
    put(fs, "/system.slice/loadgovd.service", 96 * MB)
    put(fs, "/system.slice", 0)
    assert effective_memory_min(fs) == 0


def test_a_missing_file_counts_as_zero(fs):
    put(fs, "/system.slice/loadgovd.service", 96 * MB)
    put(fs, "/system.slice", None)
    assert effective_memory_min(fs) == 0


def test_full_protection_is_the_unit_value(fs):
    put(fs, "/system.slice/loadgovd.service", 96 * MB)
    put(fs, "/system.slice", 128 * MB)
    assert effective_memory_min(fs) == 96 * MB


def test_unreadable_self_cgroup_is_zero(tmp_path):
    fs = HostFS(proc_root=str(tmp_path / "none"), cgroup_root=str(tmp_path))
    assert effective_memory_min(fs) == 0


def test_install_script_ships_the_dropin_without_lowering_a_higher_value():
    root = Path(__file__).parents[2]
    dropin = root / "packaging" / "systemd" / "system.slice.d" / "50-loadgov-memorymin.conf"
    assert "[Slice]\nMemoryMin=96M\n" in dropin.read_text()
    text = (root / "packaging" / "install.sh").read_text()
    assert "systemctl show system.slice -p MemoryMin" in text
    assert text.index("50-loadgov-memorymin.conf") < text.index("systemctl daemon-reload")
