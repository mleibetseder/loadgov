"""Runtime state (profile, overrides, owner ceiling, hold/freeze) persisted to <state-dir>/state.json."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict

from loadgov.config import PARAMS, ConfigError, check_host_id
from loadgov.engines.limits import Ceiling, Flag, LimitState, Override

log = logging.getLogger("loadgovd")


def atomic_write_text(path: str, text: str) -> None:
    """Write tmp, fsync it, rename over `path`, fsync the directory: a crash leaves old or new, never half."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class StateStore:
    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "state.json")
        self.rejected_host_id: str | None = None  # a saved host id that failed the check (load_host_id)

    def save(
        self,
        state: LimitState,
        slots: dict | None = None,
        jobs: list | None = None,
        memguard: dict | None = None,
        job_stats: list | None = None,
        image: dict | None = None,
        host_id: str | None = None,
        job_endings: list | None = None,
        compile_peaks: dict | None = None,
    ) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        body = {
            **asdict(state),
            "slots": slots or {},
            "jobs": jobs or [],
            "memguard": memguard or {},
            "job_stats": job_stats or [],
            "image": image or {},
            "host_id": host_id,
            "job_endings": job_endings or [],
            "compile_peaks": compile_peaks or {},
        }
        atomic_write_text(self.path, json.dumps(body, indent=1))

    def load_host_id(self) -> str | None:
        """The host id saved by an earlier start (None when missing, unreadable or an old file without it)."""
        self.rejected_host_id = None
        try:
            with open(self.path) as f:
                raw = json.load(f).get("host_id")
        except (OSError, ValueError, AttributeError):
            return None
        if raw is None:
            return None
        try:
            return check_host_id(raw)  # the same check as the config
        except ConfigError:
            self.rejected_host_id = repr(raw)[:64]  # the daemon audits it once and derives a new id
            return None

    def load_jobs(self) -> list:
        """The saved `loadctl run` jobs (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("jobs")
        except (OSError, ValueError, AttributeError):
            return []
        return raw if isinstance(raw, list) else []

    def load_job_stats(self) -> list:
        """The saved per-(project, kind) job summaries (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("job_stats")
        except (OSError, ValueError, AttributeError):
            return []
        return raw if isinstance(raw, list) else []

    def load_job_endings(self) -> list:
        """The saved ring of recent job endings (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("job_endings")
        except (OSError, ValueError, AttributeError):
            return []
        return raw if isinstance(raw, list) else []

    def load_compile_peaks(self) -> dict:
        """The saved per-(project, crate) compile memory peaks (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("compile_peaks")
        except (OSError, ValueError, AttributeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def load_image(self) -> dict:
        """The saved image train: queued jobs, last beta starts (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("image")
        except (OSError, ValueError, AttributeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def load_memguard(self) -> dict:
        """The saved paused registry (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("memguard")
        except (OSError, ValueError, AttributeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def load_slots(self) -> dict:
        """The saved slot holders and holds (empty when missing or unreadable)."""
        try:
            with open(self.path) as f:
                raw = json.load(f).get("slots")
        except (OSError, ValueError, AttributeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def load(self, default_profile: str, known_profiles: set[str]) -> LimitState:
        """Restore the saved state; a missing, unreadable or unknown-profile file means a fresh start."""
        try:
            with open(self.path) as f:
                raw = json.load(f)
            st = LimitState(
                profile=raw["profile"],
                profile_previous=raw.get("profile_previous"),
                profile_expires=raw.get("profile_expires"),
                overrides=[Override(**o) for o in raw.get("overrides", [])],
                ceiling=Ceiling(**raw["ceiling"]) if raw.get("ceiling") else None,
                hold=Flag(**raw["hold"]) if raw.get("hold") else None,
                freeze=Flag(**raw["freeze"]) if raw.get("freeze") else None,
                next_id=raw.get("next_id", 1),
                profile_restricts=bool(raw.get("profile_restricts", False)),
                profile_previous_restricts=bool(raw.get("profile_previous_restricts", False)),
                profile_reason=raw.get("profile_reason"),
                profile_principal=raw.get("profile_principal"),
                profile_set_at=raw.get("profile_set_at"),
            )
        except FileNotFoundError:
            return LimitState(default_profile)
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)
            return LimitState(default_profile)
        used = {st.profile, st.profile_previous, st.ceiling.profile if st.ceiling else None} - {None}
        if not used <= known_profiles or any(o.param not in PARAMS for o in st.overrides):
            log.warning("ignoring %s: it names profiles or parameters that are not configured", self.path)
            return LimitState(default_profile)
        return st
