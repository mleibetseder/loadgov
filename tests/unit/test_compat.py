import subprocess
from pathlib import Path

from loadgov.config import BUILTIN_PROFILES
from loadgov.daemon.compat import render_env, write_env
from loadgov.engines.limits import Limits, LimitState

GOLDEN = Path(__file__).parent / "golden" / "load-governor.env"
NOW = 1_762_800_000.0


def test_env_shim_matches_golden_and_keeps_load_hold():
    lim = Limits(dict(BUILTIN_PROFILES), LimitState("silent"), lambda: NOW)
    lim.ceiling_set("silent", "movie")
    text = render_env(lim.effective(), "silent", "silent", NOW)
    assert text == GOLDEN.read_text()
    assert "LOAD_MAX=2\n" in text and "LOAD_HOLD=5\n" in text


def test_env_file_is_atomic_and_shell_sourceable(tmp_path):
    path = tmp_path / "load-governor.env"
    lim = Limits(dict(BUILTIN_PROFILES), LimitState("balanced"), lambda: NOW)
    write_env(str(path), render_env(lim.effective(), "balanced", None, NOW))
    assert not (tmp_path / "load-governor.env.tmp").exists()
    out = subprocess.run(
        ["sh", "-c", f". {path}; echo $MAX_CORES $IMPL_MAX_QUIET [$OWNER_CEILING] $PROFILE"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "3.5 2 [] balanced"
