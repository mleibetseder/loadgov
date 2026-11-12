"""Binary sensors: workers paused, restriction active, hold active."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig

PARALLEL_UPDATES = 0


def _hold(s: dict[str, Any]) -> bool:
    return any(dig(s, f"slots.{slot}.hold") is not None for slot in ("heavy", "compile"))


BINARY_SENSORS: tuple[tuple[str, str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("workers_paused", "Host Workers Paused", "mdi:pause-circle", lambda s: bool(dig(s, "stopped", []))),
    ("restriction_active", "Host Restriction Active", "mdi:hand-back-left",
     lambda s: dig(s, "limits.restricted", False) is True),
    ("hold_active", "Host Hold Active", "mdi:hand-back-left", _hold),
)  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LoadgovBinarySensor(entry.runtime_data, *d) for d in BINARY_SENSORS)


class LoadgovBinarySensor(LoadgovEntity, BinarySensorEntity):
    _domain = "binary_sensor"

    def __init__(
        self,
        coordinator: LoadgovCoordinator,
        key: str,
        name: str,
        icon: str,
        fn: Callable[[dict[str, Any]], bool],
    ) -> None:
        super().__init__(coordinator, key, name)
        self._attr_icon, self._fn = icon, fn

    @property
    def is_on(self) -> bool:
        return self._fn(self.status)
