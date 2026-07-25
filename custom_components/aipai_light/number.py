"""Number platform.

For lights: one 0-100 % level control per spectral channel (these replace the
old per-channel `light` entities so the fixture never floods the `light`
domain / "Lights" summary). For experimental devices: the declarative numbers.
"""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHANNEL_CMD_MAX, DOMAIN
from .experimental import ExperimentalDeviceHub
from .experimental_entities import build_entities
from .hub import AipaiLightHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    if isinstance(hub, AipaiLightHub):
        async_add_entities([AipaiChannelNumber(hub, i) for i in range(hub.roads)])
        hub.request_refresh()
    elif isinstance(hub, ExperimentalDeviceHub):
        async_add_entities(build_entities(hub, "number"))


class AipaiChannelNumber(NumberEntity):
    """A single spectral channel as a 0-100 % level slider."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:brightness-6"

    def __init__(self, hub: AipaiLightHub, index: int) -> None:
        self._hub = hub
        self._index = index
        self._attr_unique_id = f"{hub.serial}_ch{index}"
        self._attr_device_info = hub.device_info
        hub.register_entity(self)

    @property
    def name(self) -> str:
        labels = self._hub.labels
        return labels[self._index] if self._index < len(labels) else f"Channel {self._index + 1}"

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def native_value(self) -> float:
        cmd = self._hub.channels[self._index] if self._index < len(self._hub.channels) else 0
        return round(cmd / CHANNEL_CMD_MAX * 100)

    async def async_set_native_value(self, value: float) -> None:
        self._hub.set_channel(self._index, round(value / 100 * CHANNEL_CMD_MAX))
