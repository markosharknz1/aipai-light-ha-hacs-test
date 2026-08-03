"""The AIPAI Aquarium Light integration.

The 'light' device type is verified against real hardware. All other device
types are EXPERIMENTAL / UNVERIFIED - transcribed from the decrypted app but
never tested on a physical unit. See experimental.py and PROTOCOL.md.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_DEVICE_TYPE,
    CONF_MODEL,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    DEFAULT_POLL_INTERVAL,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
)
from .experimental import ExperimentalDeviceHub
from .hub import AipaiLightHub

# All platforms are forwarded; each platform decides what (if anything) to
# create for the hub type behind this entry.
PLATFORMS = ["switch", "sensor", "number", "select", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    serial = entry.data[CONF_SERIAL]
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_LIGHT)
    poll = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

    if device_type == DEVICE_TYPE_LIGHT:
        hub: AipaiLightHub | ExperimentalDeviceHub = AipaiLightHub(
            hass, serial, model_hint=entry.data.get(CONF_MODEL) or None, poll_interval=poll
        )
    else:
        hub = ExperimentalDeviceHub(hass, serial, device_type, poll_interval=poll)

    try:
        await hub.async_connect()
    except OSError as err:
        # Broker unreachable right now — let HA retry setup later.
        raise ConfigEntryNotReady(f"Cannot reach AIPAI broker: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    # Re-arm (or revert) any timed lights-off that was pending across a restart -
    # never leave a tank dark because the in-memory timer was lost.
    if device_type == DEVICE_TYPE_LIGHT:
        from .off_store import async_get_off_store

        off_store = await async_get_off_store(hass)
        hub.attach_off_store(off_store)
        pending = off_store.get(serial)
        if pending:
            await hub.async_restore_off(pending)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if device_type == DEVICE_TYPE_LIGHT:
        from .panel import async_register_card, async_register_panel
        from .services import async_register_services

        async_register_services(hass)
        await async_register_panel(hass)
        await async_register_card(hass)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options (e.g. poll interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hub = hass.data[DOMAIN].pop(entry.entry_id)
        await hub.async_disconnect()
    return unload_ok
