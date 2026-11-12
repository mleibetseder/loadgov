"""Fixtures: a canned loadgovd status and a mocked REST API. Nothing here leaves the process."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.loadgov.const import DOMAIN

HOST, PORT, TOKEN = "192.0.2.10", 7420, "t" * 43
BASE = f"http://{HOST}:{PORT}"

STATUS: dict[str, Any] = {
    "v": 1,
    "at": "2026-11-10T10:00:00Z",
    "cpu": {"busy_pct": 42.5, "psi_some_avg60": 1.5},
    "memory": {"avail_mb": 9000, "psi_some_avg60": 0.5},
    "swap": {"used_mb": 120},
    "io": {"psi_some_avg60": 0.25},
    "cgroups": {
        "build-work.slice": {"cpu_max": "400000 100000", "cores": 4.0},
        "mirror-a": {"cpu_max": "max 100000", "cores": None},
    },
    "stopped": [],
    "slots": {
        "heavy": {"state": "free", "holder": None, "hold": None},
        "compile": {"state": "free", "holder": None, "hold": None},
    },
    "limits": {
        "profile": "balanced",
        "ceiling": None,
        "restricted": False,
        "restricted_by": None,
        "ceiling_info": None,
        "profile_info": None,
        "overrides": [],
        "effective": {
            "max_cores": 4.0,
            "target_cpu_pct": 70.0,
            "load_max": 4.0,
            "max_implementers": 2,
            "ram_floor_gb": 4.0,
        },
    },
    "daemon": {"version": "0.0.1"},
}


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    return


@pytest.fixture
def status() -> dict[str, Any]:
    return copy.deepcopy(STATUS)


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Build Host Load Governor",
        unique_id=f"{HOST}:{PORT}",
        data={"host": HOST, "port": PORT, "token": TOKEN},
    )


def mock_status(mocker: AiohttpClientMocker, status: dict[str, Any], **kwargs) -> None:
    mocker.get(f"{BASE}/v1/status", json={"ok": True, "status": status}, **kwargs)


def posts(mocker: AiohttpClientMocker) -> list[tuple[str, Any]]:
    """(path, json body) of every POST sent so far."""
    return [(str(u).removeprefix(BASE), d) for m, u, d, _ in mocker.mock_calls if m == "POST"]
