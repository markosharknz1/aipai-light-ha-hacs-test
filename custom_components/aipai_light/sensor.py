"""Sensor platform: a light's schedule/state snapshot, plus experimental sensors.

The light schedule sensor is diagnostic (off the main dashboard). It exists so
the visual designer can read a light's *current* on-device schedule, mode and
moon settings via the HA REST API (`/api/states`) and pre-fill from them.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .experimental import ExperimentalDeviceHub
from .experimental_entities import build_entities
from .hub import AipaiLightHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    if isinstance(hub, AipaiLightHub):
        async_add_entities([AipaiTemperatureSensor(hub), AipaiScheduleSensor(hub)])
        hub.request_refresh()
    elif isinstance(hub, ExperimentalDeviceHub):
        async_add_entities(build_entities(hub, "sensor"))
        hub.request_refresh()


def _rows_to_curves(rows: list[str], roads: int) -> list[list[int]]:
    curves: list[list[int]] = []
    for i in range(roads):
        raw = rows[i] if i < len(rows) else ""
        vals = []
        for tok in raw.split(","):
            try:
                vals.append(int(float(tok)))
            except ValueError:
                vals.append(0)
        vals = (vals + [0] * 24)[:24]
        curves.append(vals)
    return curves


class AipaiTemperatureSensor(SensorEntity):
    """The fixture's reported temperature."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, hub: AipaiLightHub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{hub.serial}_temperature"
        self._attr_device_info = hub.device_info
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available and self._hub.state.temperature is not None

    @property
    def native_value(self) -> float | None:
        return self._hub.state.temperature


class AipaiScheduleSensor(SensorEntity):
    """Diagnostic snapshot of a light's schedule, mode and moon (for the designer)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Schedule"
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: AipaiLightHub) -> None:
        self._hub = hub
        self._attr_unique_id = f"{hub.serial}_schedule"
        self._attr_device_info = hub.device_info
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def native_value(self) -> str:
        return "scheduled" if self._hub.mode == "1" else "manual"

    def _friendly_name(self) -> str:
        """The device's effective name, so the card shows what the user renamed it
        to - whether that was set in the config flow or the HA device page."""
        try:
            device = dr.async_get(self.hass).async_get_device(
                identifiers={(DOMAIN, self._hub.serial)}
            )
            if device:
                return device.name_by_user or device.name or self._hub.name
        except Exception:  # noqa: BLE001
            pass
        return self._hub.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = self._hub.state
        return {
            "aipai_kind": "schedule",   # lets the designer find this entity
            "aipai_serial": self._hub.serial,
            "aipai_name": self._friendly_name(),
            "model": st.model,
            "roads": self._hub.roads,
            "labels": self._hub.labels,
            "mode": st.mode,
            "open_hour": st.open_hour,
            "close_hour": st.close_hour,
            "timezone": st.timezone,
            "temperature": st.temperature,
            "curves": _rows_to_curves(st.road_data, self._hub.roads),
            "moon": self._hub.moon,
            "last_command": self._hub.last_ack,
            "off_until": self._hub.off_until.isoformat() if self._hub.off_until else None,
        }
