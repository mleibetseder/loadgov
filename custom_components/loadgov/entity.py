"""Base entity: one device "Build Host Load Governor" per config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import LoadgovCoordinator


def dig(data: dict[str, Any] | None, path: str, default: Any = None) -> Any:
    """`dig(status, "cpu.busy_pct")`: a dotted path into the status, `default` when absent or null."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return default if cur is None else cur


def image_settings(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """The status `image` object when the daemon has the run-time image settings (it reports `image.source`),
    else None: an older daemon has the train but would reject the new `set` fields."""
    image = (data or {}).get("image")
    return image if isinstance(image, dict) and "source" in image else None


class LoadgovEntity(CoordinatorEntity[LoadgovCoordinator]):
    """Entity ids are fixed by the design (`select.host_load_profile`, ...), so `has_entity_name` is off
    and `_attr_name` is the full name."""

    _attr_has_entity_name = False
    _domain: str

    def __init__(self, coordinator: LoadgovCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._key = key
        self._attr_name = name
        # Suggest the entity id from the name (select.host_load_profile); without
        # this the registry would prefix the device name.
        self.entity_id = f"{self._domain}.host_{key}"
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=NAME,
            manufacturer="loadgov",
            model="loadgovd",
            sw_version=dig(coordinator.data, "daemon.version"),
            serial_number=dig(coordinator.data, "host.id"),  # a note only: the identifier stays the entry id
        )

    @property
    def status(self) -> dict[str, Any]:
        return self.coordinator.data or {}
