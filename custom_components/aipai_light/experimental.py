"""EXPERIMENTAL / UNVERIFIED support for the non-light AIPAI devices.

Everything in this module was transcribed from the decrypted vendor app,
NOT tested against real hardware. Command payloads are taken verbatim from
the app's send functions; the state field names are taken from how the app
reads device replies. Topic scheme, exact scaling, and some field meanings
still need confirmation on real devices.

Each device is described declaratively by a DeviceSpec + a list of Entity
descriptors, so a contributor with the hardware can fix a device by editing
one table rather than touching platform code.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .gen2_client import AipaiGen2Client

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = 45      # default seconds between readCfg polls


@dataclass
class Entity:
    kind: str                       # switch | number | sensor | button | select
    key: str
    name: str
    field: str | None = None        # state key in the device's readCfg reply
    # switch
    on_cmd: dict[str, Any] | None = None
    off_cmd: dict[str, Any] | None = None
    on_values: tuple = (True, 1, "on", "1")
    # number
    minimum: float = 0
    maximum: float = 100
    step: float = 1
    unit: str | None = None
    cmd_type: str | None = None
    cmd_field: str | None = None
    cmd_scale: float = 1
    cmd_extra: dict[str, Any] | None = None
    # sensor
    device_class: str | None = None
    # button
    press_cmd: dict[str, Any] | None = None
    # select
    options: dict[str, Any] | None = None   # label -> raw value


@dataclass
class DeviceSpec:
    key: str
    name: str
    entities: list[Entity] = field(default_factory=list)


def _switch(key, name, fld, on_cmd, off_cmd, on_values=(True, 1, "on", "1")):
    return Entity("switch", key, name, field=fld, on_cmd=on_cmd, off_cmd=off_cmd, on_values=on_values)


def _number(key, name, fld, cmd_type, cmd_field, *, minimum=0, maximum=100, step=1,
            unit=None, scale=1, extra=None):
    return Entity("number", key, name, field=fld, minimum=minimum, maximum=maximum,
                  step=step, unit=unit, cmd_type=cmd_type, cmd_field=cmd_field,
                  cmd_scale=scale, cmd_extra=extra)


def _sensor(key, name, fld, *, unit=None, device_class=None):
    return Entity("sensor", key, name, field=fld, unit=unit, device_class=device_class)


def _button(key, name, cmd):
    return Entity("button", key, name, press_cmd=cmd)


def _select(key, name, fld, cmd_type, cmd_field, options):
    return Entity("select", key, name, field=fld, cmd_type=cmd_type, cmd_field=cmd_field, options=options)


# --- Device registry --------------------------------------------------------
# Command payloads are verbatim from the app; see PROTOCOL.md for provenance.

DEVICE_SPECS: dict[str, DeviceSpec] = {
    "pump": DeviceSpec("pump", "Pump", [
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("level", "Speed", "level", "saveCfg", "level", unit="%"),
        _number("nlevel", "Night speed", "nlevel", "saveCfg", "nlevel", unit="%"),
        _select("mode", "Mode", "mode", "saveCfg", "mode",
                {"Feed": 0, "Constant flow": 1, "Wave": 2}),
    ]),
    "wave": DeviceSpec("wave", "Wave pump", [
        _switch("power", "Power", "power", {"type": "motorStart"}, {"type": "motorStop"}),
        # app sends stepLength = percent * 10, status: 1
        _number("speed", "Speed", "speed", "speedSet", "stepLength", unit="%",
                scale=10, extra={"status": 1}),
    ]),
    "plug": DeviceSpec("plug", "Smart plug", [
        _switch("state", "Power", "state", {"type": "setRelay", "state": "on"},
                {"type": "setRelay", "state": "off"}),
    ]),
    "fan": DeviceSpec("fan", "Cooling fan", [
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("level", "Speed", "level", "saveCfg", "level", unit="%"),
    ]),
    "chiller": DeviceSpec("chiller", "Chiller", [
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("level", "Setpoint", "level", "saveCfg", "level", minimum=0, maximum=40,
                step=0.5, unit="°C"),
        _number("tempAdds", "Calibration", "tempAdds", "saveCfg", "tempAdds",
                minimum=-10, maximum=10, step=0.1, unit="°C"),
        _sensor("temp", "Water temperature", "temp", unit="°C", device_class="temperature"),
    ]),
    "skimmer": DeviceSpec("skimmer", "Protein skimmer", [
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("level", "Speed", "level", "saveCfg", "level", unit="%"),
        _number("nlevel", "Night speed", "nlevel", "saveCfg", "nlevel", unit="%"),
    ]),
    "feeder": DeviceSpec("feeder", "Auto feeder", [
        _button("feed", "Feed now", {"type": "feedStart", "status": 1}),
        _button("clean", "Clean", {"type": "clean"}),
    ]),
    "ph": DeviceSpec("ph", "pH monitor", [
        _sensor("ph", "pH", "ph"),
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("phAdds", "Calibration", "phAdds", "saveCfg", "phAdds",
                minimum=-2, maximum=2, step=0.01, unit="pH"),
    ]),
    "temp": DeviceSpec("temp", "Temperature controller", [
        _sensor("temp", "Temperature", "temp", unit="°C", device_class="temperature"),
        _switch("power", "Power", "power", {"type": "saveCfg", "power": True},
                {"type": "saveCfg", "power": False}),
        _number("tempAdds", "Calibration", "tempAdds", "saveCfg", "tempAdds",
                minimum=-10, maximum=10, step=0.1, unit="°C"),
    ]),
    "water": DeviceSpec("water", "Auto top-off (ATO)", [
        _button("fill", "Fill", {"type": "motorStart"}),
        _button("stop", "Stop", {"type": "motorStop"}),
        _button("recover", "Recover", {"type": "motorRecover"}),
    ]),
    "filter": DeviceSpec("filter", "Roller filter", [
        _button("advance", "Advance roll", {"type": "motorStep"}),
        _button("start", "Start motor", {"type": "motorStart"}),
        _button("stop", "Stop motor", {"type": "motorStop"}),
        _number("speed", "Speed", "speed", "speedSet", "speed", unit="%"),
    ]),
    "dpump": DeviceSpec("dpump", "Dosing pump", [
        # Dosing schedules (savePumpList) and calibration (motorRunTime) are
        # multi-head and not modelled yet - only a refresh is safe here.
    ]),
}

EXPERIMENTAL_TYPES = list(DEVICE_SPECS.keys())


class ExperimentalDeviceHub:
    """Owns a gen-2 MQTT connection and the latest reported state for one device."""

    def __init__(
        self, hass: HomeAssistant, serial: str, device_type: str,
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self.hass = hass
        self.serial = serial
        self.device_type = device_type
        self.spec = DEVICE_SPECS[device_type]
        self._poll_interval = max(10, int(poll_interval))
        self._connected = False
        self._last_reply = 0.0
        self._poll_unsub = None
        self.state: dict[str, Any] = {}
        self._entities: list[Any] = []
        self.client = AipaiGen2Client(
            serial,
            on_message=self._handle_message,
            on_connection_change=self._handle_connection_change,
        )

    @property
    def available(self) -> bool:
        grace = self._poll_interval * 3 + 15
        return self._connected and (time.monotonic() - self._last_reply) < grace

    def register_entity(self, entity: Any) -> None:
        self._entities.append(entity)

    async def async_connect(self) -> None:
        await self.hass.async_add_executor_job(self.client.connect)
        self._poll_unsub = async_track_time_interval(
            self.hass, self._async_poll, timedelta(seconds=self._poll_interval)
        )

    async def async_disconnect(self) -> None:
        if self._poll_unsub:
            self._poll_unsub()
            self._poll_unsub = None
        await self.hass.async_add_executor_job(self.client.disconnect)

    async def _async_poll(self, _now) -> None:  # noqa: ANN001
        if self._connected:
            self.request_refresh()
        self._notify()

    def request_refresh(self) -> None:
        self.client.request_state()

    def send(self, command: dict[str, Any]) -> None:
        self.client.send(command)
        # Optimistically reflect simple field writes so the UI feels responsive.
        self.request_refresh()

    def set_field(self, cmd_type: str, cmd_field: str, value: Any, extra: dict | None = None) -> None:
        cmd = {"type": cmd_type, cmd_field: value}
        if extra:
            cmd.update(extra)
        self.send(cmd)

    def _handle_connection_change(self, connected: bool) -> None:
        self._connected = connected
        self.hass.loop.call_soon_threadsafe(self._notify)
        if connected:
            self.hass.loop.call_soon_threadsafe(self.request_refresh)

    def _handle_message(self, payload: dict[str, Any]) -> None:
        self.hass.loop.call_soon_threadsafe(self._process, payload)

    def _process(self, payload: dict[str, Any]) -> None:
        # Device replies echo their state fields at the top level.
        self._last_reply = time.monotonic()
        for k, v in payload.items():
            if k not in ("type", "clientId", "sn"):
                self.state[k] = v
        self._notify()

    def _notify(self) -> None:
        for entity in self._entities:
            entity.async_write_ha_state()
