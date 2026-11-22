"""Sensors. "Last change by" is omitted: the status does not expose the audit log."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from homeassistant.util import dt as dt_util

from .const import FEATURE_FREEZE, FEATURE_REAP, LEVELS, PROFILE_LABELS, REQUIRES_FREEZE
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
        key="load_1min", name_text="Host Load 1 min", value_fn=lambda s: dig(s, "cpu.load1"),
        icon="mdi:gauge", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
    ),
    LoadgovSensorDescription(
        key="load_5min", name_text="Host Load 5 min", value_fn=lambda s: dig(s, "cpu.load5"),
        icon="mdi:gauge", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
    ),
    LoadgovSensorDescription(
        key="load_per_core", name_text="Host Load per Core", value_fn=lambda s: dig(s, "cpu.load1_per_core"),
        icon="mdi:gauge", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
    ),
    LoadgovSensorDescription(
        key="level", name_text="Host Level", value_fn=lambda s: dig(s, "level"),
        icon="mdi:traffic-light", device_class=SensorDeviceClass.ENUM, options=list(LEVELS),
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
            LoadgovStraysSensor(entry.runtime_data),
            LoadgovLightLaneSensor(entry.runtime_data),
            LoadgovDiskUsedSensor(entry.runtime_data),
            LoadgovImageTrainSensor(entry.runtime_data),
            LoadgovImageTrainNextStartSensor(entry.runtime_data),
            *(LoadgovSlotNextSensor(entry.runtime_data, slot) for slot in SLOTS),
            *(LoadgovSlotHolderEtaSensor(entry.runtime_data, slot) for slot in SLOTS),
            LoadgovRunningJobsSensor(entry.runtime_data),
        ]
    )

    # Per-project compute: one CPU and one memory sensor per project name the status has ever shown,
    # created when the project first appears. A project that leaves the status goes unavailable.
    coordinator = entry.runtime_data
    known: set[str] = set()

    def _add_projects() -> None:
        new = [n for n in _projects(coordinator.data or {}) if n not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(
            [
                cls(coordinator, n)
                for n in new
                for cls in (LoadgovProjectCpuSensor, LoadgovProjectMemorySensor)
            ]
        )

    _add_projects()
    entry.async_on_unload(coordinator.async_add_listener(_add_projects))


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


STRAY_ATTRS = ("pid", "start_time", "comm", "project", "cwd", "cpu_pct", "age_s")
STRAY_MAX_ITEMS = 10


class LoadgovStraysSensor(LoadgovEntity, SensorEntity):
    """Stray processes the daemon reports: the state is their number. Unavailable when the daemon is older
    and has no `strays` in its status."""

    _domain = "sensor"
    _attr_icon = "mdi:ghost"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "strays", "Stray processes")

    @property
    def _strays(self) -> dict[str, Any] | None:
        strays = self.status.get("strays")
        return strays if isinstance(strays, dict) else None

    @property
    def available(self) -> bool:
        return super().available and self._strays is not None

    @property
    def native_value(self) -> int | None:
        strays = self._strays
        return None if strays is None else len(strays.get("items") or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        strays = self._strays
        if strays is None:
            return None
        items = [
            {**{k: i.get(k) for k in STRAY_ATTRS}, "session_id": (i.get("session") or {}).get("session_id")}
            for i in (strays.get("items") or [])[:STRAY_MAX_ITEMS]
        ]
        return {"items": items, "enforce": strays.get("enforce")}


LIGHT_ATTRS = ("max", "per_project_max", "cpu_quota_pct", "memory_max_mb", "max_runtime_s")


class LoadgovLightLaneSensor(LoadgovEntity, SensorEntity):
    """The light lane: the state is the number of running light jobs. Unavailable when the daemon is older
    and has no `light` in its status."""

    _domain = "sensor"
    _attr_icon = "mdi:feather"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "light_lane", "Host Light Lane")

    @property
    def _light(self) -> dict[str, Any] | None:
        light = self.status.get("light")
        return light if isinstance(light, dict) else None

    @property
    def available(self) -> bool:
        return super().available and self._light is not None

    @property
    def native_value(self) -> Any:
        light = self._light
        return None if light is None else light.get("running")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        light = self._light
        return None if light is None else {k: light.get(k) for k in LIGHT_ATTRS}


class LoadgovDiskUsedSensor(LoadgovEntity, SensorEntity):
    """Used % of the worst watched path (`disk.used_pct`); the attribute `disk_alert` is the level of the
    disk alert (`levels.disk_alert`). Unavailable on a daemon without `disk.used_pct`."""

    _domain = "sensor"
    _attr_icon = "mdi:harddisk"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "disk_used", "Host Disk Used")

    @property
    def _used(self) -> int | float | None:
        used = dig(self.status, "disk.used_pct")
        return used if isinstance(used, int | float) and not isinstance(used, bool) else None

    @property
    def available(self) -> bool:
        return super().available and self._used is not None

    @property
    def native_value(self) -> int | float | None:
        return self._used

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"disk_alert": dig(self.status, "levels.disk_alert")}


IMAGE_QUEUE_MAX_ITEMS = 10
IMAGE_ATTRS = (
    "id",
    "position",
    "project",
    "label",
    "key",
    "url",
    "channel",
    "planned_start",
    "planned_finish",
    "state",
)
STATE_MAX_LENGTH = 255
QUEUE_MAX_ITEMS = 10
JOBS_MAX_ITEMS = 10
SLOTS = ("heavy", "compile")
QUEUE_ATTRS = ("position", "project", "label", "kind", "key", "url", "wait_s", "eta")
JOB_ATTRS = ("kind", "project", "label", "key", "url", "slot", "since", "eta", "overdue")


def _timestamp(value: Any) -> datetime | None:
    """A UTC time string of the status as an aware datetime; None when absent or malformed."""
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


def _state_text(value: Any) -> str:
    """Free text as a state: Home Assistant rejects states over 255 characters."""
    text = str(value)
    return text if len(text) <= STATE_MAX_LENGTH else text[: STATE_MAX_LENGTH - 1] + "…"


def _list(value: Any) -> list[dict[str, Any]]:
    return [i for i in value if isinstance(i, dict)] if isinstance(value, list) else []


class LoadgovSlotNextSensor(LoadgovEntity, SensorEntity):
    """Head of a slot's queue: `<key or label>`, or `none` when nobody waits. Unavailable when the daemon
    is older and publishes no `waiters`. The list attribute is kept out of the recorder."""

    _domain = "sensor"
    _attr_icon = "mdi:format-list-numbered"
    _unrecorded_attributes = frozenset({"queue"})

    def __init__(self, coordinator: LoadgovCoordinator, slot: str) -> None:
        super().__init__(coordinator, f"{slot}_slot_next", f"Host {slot.capitalize()} Slot Next")
        self._slot = slot

    @property
    def _waiters(self) -> list[dict[str, Any]] | None:
        waiters = dig(self.status, f"slots.{self._slot}.waiters")
        if not isinstance(waiters, list):
            return None
        return sorted(_list(waiters), key=lambda w: w.get("position") or 0)

    @property
    def available(self) -> bool:
        return super().available and self._waiters is not None

    @property
    def native_value(self) -> str | None:
        waiters = self._waiters
        if waiters is None:
            return None
        if not waiters:
            return "none"
        return _state_text(waiters[0].get("key") or waiters[0].get("label") or "none")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        waiters = self._waiters
        if waiters is None:
            return None
        attrs: dict[str, Any] = {
            "count": len(waiters),
            "queue": [{k: w.get(k) for k in QUEUE_ATTRS} for w in waiters[:QUEUE_MAX_ITEMS]],
        }
        if waiters:
            attrs["project"] = waiters[0].get("project")
        return attrs


def _image(status: dict[str, Any]) -> dict[str, Any] | None:
    image = status.get("image")
    return image if isinstance(image, dict) else None


class LoadgovImageTrainSensor(LoadgovEntity, SensorEntity):
    """The image train: the state is the number of jobs in it, waiting plus the running one (the daemon's
    `image.queued` + `image.running`, the full counts; its `queue` list is cut to 20). Unavailable when the
    daemon is older and has no `image` in its status. The queue is kept out of the recorder."""

    _domain = "sensor"
    _attr_icon = "mdi:train"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"queue"})

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "image_train", "Host Image Train")

    @property
    def available(self) -> bool:
        return super().available and _image(self.status) is not None

    @property
    def _jobs(self) -> list[dict[str, Any]]:
        return _list((_image(self.status) or {}).get("queue"))

    @property
    def native_value(self) -> int | None:
        image = _image(self.status)
        if image is None:
            return None
        counts = [image.get("queued"), image.get("running")]
        if all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
            return sum(counts)
        return len(self._jobs)  # a daemon that reports no counts: what the list shows

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        image = _image(self.status)
        if image is None:
            return None
        return {
            **{k: image.get(k) for k in ("window", "timezone", "in_window", "next_window_start")},
            "queue": [{k: j.get(k) for k in IMAGE_ATTRS} for j in self._jobs[:IMAGE_QUEUE_MAX_ITEMS]],
        }


class LoadgovImageTrainNextStartSensor(LoadgovEntity, SensorEntity):
    """When the train runs next: the planned start of the first job not yet running, else the next window
    start. A running job has started already, so its planned start is skipped."""

    _domain = "sensor"
    _attr_icon = "mdi:train-variant"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "image_train_next_start", "Host Image Train Next Start")

    @property
    def native_value(self) -> datetime | None:
        image = _image(self.status)
        if image is None:
            return None
        head = next((j for j in _list(image.get("queue")) if j.get("state") != "running"), None)
        return (_timestamp(head.get("planned_start")) if head else None) or _timestamp(
            image.get("next_window_start")
        )


class LoadgovSlotHolderEtaSensor(LoadgovEntity, SensorEntity):
    """Expected finish of the job that holds a slot (`jobs[].eta`); no state while the slot is free or the
    daemon has no estimate. A past value together with `overdue` means the job runs longer than usual."""

    _domain = "sensor"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: LoadgovCoordinator, slot: str) -> None:
        super().__init__(coordinator, f"{slot}_slot_holder_eta", f"Host {slot.capitalize()} Slot Holder ETA")
        self._slot = slot

    @property
    def _job(self) -> dict[str, Any] | None:
        return next((j for j in _list(self.status.get("jobs")) if j.get("slot") == self._slot), None)

    @property
    def native_value(self) -> datetime | None:
        job = self._job
        return _timestamp(job.get("eta")) if job else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        job = self._job
        if job is None:
            return None
        return {k: job.get(k) for k in ("key", "url", "project", "label", "kind", "expected_s", "overdue")}


class LoadgovRunningJobsSensor(LoadgovEntity, SensorEntity):
    """Jobs `loadctl run` has started and that still run: the state is their number."""

    _domain = "sensor"
    _attr_icon = "mdi:run-fast"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"jobs"})

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "running_jobs", "Host Running Jobs")

    @property
    def native_value(self) -> int:
        return len(_list(self.status.get("jobs")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        jobs = _list(self.status.get("jobs"))
        return {"jobs": [{k: j.get(k) for k in JOB_ATTRS} for j in jobs[:JOBS_MAX_ITEMS]]}


def _projects(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    projects = status.get("projects")
    if not isinstance(projects, dict):
        return {}
    return {str(n): (b if isinstance(b, dict) else {}) for n, b in projects.items()}


class _LoadgovProjectSensor(LoadgovEntity, SensorEntity):
    """Base of the per-project sensors. The unique id carries the project name, so it survives restarts;
    the entity id is a slug of it. Unavailable while the status has no such project."""

    _domain = "sensor"
    _suffix: str
    _label: str

    def __init__(self, coordinator: LoadgovCoordinator, project: str) -> None:
        slug = re.sub(r"[^a-z0-9]+", "_", project.lower()).strip("_") or "project"
        super().__init__(
            coordinator, f"project_{slug}_{self._suffix}", f"Host Project {project} {self._label}"
        )
        self._project = project
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_project_{project}_{self._suffix}"

    @property
    def _body(self) -> dict[str, Any] | None:
        return _projects(self.status).get(self._project)

    @property
    def available(self) -> bool:
        return super().available and self._body is not None


class LoadgovProjectCpuSensor(_LoadgovProjectSensor):
    _suffix = "cpu"
    _label = "CPU"
    _attr_icon = "mdi:cpu-64-bit"
    _attr_native_unit_of_measurement = "cores"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        cores = (self._body or {}).get("cores")
        return round(cores, 2) if isinstance(cores, int | float) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        body = self._body
        if body is None:
            return None
        return {k: body.get(k) for k in ("cpu_pct", "docker_cpu_pct", "jobs", "waiting")}


class LoadgovProjectMemorySensor(_LoadgovProjectSensor):
    _suffix = "memory"
    _label = "Memory"
    _attr_icon = "mdi:memory"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | float | None:
        mem = (self._body or {}).get("mem_mb")
        return mem if isinstance(mem, int | float) else None
