"""Select platform: light control-mode, plus experimental-device selects."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .experimental import ExperimentalDeviceHub
from .experimental_entities import build_entities
from .hub import AipaiLightHub

# Human labels <-> the device's DeviceMode field.
_MODE_LABELS = {"Manual": "0", "Scheduled (sunrise/sunset)": "1"}
_MODE_BY_VALUE = {v: k for k, v in _MODE_LABELS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    if isinstance(hub, AipaiLightHub):
        async_add_entities([AipaiModeSelect(hub)])
    elif isinstance(hub, ExperimentalDeviceHub):
        async_add_entities(build_entities(hub, "select"))


class AipaiModeSelect(SelectEntity):
    """Switch a fixture between manual levels and its stored day schedule."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Control mode"
    _attr_icon = "mdi:calendar-clock"
    _attr_options = list(_MODE_LABELS)

    def __init__(self, hub: AipaiLightHub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{hub.serial}_mode"
        self._attr_device_info = hub.device_info
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available and self._hub.has_state

    @property
    def current_option(self) -> str | None:
        return _MODE_BY_VALUE.get(self._hub.mode)

    async def async_select_option(self, option: str) -> None:
        value = _MODE_LABELS.get(option)
        if value is not None:
            self._hub.set_mode(value)
