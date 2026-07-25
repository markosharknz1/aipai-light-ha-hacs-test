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

# The native Lovelace card. Served from the integration and injected on every
# dashboard load, so `type: custom:aipai-reef-card` works with no manual
# resource setup - the point of bundling it with the integration.
CARD_URL = "/aipai_light/aipai-reef-card.js"
CARD_VERSION = "0.5.1"   # bump to bust the browser cache when the card changes

_REGISTERED_KEY = "aipai_light_panel_registered"
_CARD_REGISTERED_KEY = "aipai_light_card_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the native card and add it to every dashboard (idempotent)."""
    if hass.data.get(_CARD_REGISTERED_KEY):
        return

    source = Path(__file__).parent / "lovelace" / "aipai-reef-card.js"
    if not source.is_file():
        _LOGGER.warning("Bundled card missing at %s; skipping", source)
        return

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(source), True)]
        )
    except ImportError:  # Home Assistant < 2024.7
        hass.http.register_static_path(CARD_URL, str(source), True)
    except RuntimeError:
        pass  # already registered (reload)

    # add_extra_js_url injects the module on every dashboard, so users don't
    # have to add a Lovelace resource by hand (which also fails in YAML mode).
    try:
        from homeassistant.components import frontend

        frontend.add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    except Exception as err:  # noqa: BLE001
        _LOGGER.info(
            "Could not auto-add the AIPAI card resource (%s). Add it manually: "
            "Settings > Dashboards > Resources > %s (JavaScript module).",
            err, CARD_URL,
        )

    hass.data[_CARD_REGISTERED_KEY] = True


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

    # Best-effort sidebar entry. Home Assistant removed the `panel_iframe`
    # integration (deprecated 2024.4, deleted since), so the built-in "iframe"
    # panel type is not available on newer cores and this will simply not take.
    # The designer is still reachable at PANEL_URL and via the dashboard's
    # Designer view, which is the supported route - see docs/install-web-files.md.
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
        _LOGGER.debug("Registered '%s' sidebar panel", PANEL_TITLE)
    except ValueError:
        pass  # already in the sidebar
    except Exception as err:  # noqa: BLE001
        _LOGGER.info(
            "No sidebar entry for the designer on this Home Assistant version "
            "(%s). It is served at %s - add it as a Webpage dashboard, or use "
            "the Designer view in the generated dashboard.",
            err,
            PANEL_URL,
        )

    hass.data[_REGISTERED_KEY] = True
