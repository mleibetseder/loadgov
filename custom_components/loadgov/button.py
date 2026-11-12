"""Buttons that need daemon support (reap, unfreeze): unavailable until the daemon has it."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities(
        [
            LoadgovButton(
                c, "kill_build_daemons", "Host Kill Build Daemons", "/v1/reap", "reap", "mdi:skull"
            ),
            LoadgovButton(
                c, "resume_paused_workers", "Host Resume Paused Workers", "/v1/unfreeze", "freeze", "mdi:play"
            ),
        ]
    )


class LoadgovButton(LoadgovEntity, ButtonEntity):
    _domain = "button"

    def __init__(
        self, coordinator: LoadgovCoordinator, key: str, name: str, path: str, feature: str, icon: str
    ) -> None:
        super().__init__(coordinator, key, name)
        self._path, self._feature, self._attr_icon = path, feature, icon

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.supports(self._feature)

    async def async_press(self) -> None:
        self.coordinator.require(self._feature)
        await self.coordinator.async_post(self._path)
