"""Text entities for the run-time image-train settings: the window (HH:MM-HH:MM) and the timezone."""

from __future__ import annotations

import re

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import IMAGE_WINDOW_PATTERN
from .coordinator import LoadgovConfigEntry, LoadgovCoordinator
from .entity import LoadgovEntity, image_settings

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: LoadgovConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    async_add_entities([LoadgovImageWindowText(c), LoadgovImageTimezoneText(c)])


class _ImageText(LoadgovEntity, TextEntity):
    _domain = "text"
    _attr_mode = TextMode.TEXT
    _field: str  # the key in status `image` and the suffix of the /v1/set field `image_<field>`

    @property
    def available(self) -> bool:
        return super().available and image_settings(self.coordinator.data) is not None

    @property
    def native_value(self) -> str | None:
        value = (image_settings(self.status) or {}).get(self._field)
        return value if isinstance(value, str) and value else None

    def _check(self, value: str) -> None:
        """Raise ServiceValidationError before anything is sent."""

    async def async_set_value(self, value: str) -> None:
        value = value.strip()
        self._check(value)
        await self.coordinator.async_set_image({f"image_{self._field}": value})


class LoadgovImageWindowText(_ImageText):
    """One entity for the whole window, not a start and an end time: the daemon takes `HH:MM-HH:MM` as one value
    (it may cross midnight; start == end is refused), so one write is atomic, while two time entities would need
    two writes or a read-modify-write that can leave a half-changed window."""

    _field = "window"
    _attr_icon = "mdi:clock-time-eight-outline"
    _attr_native_min = 11
    _attr_native_max = 11
    _attr_pattern = IMAGE_WINDOW_PATTERN

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "image_window", "Host Image Window")

    def _check(self, value: str) -> None:
        if not re.fullmatch(IMAGE_WINDOW_PATTERN, value):
            raise ServiceValidationError(
                f"image window must look like 00:00-06:00 (HH:MM-HH:MM, 24 h), got {value!r}"
            )
        start, end = value.split("-")
        if start == end:
            raise ServiceValidationError("image window start and end must differ")


class LoadgovImageTimezoneText(_ImageText):
    """An IANA name such as America/New_York. The daemon is the judge of the name (its own tzdata decides), so
    only an empty value is refused here; a bad name comes back as the daemon's one-line message."""

    _field = "timezone"
    _attr_icon = "mdi:earth"
    _attr_native_min = 1
    _attr_native_max = 64

    def __init__(self, coordinator: LoadgovCoordinator) -> None:
        super().__init__(coordinator, "image_timezone", "Host Image Timezone")

    def _check(self, value: str) -> None:
        if not value:
            raise ServiceValidationError("image timezone must be an IANA name such as America/New_York")
