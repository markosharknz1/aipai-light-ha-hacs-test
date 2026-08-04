"""Downloadable diagnostics for a light config entry.

A diagnostics dump is meant to be shared (attached to a bug report), so the
device **serial is redacted** - a serial is enough to control the light, so it
must not travel in a file the user might paste publicly.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SERIAL, DOMAIN

TO_REDACT = {CONF_SERIAL, "serial", "aipai_serial", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    # The entry title embeds the serial ("AIPAI Light (verified) 12345678"), and
    # a diagnostics dump is shareable - strip it out (async_redact_data only
    # redacts by key, so the title needs doing by hand).
    serial = str(entry.data.get(CONF_SERIAL, ""))
    title = entry.title.replace(serial, "**REDACTED**") if serial else entry.title
    data: dict[str, Any] = {
        "entry": {
            "title": title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        }
    }
    if hub is not None:
        st = getattr(hub, "state", None)
        data["hub"] = {
            "connected": getattr(hub, "_connected", None),
            "available": getattr(hub, "available", None),
            "has_state": getattr(hub, "has_state", None),
            "roads": getattr(hub, "roads", None),
            "labels": getattr(hub, "labels", None),
            "mode": getattr(st, "mode", None),
            "model": getattr(st, "model", None),
            "temperature": getattr(st, "temperature", None),
            "channels": list(getattr(hub, "channels", []) or []),
            "off_until": hub.off_until.isoformat() if getattr(hub, "off_until", None) else None,
            "moon": getattr(hub, "moon", None),
            "last_command": getattr(hub, "last_ack", None),
        }
    return async_redact_data(data, TO_REDACT)
