"""Button platform: persist-levels, restart, and force-refresh."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .hub import AipaiLightHub


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    if isinstance(hub, AipaiLightHub):
        async_add_entities(
            [AipaiPersistButton(hub), AipaiRestartButton(hub), AipaiRefreshButton(hub)]
        )
        return
    from .experimental import ExperimentalDeviceHub
    from .experimental_entities import build_entities

    if isinstance(hub, ExperimentalDeviceHub):
        entities = build_entities(hub, "button")
        entities.append(ExpRefreshButton(hub))
        async_add_entities(entities)


class _AipaiButtonBase(ButtonEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG  # keep out of the main dashboard

    def __init__(self, hub: AipaiLightHub) -> None:
        self._hub = hub
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.serial)})
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available


class AipaiPersistButton(_AipaiButtonBase):
    _attr_name = "Save levels to device"
    _attr_icon = "mdi:content-save"

    def __init__(self, hub: AipaiLightHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.serial}_persist"

    async def async_press(self) -> None:
        self._hub.persist_levels()


class AipaiRestartButton(_AipaiButtonBase):
    _attr_name = "Restart"
    _attr_icon = "mdi:restart"

    def __init__(self, hub: AipaiLightHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.serial}_restart"

    async def async_press(self) -> None:
        self._hub.restart()


class AipaiRefreshButton(_AipaiButtonBase):
    _attr_name = "Refresh state"
    _attr_icon = "mdi:refresh"

    def __init__(self, hub: AipaiLightHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.serial}_refresh"

    async def async_press(self) -> None:
        self._hub.request_refresh()


class ExpRefreshButton(ButtonEntity):
    """Refresh button for experimental (non-light) devices."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Refresh state"
    _attr_icon = "mdi:refresh"

    def __init__(self, hub) -> None:  # noqa: ANN001
        self._hub = hub
        self._attr_unique_id = f"{hub.serial}_refresh"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, hub.serial)})
        hub.register_entity(self)

    @property
    def available(self) -> bool:
        return self._hub.available

    async def async_press(self) -> None:
        self._hub.request_refresh()
