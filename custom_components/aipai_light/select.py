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
        from .preset_store import async_get_store

        store = await async_get_store(hass)
        async_add_entities([AipaiModeSelect(hub), AipaiPresetSelect(hub, store)])
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


class AipaiPresetSelect(SelectEntity):
    """One-tap lighting presets (Daylight, Viewing, Feeding, Maintenance, ...)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Preset"
    _attr_icon = "mdi:palette"

    def __init__(self, hub: AipaiLightHub, store) -> None:  # noqa: ANN001
        self._hub = hub
        self._store = store
        self._applied: str | None = None
        self._attr_unique_id = f"{hub.serial}_preset"
        self._attr_device_info = hub.device_info
        hub.register_entity(self)
        store.add_listener(self._on_presets_changed)

    def _on_presets_changed(self) -> None:
        # A preset was saved or deleted - refresh the dropdown.
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._hub.available

    @property
    def options(self) -> list[str]:
        return self._store.names

    @property
    def current_option(self) -> str | None:
        # Presets are "fire and forget" - we report the last one applied rather
        # than trying to reverse-engineer which preset the levels match.
        return self._applied if self._applied in self._store.names else None

    @property
    def extra_state_attributes(self) -> dict:
        # The native card renders three slot chips and lets you *view* a preset
        # before applying it, so expose both the names and each slot's stored
        # levels (label-keyed), not just the flat options list.
        details = []
        for slot in self._store.slots:
            if slot is None:
                details.append(None)
            else:
                body = slot.get("body") or {}
                details.append({
                    "name": slot.get("name", ""),
                    "levels": body.get("levels", {}),
                })
        return {
            "aipai_kind": "presets",
            "slots": self._store.slot_names(),   # [name | null, ...]
            "slot_details": details,             # [{name, levels} | null, ...]
        }

    async def async_select_option(self, option: str) -> None:
        preset = self._store.get(option)
        if preset is None:
            return
        self._hub.apply_preset(preset)
        self._applied = option
        self.async_write_ha_state()
