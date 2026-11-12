"""One DataUpdateCoordinator per config entry: GET /v1/status every 10 s."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    LoadgovApi,
    LoadgovAuthError,
    LoadgovConnectionError,
    LoadgovError,
    LoadgovRefused,
    LoadgovSourceError,
    LoadgovUnavailable,
)
from .const import DOMAIN, FEATURE_FREEZE, FEATURE_REAP, HA_REASON, REQUIRES_FREEZE, SCAN_INTERVAL_S

if TYPE_CHECKING:
    from .triggers import Triggers

_LOGGER = logging.getLogger(__name__)

type LoadgovConfigEntry = ConfigEntry[LoadgovCoordinator]


class LoadgovCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    config_entry: LoadgovConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LoadgovConfigEntry, api: LoadgovApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_S),
        )
        self.api = api
        self.triggers: Triggers | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.status()
        except LoadgovAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LoadgovSourceError as err:  # not a token problem: no reauth, the user must fix `[rest] allow`
            raise UpdateFailed(f"source address not allowed: {err}") from err
        except LoadgovError as err:  # connection, 503, anything else: entities go unavailable
            raise UpdateFailed(str(err)) from err

    # --- capabilities (what the running daemon can do) ---

    def supports(self, feature: str) -> bool:
        status = self.data or {}
        if feature in (status.get("capabilities") or []):
            return True
        return feature == FEATURE_FREEZE and "freeze" in (status.get("limits") or {})

    @property
    def supports_freeze(self) -> bool:
        return self.supports(FEATURE_FREEZE)

    @property
    def supports_reap(self) -> bool:
        return self.supports(FEATURE_REAP)

    def require(self, feature: str) -> None:
        """Raise unless the daemon supports `feature`: nothing is sent to an endpoint that may not exist."""
        if not self.supports(feature):
            raise HomeAssistantError(f"{REQUIRES_FREEZE} ({feature} is not supported by this daemon)")

    # --- writes ---

    async def async_post(self, path: str, body: dict | None = None) -> dict[str, Any]:
        """POST, then refresh. A refusal raises HomeAssistantError carrying the daemon's reason."""
        body = {"reason": HA_REASON, **(body or {})}
        try:
            reply = await self.api.post(path, body)
        except LoadgovRefused as err:
            raise HomeAssistantError(f"loadgovd refused: {err}") from err
        except LoadgovSourceError as err:
            raise HomeAssistantError(f"source address not allowed: {err}") from err
        except LoadgovAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"loadgovd rejected the token: {err}") from err
        except LoadgovUnavailable as err:
            raise HomeAssistantError(f"loadgovd is not ready yet, try again later: {err}") from err
        except LoadgovConnectionError as err:
            raise HomeAssistantError(str(err)) from err
        except LoadgovError as err:
            raise HomeAssistantError(f"loadgovd error: {err}") from err
        await self.async_request_refresh()
        return reply
