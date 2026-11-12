"""The five tunables as numbers; values come from `limits.effective`."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class LoadgovNumberDescription(NumberEntityDescription):
    param: str  # the REST /v1/set field and the key in limits.effective
    name_text: str


NUMBERS = (
    LoadgovNumberDescription(
        key="max_cores", param="max_cores", name_text="Host Max Cores",
        native_min_value=1.0, native_max_value=6.0, native_step=0.5, icon="mdi:cpu-64-bit",
    ),
    LoadgovNumberDescription(
        key="target_cpu_pct", param="target_cpu_pct", name_text="Host Target CPU",
        native_min_value=35, native_max_value=90, native_step=5,
        native_unit_of_measurement=PERCENTAGE, icon="mdi:cpu-64-bit",
    ),
    LoadgovNumberDescription(
        key="load_max", param="load_max", name_text="Host Load Max",
        native_min_value=1.0, native_max_value=6.0, native_step=0.5, icon="mdi:gauge",
    ),
    LoadgovNumberDescription(
        key="max_implementers", param="max_implementers", name_text="Host Max Implementers",
        native_min_value=0, native_max_value=4, native_step=1, icon="mdi:account-hard-hat",
    ),
    LoadgovNumberDescription(
        # the daemon accepts 2.0-6.0 (config.RANGES); the entity mirrors that range.
        key="ram_floor_gb", param="ram_floor_gb", name_text="Host RAM Floor",
        native_min_value=2.0, native_max_value=6.0, native_step=0.5,
        native_unit_of_measurement=UnitOfInformation.GIBIBYTES, icon="mdi:memory",
    ),
)  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(LoadgovNumber(entry.runtime_data, d) for d in NUMBERS)


class LoadgovNumber(LoadgovEntity, NumberEntity):
    _domain = "number"
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: LoadgovCoordinator, description: LoadgovNumberDescription) -> None:
        super().__init__(coordinator, description.key, description.name_text)
        self.entity_description = description
        self._attr_name = description.name_text
        self._param = description.param

    @property
    def native_value(self) -> float | None:
        value = dig(self.status, f"limits.effective.{self._param}")
        return float(value) if isinstance(value, int | float) else None

    async def async_set_native_value(self, value: float) -> None:
        sent = int(value) if self._param == "max_implementers" else float(value)
        await self.coordinator.async_post("/v1/set", {self._param: sent})
