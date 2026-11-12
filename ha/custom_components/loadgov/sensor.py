"""Sensors. "Last change by" is omitted: the status does not expose the audit log."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import FEATURE_FREEZE, FEATURE_REAP, PROFILE_LABELS, REQUIRES_FREEZE
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, dig

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class LoadgovSensorDescription(SensorEntityDescription):
    name_text: str
    value_fn: Callable[[dict[str, Any]], Any]


def _label(path: str) -> Callable[[dict[str, Any]], Any]:
    def fn(s: dict[str, Any]) -> Any:
        name = dig(s, path)
        return PROFILE_LABELS.get(name, name) if name else None

    return fn


def _cap(group: str) -> Callable[[dict[str, Any]], Any]:
    """Core cap of a watched cgroup (None when unlimited or unreadable); names contain dots."""
    return lambda s: ((dig(s, "cgroups", {}) or {}).get(group) or {}).get("cores")


def _mirror_cap(s: dict[str, Any]) -> Any:
    """Core cap of the first mirrored cgroup ([compat] mirror_cgroups); None when none is configured."""
    for name, body in (dig(s, "cgroups", {}) or {}).items():
        if name != "build-work.slice":
            return (body or {}).get("cores")
    return None


_PCT = {"native_unit_of_measurement": PERCENTAGE, "state_class": SensorStateClass.MEASUREMENT}

SENSORS = (
    LoadgovSensorDescription(
        key="busy_pct", name_text="Host Busy", icon="mdi:cpu-64-bit", **_PCT,
        value_fn=lambda s: dig(s, "cpu.busy_pct"),
    ),
    LoadgovSensorDescription(
        key="cpu_psi", name_text="Host CPU Pressure", value_fn=lambda s: dig(s, "cpu.psi_some_avg60"),
        icon="mdi:gauge", entity_category=EntityCategory.DIAGNOSTIC, **_PCT,
    ),
    LoadgovSensorDescription(
        key="memory_psi", name_text="Host Memory Pressure",
        value_fn=lambda s: dig(s, "memory.psi_some_avg60"),
        icon="mdi:memory", entity_category=EntityCategory.DIAGNOSTIC, **_PCT,
    ),
    LoadgovSensorDescription(
        key="io_psi", name_text="Host IO Pressure", value_fn=lambda s: dig(s, "io.psi_some_avg60"),
        icon="mdi:harddisk", entity_category=EntityCategory.DIAGNOSTIC, **_PCT,
    ),
    LoadgovSensorDescription(
        key="mem_available", name_text="Host Memory Available", value_fn=lambda s: dig(s, "memory.avail_mb"),
        device_class=SensorDeviceClass.DATA_SIZE, native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LoadgovSensorDescription(
        key="swap_used", name_text="Host Swap Used", value_fn=lambda s: dig(s, "swap.used_mb"),
        device_class=SensorDeviceClass.DATA_SIZE, native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LoadgovSensorDescription(
        key="cap_cores_build_work", name_text="Host Cap Cores Build Work",
        value_fn=_cap("build-work.slice"), icon="mdi:cpu-64-bit",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LoadgovSensorDescription(
        key="cap_cores_mirror", name_text="Host Cap Cores Mirror",
        value_fn=_mirror_cap, icon="mdi:cpu-64-bit",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    LoadgovSensorDescription(
        key="effective_profile", name_text="Host Effective Profile", value_fn=_label("limits.profile"),
        icon="mdi:speedometer",
    ),
    LoadgovSensorDescription(
        key="owner_ceiling", name_text="Host Owner Ceiling", value_fn=_label("limits.ceiling"),
        icon="mdi:arrow-collapse-down",
    ),
    LoadgovSensorDescription(
        key="heavy_holder", name_text="Host Heavy Slot Holder",
        value_fn=lambda s: dig(s, "slots.heavy.holder"),
        icon="mdi:lock",
    ),
    LoadgovSensorDescription(
        key="paused_workers", name_text="Host Paused Workers", value_fn=lambda s: len(dig(s, "stopped", [])),
        icon="mdi:pause-circle", state_class=SensorStateClass.MEASUREMENT,
    ),
)  # fmt: skip


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities(
        [
            *(LoadgovSensor(entry.runtime_data, d) for d in SENSORS),
            LoadgovFeaturesSensor(entry.runtime_data),
            LoadgovTriggersSensor(entry.runtime_data),
        ]
    )


class LoadgovFeaturesSensor(LoadgovEntity, SensorEntity):
    """Which daemon features are on. Home Assistant drops the attributes of unavailable entities, so the
    note "requires a loadgovd with freeze support" for freeze, reap and unfreeze lives here."""

    _domain = "sensor"
    _attr_icon = "mdi:feature-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "daemon_features", "Host Daemon Features")

    @property
    def native_value(self) -> str:
        on = [f for f in (FEATURE_FREEZE, FEATURE_REAP) if self.coordinator.supports(f)]
        return ", ".join(on) if on else "none"

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {
            f: "supported" if self.coordinator.supports(f) else REQUIRES_FREEZE
            for f in (FEATURE_FREEZE, FEATURE_REAP)
        }


class LoadgovSensor(LoadgovEntity, SensorEntity):
    _domain = "sensor"

    def __init__(self, coordinator: LoadgovCoordinator, description: LoadgovSensorDescription) -> None:
        super().__init__(coordinator, description.key, description.name_text)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.status)


class LoadgovTriggersSensor(LoadgovEntity, SensorEntity):
    """State of the smart triggers: the state is the number of active ones, the attributes list each
    trigger's enabled, active, last_action and last_error. No tokens or secrets."""

    _domain = "sensor"
    _attr_icon = "mdi:robot"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "triggers", "Host Smart Triggers")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.triggers is not None:
            self.async_on_remove(self.coordinator.triggers.async_add_listener(self.async_write_ha_state))

    @property
    def _info(self) -> dict[str, dict[str, Any]]:
        return self.coordinator.triggers.info() if self.coordinator.triggers else {}

    @property
    def native_value(self) -> int:
        return sum(1 for t in self._info.values() if t["enabled"] and t["active"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._info
