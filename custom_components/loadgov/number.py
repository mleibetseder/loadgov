"""The six tunables as numbers; values come from `limits.effective`."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import IMAGE_BETA_INTERVAL_MAX_H
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig, image_settings

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
        # the daemon accepts 0-8 (config.RANGES); parallel light jobs, host-wide.
        key="max_light", param="max_light", name_text="Host Max Light Jobs",
        native_min_value=0, native_max_value=8, native_step=1, icon="mdi:feather",
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
    async_add_entities([LoadgovImageBetaIntervalNumber(entry.runtime_data)])


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
        sent = int(value) if self._param in ("max_implementers", "max_light") else float(value)
        await self.coordinator.async_post("/v1/set", {self._param: sent})


class LoadgovImageBetaIntervalNumber(LoadgovEntity, NumberEntity):
    """Hours between two beta image builds of a project (0 = no interval), `image.beta_interval_h`.
    Unavailable on a daemon without the run-time image settings."""

    _domain = "number"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = IMAGE_BETA_INTERVAL_MAX_H
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon = "mdi:timer-sand"

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "image_beta_interval", "Host Image Beta Interval")

    @property
    def available(self) -> bool:
        return super().available and image_settings(self.coordinator.data) is not None

    @property
    def native_value(self) -> float | None:
        value = (image_settings(self.status) or {}).get("beta_interval_h")
        return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_image({"image_beta_interval_h": int(value)})
