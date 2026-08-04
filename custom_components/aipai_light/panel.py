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
CARD_VERSION = "0.6.2"   # bump to bust the browser cache when the card changes

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

    hass.data[_CARD_REGISTERED_KEY] = True

    # Register the card as a Lovelace *resource* - this is what actually makes
    # `custom:aipai-reef-card` resolve on storage-mode dashboards. (The older
    # add_extra_js_url approach did not reliably load the module, so users had
    # to add the resource by hand.) Do it once HA has started, when the Lovelace
    # resource collection is guaranteed to exist; async_at_started fires
    # immediately if HA is already running (e.g. a HACS update + reload).
    from homeassistant.helpers.start import async_at_started

    async def _add_card_resource(_event=None) -> None:
        if await _register_lovelace_resource(hass):
            _LOGGER.info("AIPAI Reef card registered as a Lovelace resource")
            return
        # No storage-mode resource collection (YAML-mode dashboards) - fall back
        # to injecting the module globally, and tell the user how to do it by hand
        # if even that isn't available.
        try:
            from homeassistant.components import frontend

            frontend.add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
            _LOGGER.info("AIPAI Reef card injected via extra_js_url (YAML mode)")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not auto-load the AIPAI Reef card (%s). Add it by hand: "
                "Settings > Dashboards > (top-right) Resources > Add > "
                "URL %s , type JavaScript module.",
                err, f"{CARD_URL}?v={CARD_VERSION}",
            )

    async_at_started(hass, _add_card_resource)


async def _register_lovelace_resource(hass: HomeAssistant) -> bool:
    """Add (or update) the card in the Lovelace resource collection.

    Returns True if the storage-backed resource collection handled it, False if
    it isn't available (e.g. YAML-mode dashboards, which are read-only here).
    Guarded heavily because ``hass.data['lovelace']`` has changed shape across
    Home Assistant versions.
    """
    url = f"{CARD_URL}?v={CARD_VERSION}"
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        # YAML-mode collections have no async_create_item - not our path.
        if resources is None or not hasattr(resources, "async_create_item"):
            return False

        # Ensure the collection has read its store before we inspect it.
        if hasattr(resources, "loaded") and not resources.loaded:
            await resources.async_load()
            resources.loaded = True

        for item in resources.async_items():
            if str(item.get("url", "")).split("?")[0] == CARD_URL:
                # Already present (incl. a hand-added one) - keep the version
                # query current so the browser cache busts on upgrades.
                if item.get("url") != url and item.get("id"):
                    await resources.async_update_item(item["id"], {"url": url})
                return True

        await resources.async_create_item({"res_type": "module", "url": url})
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Lovelace resource registration skipped: %s", err)
        return False


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
