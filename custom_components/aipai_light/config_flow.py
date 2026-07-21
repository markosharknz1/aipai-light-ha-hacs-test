"""Config flow: pick device type + serial. Model is optional (lights only)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICE_TYPE,
    CONF_MODEL,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    DEFAULT_POLL_INTERVAL,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
)
from .experimental import DEVICE_SPECS

# Verified light first, then the experimental device types.
_TYPE_OPTIONS = {DEVICE_TYPE_LIGHT: "Light (verified)"}
_TYPE_OPTIONS.update(
    {key: f"{spec.name} (experimental)" for key, spec in DEVICE_SPECS.items()}
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL): str,
        vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_LIGHT): vol.In(_TYPE_OPTIONS),
        vol.Optional(CONF_MODEL, default=""): str,
    }
)


class AipaiLightConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AIPAI Aquarium Light."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            serial = user_input[CONF_SERIAL].strip()
            device_type = user_input[CONF_DEVICE_TYPE]
            model = (user_input.get(CONF_MODEL) or "").strip()
            if not serial.isdigit():
                errors["base"] = "invalid_serial"
            else:
                # Allow the same serial once per device type.
                await self.async_set_unique_id(f"{device_type}_{serial}")
                self._abort_if_unique_id_configured()
                label = _TYPE_OPTIONS.get(device_type, device_type)
                return self.async_create_entry(
                    title=f"AIPAI {label} {serial}",
                    data={
                        CONF_SERIAL: serial,
                        CONF_DEVICE_TYPE: device_type,
                        CONF_MODEL: model,
                    },
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "AipaiOptionsFlow":  # noqa: ANN001
        return AipaiOptionsFlow(config_entry)


class AipaiOptionsFlow(config_entries.OptionsFlow):
    """Tune runtime options (currently the state poll interval)."""

    def __init__(self, config_entry) -> None:  # noqa: ANN001
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        schema = vol.Schema(
            {
                vol.Optional(CONF_POLL_INTERVAL, default=current): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=600)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
