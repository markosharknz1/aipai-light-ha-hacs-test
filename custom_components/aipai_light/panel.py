"""Serve the bundled Reef Schedule Designer and add it to the sidebar.

The designer ships inside the integration (``panel/designer.html``) so that a
HACS install delivers it too — HACS only copies ``custom_components/``, so a
file left in ``config/www`` would never arrive. Registering it here means the
tool is available at ``/aipai_light/designer.html`` and appears in the sidebar
with no manual file copying and no ``configuration.yaml`` edits.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "/aipai_light/designer.html"
PANEL_PATH = "aipai-designer"
PANEL_TITLE = "Reef Designer"
PANEL_ICON = "mdi:jellyfish"

_REGISTERED_KEY = "aipai_light_panel_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the designer and add a sidebar item (idempotent)."""
    if hass.data.get(_REGISTERED_KEY):
        return

    source = Path(__file__).parent / "panel" / "designer.html"
    if not source.is_file():
        _LOGGER.warning("Bundled designer missing at %s; skipping panel", source)
        return

    # Serve the single HTML file.
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_URL, str(source), False)]
        )
    except ImportError:  # Home Assistant < 2024.7
        hass.http.register_static_path(PANEL_URL, str(source), False)
    except RuntimeError:
        # Path already registered (e.g. a previous reload) - fine.
        pass

    # Add it to the sidebar.
    try:
        from homeassistant.components import frontend

        frontend.async_register_built_in_panel(
            hass,
            "iframe",
            PANEL_TITLE,
            PANEL_ICON,
            PANEL_PATH,
            {"url": PANEL_URL},
            require_admin=False,
        )
    except ValueError:
        pass  # already in the sidebar
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not register sidebar panel: %s", err)

    hass.data[_REGISTERED_KEY] = True
