"""Generic HA entities built from a device's declarative Entity descriptors.

EXPERIMENTAL / UNVERIFIED - see experimental.py.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN
from .experimental import Entity, ExperimentalDeviceHub


def _device_info(hub: ExperimentalDeviceHub) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, hub.serial)},
        name=f"AIPAI {hub.spec.name} {hub.serial}",
        manufacturer="AIPAI (Doseen)",
        model=f"{hub.spec.name} (experimental)",
    )


def build_entities(hub: ExperimentalDeviceHub, kind: str) -> list[Any]:
    classes = {
        "switch": ExpSwitch,
        "number": ExpNumber,
        "sensor": ExpSensor,
        "button": ExpButton,
        "select": ExpSelect,
    }
    cls = classes[kind]
    return [cls(hub, e) for e in hub.spec.entities if e.kind == kind]


class _ExpBase:
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hub: ExperimentalDeviceHub, desc: Entity) -> None:
        self._hub = hub
        self._desc = desc
        self._attr_name = desc.name
        self._attr_unique_id = f"{hub.serial}_{desc.key}"
        self._attr_device_info = _device_info(hub)
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available

    def _value(self) -> Any:
        return self._hub.state.get(self._desc.field) if self._desc.field else None


class ExpSwitch(_ExpBase, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return self._value() in self._desc.on_values

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._hub.send(self._desc.on_cmd)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._hub.send(self._desc.off_cmd)


class ExpNumber(_ExpBase, NumberEntity):
    def __init__(self, hub: ExperimentalDeviceHub, desc: Entity) -> None:
        super().__init__(hub, desc)
        self._attr_native_min_value = desc.minimum
        self._attr_native_max_value = desc.maximum
        self._attr_native_step = desc.step
        self._attr_native_unit_of_measurement = desc.unit

    @property
    def native_value(self) -> float | None:
        v = self._value()
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        payload = value * self._desc.cmd_scale
        payload = int(payload) if payload == int(payload) else payload
        self._hub.set_field(self._desc.cmd_type, self._desc.cmd_field, payload, self._desc.cmd_extra)


class ExpSensor(_ExpBase, SensorEntity):
    def __init__(self, hub: ExperimentalDeviceHub, desc: Entity) -> None:
        super().__init__(hub, desc)
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class = desc.device_class

    @property
    def native_value(self) -> Any:
        return self._value()


class ExpButton(_ExpBase, ButtonEntity):
    async def async_press(self) -> None:
        self._hub.send(self._desc.press_cmd)


class ExpSelect(_ExpBase, SelectEntity):
    def __init__(self, hub: ExperimentalDeviceHub, desc: Entity) -> None:
        super().__init__(hub, desc)
        self._attr_options = list(desc.options.keys())

    @property
    def current_option(self) -> str | None:
        v = self._value()
        for label, raw in self._desc.options.items():
            if raw == v:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        raw = self._desc.options.get(option)
        if raw is not None:
            self._hub.set_field(self._desc.cmd_type, self._desc.cmd_field, raw)
