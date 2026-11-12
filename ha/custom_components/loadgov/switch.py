"""Hold switch (works today) and the instant-quiet freeze switch (needs daemon support)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import FEATURE_FREEZE
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([LoadgovHoldSwitch(entry.runtime_data), LoadgovFreezeSwitch(entry.runtime_data)])


class LoadgovHoldSwitch(LoadgovEntity, SwitchEntity):
    """Hold the heavy slot: no new heavy gates or implementers start until it is turned off."""

    _domain = "switch"

    _attr_icon = "mdi:hand-back-left"

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "load_gate_hold", "Host Load Gate Hold")

    @property
    def is_on(self) -> bool:
        """Only an owner hold: the drain profile's own hold is not this switch's to show or clear."""
        return dig(self.status, "slots.heavy.hold") is not None and self._origin != "profile"

    @property
    def _origin(self) -> str | None:
        return (
            dig(self.status, "slots.heavy.hold_origin") or "owner"
        )  # an older daemon only knows owner holds

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "profile_hold": dig(self.status, "slots.heavy.hold") is not None and self._origin == "profile"
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_post("/v1/hold", {"on": True, "slot": "heavy"})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_post("/v1/hold", {"on": False, "slot": "heavy"})


class LoadgovFreezeSwitch(LoadgovEntity, SwitchEntity):
    """SIGSTOP of the build workers. Unavailable until the daemon reports the freeze feature."""

    _domain = "switch"

    _attr_icon = "mdi:snowflake"

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "instant_quiet_freeze", "Host Instant Quiet Freeze")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.supports_freeze

    @property
    def is_on(self) -> bool:
        return dig(self.status, "limits.freeze") is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Endpoint per docs/protocol.md; unverified until the daemon supports it (unavailable before that).
        self.coordinator.require(FEATURE_FREEZE)
        await self.coordinator.async_post("/v1/freeze")

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.require(FEATURE_FREEZE)
        await self.coordinator.async_post("/v1/unfreeze")
