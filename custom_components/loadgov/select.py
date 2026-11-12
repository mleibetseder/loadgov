"""select.host_load_profile."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PROFILE_LABELS
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([LoadgovProfileSelect(entry.runtime_data)])


class LoadgovProfileSelect(LoadgovEntity, SelectEntity):
    _domain = "select"
    _attr_icon = "mdi:speedometer"

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "load_profile", "Host Load Profile")

    def _names(self) -> list[str]:
        # Profile names come from the status when the daemon lists them, else the four built-ins.
        listed = dig(self.status, "limits.profiles")
        return list(listed) if isinstance(listed, list) and listed else list(PROFILE_LABELS)

    @staticmethod
    def _label(name: str) -> str:
        return PROFILE_LABELS.get(name, name.title())

    @property
    def options(self) -> list[str]:
        return [self._label(n) for n in self._names()]

    @property
    def current_option(self) -> str | None:
        name = dig(self.status, "limits.profile")
        return self._label(name) if name else None

    async def async_select_option(self, option: str) -> None:
        name = next((n for n in self._names() if self._label(n) == option), None)
        if name is None:
            raise ValueError(f"unknown profile {option!r}")
        await self.coordinator.async_post("/v1/set", {"profile": name})
