"""Home Assistant services for time, schedule, and moon control (lights)."""
from __future__ import annotations

import calendar
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .dashboard import build_core, build_mushroom, group_tanks
from .hub import AipaiLightHub
from .schedule import (
    Period,
    build_channel_curve,
    curve_to_csv,
    hhmm_dotted,
    parse_hhmm,
    validate_curve,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_SYNC_CLOCK = "sync_clock"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_SET_CHANNEL_PERIOD = "set_channel_period"
SERVICE_SET_MOON = "set_moon"
SERVICE_GENERATE_DASHBOARD = "generate_dashboard"

_SERIAL = vol.Optional("serial")
# A serial may be one value, a list of values, or omitted (= all lights).
_SERIAL_VALUE = vol.Any(cv.string, [cv.string])

_SYNC_CLOCK_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Optional("datetime"): cv.string,      # device-local target 'YYYY-MM-DD HH:MM:SS'
    vol.Optional("timezone"): vol.Coerce(int),  # UTC offset hours
})

_SET_SCHEDULE_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Optional("curves"): [list],           # list of per-channel 24-int curves
    vol.Optional("open_hour"): vol.Coerce(int),
    vol.Optional("close_hour"): vol.Coerce(int),
    vol.Optional("mode"): vol.Any("0", "1", 0, 1),  # optional: set control mode too
})

_PERIOD_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("channel"): vol.Any(int, cv.string),  # index or label
    vol.Required("start"): cv.string,
    vol.Required("end"): cv.string,
    vol.Required("level"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
    vol.Optional("ramp_up", default=1.0): vol.Coerce(float),
    vol.Optional("ramp_down", default=1.0): vol.Coerce(float),
})

_GENERATE_DASHBOARD_SCHEMA = vol.Schema({
    vol.Optional("style", default="core"): vol.Any("core", "mushroom"),
    vol.Optional("tanks"): cv.string,          # 'Display=123,456;Frag=789'
    vol.Optional("designer_url", default="/local/aipai/designer.html"): cv.string,
})

_SET_MOON_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("color", default="#00A0E9"): cv.string,
    vol.Required("level", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    vol.Required("start"): cv.string,
    vol.Required("end"): cv.string,
    vol.Optional("enable", default=True): cv.boolean,
    vol.Optional("preview", default=False): cv.boolean,
})


def _light_hubs(hass: HomeAssistant, serial: str | list[str] | None) -> list[AipaiLightHub]:
    """Resolve target light hubs. `serial` may be one, many, or None (= all)."""
    all_hubs = [
        h for h in hass.data.get(DOMAIN, {}).values() if isinstance(h, AipaiLightHub)
    ]
    if serial is None:
        return all_hubs
    wanted = {serial} if isinstance(serial, str) else set(serial)
    wanted = {str(s).strip() for s in wanted}
    hubs = [h for h in all_hubs if h.serial in wanted]
    if not hubs:
        _LOGGER.warning("aipai_light service: no matching light for serial=%s", serial)
    return hubs


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SET_MOON):
        return

    async def handle_sync_clock(call: ServiceCall) -> None:
        serial = call.data.get("serial")
        tz = call.data.get("timezone")
        dt_str = call.data.get("datetime")
        for hub in _light_hubs(hass, serial):
            if tz is not None:
                hub.set_timezone(tz)
            epoch = None
            if dt_str:
                # Interpret the given time as device-local; convert to UTC epoch.
                local = _parse_dt(dt_str)
                offset = tz if tz is not None else _int(hub.state.timezone)
                epoch = calendar.timegm(local) - offset * 3600
            hub.sync_clock(epoch)

    async def handle_set_schedule(call: ServiceCall) -> None:
        serial = call.data.get("serial")
        curves = call.data.get("curves")
        for hub in _light_hubs(hass, serial):
            road_data = None
            if curves is not None:
                road_data = [curve_to_csv(validate_curve(c)) for c in curves]
            mode = call.data.get("mode")
            hub.apply_schedule(
                road_data=road_data,
                open_hour=call.data.get("open_hour"),
                close_hour=call.data.get("close_hour"),
                mode=None if mode is None else str(mode),
            )

    async def handle_set_channel_period(call: ServiceCall) -> None:
        serial = call.data.get("serial")
        period = Period(
            start=parse_hhmm(call.data["start"]),
            end=parse_hhmm(call.data["end"]),
            level=call.data["level"],
            ramp_up=call.data["ramp_up"],
            ramp_down=call.data["ramp_down"],
        )
        for hub in _light_hubs(hass, serial):
            idx = _resolve_channel(hub, call.data["channel"])
            if idx is None:
                continue
            rows = list(hub.state.road_data)
            while len(rows) <= idx:
                rows.append(",".join(["0"] * 24))
            rows[idx] = curve_to_csv(build_channel_curve([period]))
            hub.apply_schedule(road_data=rows)

    async def handle_set_moon(call: ServiceCall) -> None:
        serial = call.data.get("serial")
        for hub in _light_hubs(hass, serial):
            hub.set_moon(
                color_hex=call.data["color"],
                level_pct=call.data["level"],
                # The device encodes moon times as HH.MM (12:23 -> 12.23), not
                # decimal hours — see the app's moonSet builder.
                start_hhmm=hhmm_dotted(call.data["start"]),
                end_hhmm=hhmm_dotted(call.data["end"]),
                enable=call.data["enable"],
                save=not call.data["preview"],
            )

    async def handle_generate_dashboard(call: ServiceCall) -> dict[str, Any]:
        """Build a Lovelace dashboard for every configured light.

        Runs inside Home Assistant, so no external tooling is needed, and it
        reads the entity registry for the *real* entity IDs rather than
        guessing them.
        """
        import yaml  # PyYAML ships with Home Assistant

        registry = er.async_get(hass)
        lights: list[dict[str, Any]] = []
        for entry_id, hub in hass.data.get(DOMAIN, {}).items():
            if not isinstance(hub, AipaiLightHub):
                continue
            light: dict[str, Any] = {
                "serial": hub.serial, "model": hub.state.model or "",
                "power": None, "mode": None, "temp": None, "channels": [],
            }
            channels: list[tuple[int, str]] = []
            for ent in er.async_entries_for_config_entry(registry, entry_id):
                uid = ent.unique_id or ""
                if uid.endswith("_power"):
                    light["power"] = ent.entity_id
                elif uid.endswith("_mode"):
                    light["mode"] = ent.entity_id
                elif uid.endswith("_temperature"):
                    light["temp"] = ent.entity_id
                elif "_ch" in uid:
                    idx = uid.rsplit("_ch", 1)[-1]
                    if idx.isdigit():
                        channels.append((int(idx), ent.entity_id))
            light["channels"] = [e for _i, e in sorted(channels)]
            lights.append(light)

        if not lights:
            return {"yaml": "", "lights": 0,
                    "error": "No AIPAI lights are configured yet."}

        lights.sort(key=lambda x: str(x["serial"]))
        groups = group_tanks(lights, call.data.get("tanks"))
        builder = build_mushroom if call.data["style"] == "mushroom" else build_core
        board = builder(groups, call.data["designer_url"])
        text = yaml.safe_dump(board, sort_keys=False, allow_unicode=True, width=100)
        return {"yaml": text, "lights": len(lights),
                "serials": [str(l["serial"]) for l in lights]}

    hass.services.async_register(
        DOMAIN, SERVICE_GENERATE_DASHBOARD, handle_generate_dashboard,
        _GENERATE_DASHBOARD_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_SYNC_CLOCK, handle_sync_clock, _SYNC_CLOCK_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, handle_set_schedule, _SET_SCHEDULE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_CHANNEL_PERIOD, handle_set_channel_period, _PERIOD_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_MOON, handle_set_moon, _SET_MOON_SCHEMA)


def _resolve_channel(hub: AipaiLightHub, channel: Any) -> int | None:
    if isinstance(channel, int):
        return channel if 0 <= channel < hub.roads else None
    label = str(channel).strip().lower()
    for i, name in enumerate(hub.labels):
        if name.lower() == label:
            return i
    try:
        idx = int(label)
        return idx if 0 <= idx < hub.roads else None
    except ValueError:
        return None


def _parse_dt(value: str):  # noqa: ANN201
    from datetime import datetime

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(value, fmt).timetuple()
        except ValueError:
            continue
    raise vol.Invalid(f"Unrecognized datetime: {value}")


def _int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
