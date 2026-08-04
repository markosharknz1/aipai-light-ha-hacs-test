"""Config flow: find lights on the network, or add one by serial.

Two ways in:
* **Search network** - scan the HA host's own subnet (or one you name) for
  lights answering the local ``/?read=config`` endpoint, then pick from what's
  found (serial + model filled in for you).
* **Enter manually** - type a serial (and optionally a model), the original path.

Either way you can give the light a friendly **name** up front; it can also be
renamed any time from the device's page in Home Assistant.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICE_TYPE,
    CONF_MODEL,
    CONF_NAME,
    CONF_POLL_INTERVAL,
    CONF_SERIAL,
    CONF_SUBNET,
    DEFAULT_POLL_INTERVAL,
    DEVICE_TYPE_LIGHT,
    DOMAIN,
)
from .discovery import FoundLight, async_local_subnets, async_scan
from .experimental import DEVICE_SPECS

# Verified light first, then the experimental device types.
_TYPE_OPTIONS = {DEVICE_TYPE_LIGHT: "Light (verified)"}
_TYPE_OPTIONS.update(
    {key: f"{spec.name} (experimental)" for key, spec in DEVICE_SPECS.items()}
)

STEP_MANUAL_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=""): str,
        vol.Required(CONF_SERIAL): str,
        vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_LIGHT): vol.In(_TYPE_OPTIONS),
        vol.Optional(CONF_MODEL, default=""): str,
    }
)


class AipaiLightConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for AIPAI Aquarium Light."""

    VERSION = 1

    def __init__(self) -> None:
        self._found: list[FoundLight] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry point: choose to search the network or add by hand."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["search", "manual"],
        )

    # -- network search ----------------------------------------------------

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask which subnet to scan (default: the HA host's own), then scan it."""
        errors: dict[str, str] = {}
        detected = await async_local_subnets(self.hass)
        default_subnet = ", ".join(detected) if detected else ""

        if user_input is not None:
            raw = (user_input.get(CONF_SUBNET) or default_subnet).strip()
            cidrs = [c.strip() for c in raw.split(",") if c.strip()]
            if not cidrs:
                errors["base"] = "no_subnet"
            else:
                found = await async_scan(self.hass, cidrs)
                # Drop lights already configured as a light entry.
                configured = {
                    e.data.get(CONF_SERIAL)
                    for e in self._async_current_entries()
                    if e.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_LIGHT) == DEVICE_TYPE_LIGHT
                }
                self._found = [f for f in found if f.serial not in configured]
                if not self._found:
                    errors["base"] = "none_found" if not found else "all_configured"
                else:
                    return await self.async_step_pick()

        schema = vol.Schema({vol.Optional(CONF_SUBNET, default=default_subnet): str})
        return self.async_show_form(
            step_id="search",
            data_schema=schema,
            errors=errors,
            description_placeholders={"detected": default_subnet or "unknown"},
        )

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Pick one of the discovered lights and (optionally) name it."""
        errors: dict[str, str] = {}
        choices = {
            f.serial: f"{f.serial} — {f.model or 'AIPAI'} ({f.roads}ch) @ {f.ip}"
            for f in self._found
        }

        if user_input is not None:
            serial = user_input[CONF_SERIAL]
            match = next((f for f in self._found if f.serial == serial), None)
            if match is None:
                errors["base"] = "unknown"
            else:
                name = (user_input.get(CONF_NAME) or "").strip()
                await self.async_set_unique_id(f"{DEVICE_TYPE_LIGHT}_{serial}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name or f"AIPAI Light {serial}",
                    data={
                        CONF_SERIAL: serial,
                        CONF_DEVICE_TYPE: DEVICE_TYPE_LIGHT,
                        CONF_MODEL: match.model or "",
                        CONF_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_SERIAL): vol.In(choices),
                vol.Optional(CONF_NAME, default=""): str,
            }
        )
        return self.async_show_form(step_id="pick", data_schema=schema, errors=errors)

    # -- manual entry ------------------------------------------------------

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            serial = user_input[CONF_SERIAL].strip()
            device_type = user_input[CONF_DEVICE_TYPE]
            model = (user_input.get(CONF_MODEL) or "").strip()
            name = (user_input.get(CONF_NAME) or "").strip()
            if not serial.isdigit():
                errors["base"] = "invalid_serial"
            else:
                # Allow the same serial once per device type.
                await self.async_set_unique_id(f"{device_type}_{serial}")
                self._abort_if_unique_id_configured()
                label = _TYPE_OPTIONS.get(device_type, device_type)
                return self.async_create_entry(
                    title=name or f"AIPAI {label} {serial}",
                    data={
                        CONF_SERIAL: serial,
                        CONF_DEVICE_TYPE: device_type,
                        CONF_MODEL: model,
                        CONF_NAME: name,
                    },
                )

        return self.async_show_form(
            step_id="manual", data_schema=STEP_MANUAL_SCHEMA, errors=errors
        )

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
