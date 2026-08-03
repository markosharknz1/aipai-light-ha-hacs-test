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
from .protocol import cmd_to_pct
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
SERVICE_APPLY_PRESET = "apply_preset"
SERVICE_SAVE_PRESET = "save_preset"
SERVICE_DELETE_PRESET = "delete_preset"
SERVICE_PREVIEW_SCHEDULE = "preview_schedule"
SERVICE_SAVE_SETTINGS = "save_settings"
SERVICE_DISCARD_CHANGES = "discard_changes"
SERVICE_UNSAVED_CHANGES = "unsaved_changes"
SERVICE_SAVE_SLOT = "save_slot"
SERVICE_SET_SLOT = "set_slot"
SERVICE_APPLY_SLOT = "apply_slot"
SERVICE_CLEAR_SLOT = "clear_slot"
SERVICE_EXPORT_CONFIG = "export_config"
SERVICE_IMPORT_CONFIG = "import_config"
SERVICE_LIGHTS_OFF = "lights_off"
SERVICE_CANCEL_LIGHTS_OFF = "cancel_lights_off"

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
    vol.Optional("designer_url", default="/aipai_light/designer.html"): cv.string,
})

_APPLY_PRESET_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("preset"): cv.string,
})

_SAVE_PRESET_SCHEMA = vol.Schema({
    vol.Required("serial"): cv.string,     # which light's current look to capture
    vol.Required("name"): cv.string,
})

_DELETE_PRESET_SCHEMA = vol.Schema({vol.Required("name"): cv.string})

_SET_MOON_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("color", default="#00A0E9"): cv.string,
    vol.Required("level", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    vol.Required("start"): cv.string,
    vol.Required("end"): cv.string,
    vol.Optional("enable", default=True): cv.boolean,
    vol.Optional("preview", default=False): cv.boolean,
})


# A time point: an hour, plus a level for every channel at that hour.
_POINT_SCHEMA = vol.Schema({
    vol.Required("hour"): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
    vol.Required("levels"): [vol.All(vol.Coerce(int), vol.Range(min=0, max=100))],
})

_PREVIEW_SCHEDULE_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("points"): vol.All([_POINT_SCHEMA], vol.Length(min=1)),
})

_COMMIT_SCHEMA = vol.Schema({_SERIAL: _SERIAL_VALUE})

# Timed lights-off: either a duration in minutes, or a clock time to come back on.
_LIGHTS_OFF_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Exclusive("minutes", "when"): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
    vol.Exclusive("until", "when"): cv.string,   # "HH:MM" local time
})
_CANCEL_OFF_SCHEMA = vol.Schema({_SERIAL: _SERIAL_VALUE})

_SLOT = vol.All(vol.Coerce(int), vol.Range(min=1, max=3))   # 1-based for humans

_SAVE_SLOT_SCHEMA = vol.Schema({
    vol.Required("serial"): cv.string,        # whose current look to capture
    vol.Required("slot"): _SLOT,
    vol.Optional("name"): cv.string,
})

_APPLY_SLOT_SCHEMA = vol.Schema({_SERIAL: _SERIAL_VALUE, vol.Required("slot"): _SLOT})
_CLEAR_SLOT_SCHEMA = vol.Schema({vol.Required("slot"): _SLOT})

# Store explicit levels into a slot (edit a preset without touching the lights).
# `levels` is label-keyed: {"Blue": 90, "White": 5, ...}.
_SET_SLOT_SCHEMA = vol.Schema({
    vol.Required("slot"): _SLOT,
    vol.Required("name"): cv.string,
    vol.Required("levels"): {cv.string: vol.All(vol.Coerce(int), vol.Range(min=0, max=100))},
})

_EXPORT_SCHEMA = vol.Schema({
    vol.Required("serial"): cv.string,
    vol.Optional("name"): cv.string,
    vol.Optional("include_slots", default=True): cv.boolean,
})

_IMPORT_SCHEMA = vol.Schema({
    _SERIAL: _SERIAL_VALUE,
    vol.Required("config"): vol.Any(cv.string, dict),
    vol.Optional("apply", default=True): cv.boolean,   # off = validate only
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

    async def handle_apply_preset(call: ServiceCall) -> None:
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        preset = store.get(call.data["preset"])
        if preset is None:
            _LOGGER.warning("Unknown preset %r (have: %s)",
                            call.data["preset"], ", ".join(store.names))
            return
        for hub in _light_hubs(hass, call.data.get("serial")):
            hub.apply_preset(preset)

    async def handle_save_preset(call: ServiceCall) -> None:
        """Capture a light's current channel levels as a reusable preset."""
        from .preset_store import async_get_store
        from .presets import levels_from_current

        hubs = _light_hubs(hass, call.data["serial"])
        if not hubs:
            return
        hub = hubs[0]
        levels_pct = [cmd_to_pct(v) for v in hub.channels[: hub.roads]]
        body = {"kind": "levels",
                "levels": levels_from_current(levels_pct, hub.labels[: hub.roads])}
        store = await async_get_store(hass)
        await store.async_save_preset(call.data["name"], body)
        _LOGGER.info("Saved preset %r from light %s", call.data["name"], hub.serial)

    async def handle_delete_preset(call: ServiceCall) -> None:
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        if not await store.async_delete_preset(call.data["name"]):
            _LOGGER.warning("No custom preset named %r to delete", call.data["name"])

    # -- preview / save / discard ----------------------------------------
    # Previews go to the fixture immediately so they can be judged in the
    # water; the roll-back baseline lives in draft_store.

    async def handle_preview_schedule(call: ServiceCall) -> None:
        from .draft_store import async_get_draft_store

        store = await async_get_draft_store(hass)
        points = call.data["points"]
        for hub in _light_hubs(hass, call.data.get("serial")):
            # Capture a baseline the first time we touch a light, otherwise
            # this preview becomes its own "saved" state and Discard is a no-op.
            if store.saved(hub.serial) is None:
                snap = hub.capture_snapshot()
                if snap is not None:
                    await store.async_commit(hub.serial, snap)
            hub.apply_points(points)

    async def handle_save_settings(call: ServiceCall) -> None:
        from .draft_store import async_get_draft_store

        store = await async_get_draft_store(hass)
        for hub in _light_hubs(hass, call.data.get("serial")):
            snap = hub.capture_snapshot()
            if snap is None:
                _LOGGER.warning("save_settings skipped for %s: no state read yet", hub.serial)
                continue
            await store.async_commit(hub.serial, snap)
            _LOGGER.info("Saved current settings for light %s", hub.serial)

    async def handle_discard_changes(call: ServiceCall) -> None:
        from .draft_store import async_get_draft_store

        store = await async_get_draft_store(hass)
        for hub in _light_hubs(hass, call.data.get("serial")):
            baseline = store.saved(hub.serial)
            if baseline is None:
                _LOGGER.warning("Nothing saved for light %s - cannot discard", hub.serial)
                continue
            if hub.restore_snapshot(baseline):
                _LOGGER.info("Rolled light %s back to its saved settings", hub.serial)

    async def handle_unsaved_changes(call: ServiceCall) -> dict[str, Any]:
        """Which lights are running something they haven't saved."""
        from .draft_store import async_get_draft_store

        store = await async_get_draft_store(hass)
        lights = []
        for hub in _light_hubs(hass, call.data.get("serial")):
            current = hub.capture_snapshot()
            lights.append({
                "serial": hub.serial,
                "unsaved": store.has_unsaved(hub.serial, current),
                "saved_at": store.saved_at(hub.serial),
                "known": current is not None,
            })
        return {"lights": lights, "any_unsaved": any(x["unsaved"] for x in lights)}

    # -- preset slots (empty until the user fills them) --------------------

    async def handle_save_slot(call: ServiceCall) -> None:
        from .preset_store import async_get_store
        from .presets import levels_from_current

        hubs = _light_hubs(hass, call.data["serial"])
        if not hubs:
            return
        hub = hubs[0]
        index = int(call.data["slot"]) - 1
        levels_pct = [cmd_to_pct(v) for v in hub.channels[: hub.roads]]
        body = {"kind": "levels",
                "levels": levels_from_current(levels_pct, hub.labels[: hub.roads])}
        store = await async_get_store(hass)
        await store.async_save_slot(index, call.data.get("name", ""), body)
        _LOGGER.info("Saved slot %s from light %s", call.data["slot"], hub.serial)

    async def handle_set_slot(call: ServiceCall) -> None:
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        body = {"kind": "levels", "levels": dict(call.data["levels"])}
        await store.async_save_slot(int(call.data["slot"]) - 1, call.data["name"], body)
        _LOGGER.info("Set slot %s = %r", call.data["slot"], call.data["name"])

    async def handle_apply_slot(call: ServiceCall) -> None:
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        index = int(call.data["slot"]) - 1
        slot = store.slots[index] if 0 <= index < len(store.slots) else None
        if slot is None:
            _LOGGER.warning("Preset slot %s is empty", call.data["slot"])
            return
        for hub in _light_hubs(hass, call.data.get("serial")):
            hub.apply_preset(slot["body"])

    async def handle_clear_slot(call: ServiceCall) -> None:
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        await store.async_clear_slot(int(call.data["slot"]) - 1)

    # -- import / export ----------------------------------------------------

    async def handle_export_config(call: ServiceCall) -> dict[str, Any]:
        """A portable, label-keyed config others can import onto their light."""
        from .draft import normalise_curve
        from .preset_store import async_get_store
        from .share import build_document

        hubs = _light_hubs(hass, call.data["serial"])
        if not hubs:
            return {"error": f"No light with serial {call.data['serial']}"}
        hub = hubs[0]
        if not hub.has_state:
            return {"error": "No configuration read from the light yet"}

        labels = hub.labels[: hub.roads]
        # Curves are stored per channel; turn them back into time points by
        # taking every hour. Verbose, but lossless and trivially correct.
        curves = [normalise_curve(row) for row in hub.state.road_data[: hub.roads]]
        points = [
            {"hour": h, "levels": [c[h] for c in curves]}
            for h in range(24)
        ]
        slots = None
        if call.data.get("include_slots", True):
            store = await async_get_store(hass)
            slots = [
                None if s is None else {"name": s["name"],
                                        "levels": s["body"].get("levels", {})}
                for s in store.slots
            ]
        doc = build_document(
            labels=labels, points=points, slots=slots,
            model=hub.state.model, name=call.data.get("name"),
        )
        return {"config": doc}

    async def handle_import_config(call: ServiceCall) -> dict[str, Any]:
        """Validate a shared config and (optionally) preview it on the lights."""
        from .draft_store import async_get_draft_store
        from .preset_store import async_get_store
        from .share import read_document

        hubs = _light_hubs(hass, call.data.get("serial"))
        if not hubs:
            return {"ok": False, "error": "No matching light"}

        results = []
        draft = await async_get_draft_store(hass)
        for hub in hubs:
            parsed = read_document(call.data["config"], hub.labels[: hub.roads])
            entry = {"serial": hub.serial, "ok": parsed["ok"],
                     "error": parsed["error"], "warnings": parsed["warnings"],
                     "applied": False}
            if parsed["ok"] and call.data.get("apply", True) and parsed["points"]:
                # Same baseline rule as preview_schedule: never let an import
                # become its own saved state, or Discard has nothing to undo.
                if draft.saved(hub.serial) is None:
                    snap = hub.capture_snapshot()
                    if snap is not None:
                        await draft.async_commit(hub.serial, snap)
                entry["applied"] = hub.apply_points(parsed["points"])
            results.append(entry)

        # Slots are shared across lights, so import them once.
        first = read_document(call.data["config"], hubs[0].labels[: hubs[0].roads])
        if first["ok"] and call.data.get("apply", True) and first["slots"]:
            store = await async_get_store(hass)
            for i, slot in enumerate(first["slots"]):
                if slot:
                    await store.async_save_slot(
                        i, slot["name"], {"kind": "levels", "levels": slot["levels"]})

        return {"ok": all(r["ok"] for r in results), "lights": results}

    # -- timed lights-off --------------------------------------------------

    async def handle_lights_off(call: ServiceCall) -> None:
        from datetime import timedelta

        from homeassistant.util import dt as dt_util

        minutes = call.data.get("minutes")
        until = call.data.get("until")
        if minutes is not None:
            revert_at = dt_util.utcnow() + timedelta(minutes=int(minutes))
        elif until:
            h, m = (until.split(":", 1) + ["0"])[:2]
            local_now = dt_util.now()
            target = local_now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            if target <= local_now:
                target += timedelta(days=1)   # that time already passed today
            revert_at = dt_util.as_utc(target)
        else:
            # No duration given - default to a 1-hour off.
            revert_at = dt_util.utcnow() + timedelta(hours=1)

        for hub in _light_hubs(hass, call.data.get("serial")):
            await hub.async_lights_off(revert_at)

    async def handle_cancel_lights_off(call: ServiceCall) -> None:
        for hub in _light_hubs(hass, call.data.get("serial")):
            await hub.async_cancel_off(revert=True)

    hass.services.async_register(
        DOMAIN, SERVICE_LIGHTS_OFF, handle_lights_off, _LIGHTS_OFF_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_LIGHTS_OFF, handle_cancel_lights_off, _CANCEL_OFF_SCHEMA)

    hass.services.async_register(DOMAIN, SERVICE_SAVE_SLOT, handle_save_slot, _SAVE_SLOT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_SLOT, handle_set_slot, _SET_SLOT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_APPLY_SLOT, handle_apply_slot, _APPLY_SLOT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_SLOT, handle_clear_slot, _CLEAR_SLOT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_EXPORT_CONFIG, handle_export_config, _EXPORT_SCHEMA,
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(
        DOMAIN, SERVICE_IMPORT_CONFIG, handle_import_config, _IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL)

    hass.services.async_register(
        DOMAIN, SERVICE_PREVIEW_SCHEDULE, handle_preview_schedule, _PREVIEW_SCHEDULE_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_SAVE_SETTINGS, handle_save_settings, _COMMIT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_DISCARD_CHANGES, handle_discard_changes, _COMMIT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_UNSAVED_CHANGES, handle_unsaved_changes, _COMMIT_SCHEMA,
        supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, SERVICE_APPLY_PRESET, handle_apply_preset, _APPLY_PRESET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SAVE_PRESET, handle_save_preset, _SAVE_PRESET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_PRESET, handle_delete_preset, _DELETE_PRESET_SCHEMA)
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
