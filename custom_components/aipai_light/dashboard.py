"""Lovelace dashboard builders.

Pure functions (no Home Assistant imports) so they can be used both by the
`aipai_light.generate_dashboard` service *inside* Home Assistant and by the
standalone `dashboards/generate_dashboard.py` script outside it.

A "light" is a plain dict:
    {"serial", "model", "power", "mode", "temp", "channels": [entity_id, ...]}
where the entity values are real entity_ids (or None if that entity is absent).
"""
from __future__ import annotations

from typing import Any

Light = dict[str, Any]
Group = tuple[str, list[Light]]


# -- core (stock Home Assistant only) --------------------------------------

def _core_summary_tiles(light: Light) -> list[dict]:
    cards: list[dict] = []
    if light.get("power"):
        cards.append({"type": "tile", "entity": light["power"],
                      "name": f"Light {light['serial']}",
                      "icon": "mdi:led-strip-variant",
                      "features": [{"type": "toggle"}]})
    if light.get("temp"):
        cards.append({"type": "tile", "entity": light["temp"], "name": "Temperature"})
    if light.get("mode"):
        cards.append({"type": "tile", "entity": light["mode"], "name": "Mode",
                      "features": [{"type": "select-options"}]})
    return cards


def _core_channel_tiles(light: Light) -> list[dict]:
    return [{"type": "tile", "entity": ent,
             "features": [{"type": "numeric-input", "style": "slider"}]}
            for ent in light.get("channels") or []]


def build_core(groups: list[Group], designer_url: str = "/local/aipai/designer.html") -> dict:
    """Dashboard using only stock cards (tiles, headings, badges)."""
    views: list[dict] = []
    sections, badges = [], []
    for tank, lights in groups:
        cards: list[dict] = [{"type": "heading", "heading": tank,
                              "heading_style": "title", "icon": "mdi:fishbowl"}]
        for light in lights:
            cards.extend(_core_summary_tiles(light))
            if light.get("temp"):
                badges.append({"type": "entity", "entity": light["temp"]})
        sections.append({"type": "grid", "cards": cards})
    views.append({"title": "Overview", "path": "overview", "type": "sections",
                  "max_columns": 3, "badges": badges, "sections": sections})

    for _tank, lights in groups:
        for light in lights:
            cards = [{"type": "heading",
                      "heading": f"Light {light['serial']} · {light.get('model') or ''}".strip(" ·"),
                      "heading_style": "title"}]
            cards.extend(_core_summary_tiles(light))
            cards.append({"type": "heading", "heading": "Channels",
                          "heading_style": "subtitle"})
            cards.extend(_core_channel_tiles(light))
            views.append({"title": str(light["serial"]),
                          "path": f"light-{light['serial']}",
                          "icon": "mdi:led-strip-variant", "type": "sections",
                          "max_columns": 2,
                          "sections": [{"type": "grid", "cards": cards}]})

    views.append({"title": "Designer", "path": "designer",
                  "icon": "mdi:chart-bell-curve",
                  "cards": [{"type": "iframe", "url": designer_url,
                             "aspect_ratio": "150%"}]})
    return {"title": "Reef Lights", "views": views}


# -- mushroom (needs the Mushroom + auto-entities HACS cards) ---------------

def build_mushroom(groups: list[Group], designer_url: str = "/local/aipai/designer.html") -> dict:
    views: list[dict] = []
    sections = []
    for tank, lights in groups:
        cards: list[dict] = [{"type": "custom:mushroom-title-card", "title": tank}]
        for light in lights:
            row = []
            if light.get("power"):
                row.append({"type": "custom:mushroom-entity-card",
                            "entity": light["power"],
                            "name": f"Light {light['serial']}",
                            "icon": "mdi:led-strip-variant",
                            "tap_action": {"action": "toggle"}})
            if light.get("temp"):
                row.append({"type": "custom:mushroom-entity-card",
                            "entity": light["temp"], "name": "Temp"})
            if row:
                cards.append({"type": "grid", "columns": len(row), "square": False,
                              "cards": row})
            if light.get("mode"):
                cards.append({"type": "custom:mushroom-select-card",
                              "entity": light["mode"], "name": "Mode"})
        sections.append({"type": "grid", "cards": cards})
    views.append({"title": "Overview", "path": "overview", "type": "sections",
                  "max_columns": 3, "sections": sections})

    # Globs every AIPAI channel entity, so lights added later need no edits.
    views.append({
        "title": "All channels", "path": "channels", "icon": "mdi:tune-vertical",
        "cards": [{
            "type": "custom:auto-entities",
            "card": {"type": "grid", "columns": 2, "square": False},
            "card_param": "cards",
            "filter": {"include": [{
                "entity_id": "number.aipai_light_*",
                "options": {"type": "custom:mushroom-number-card",
                            "display_mode": "slider"},
            }]},
            "sort": {"method": "friendly_name"},
        }],
    })
    views.append({"title": "Designer", "path": "designer",
                  "icon": "mdi:chart-bell-curve",
                  "cards": [{"type": "iframe", "url": designer_url,
                             "aspect_ratio": "150%"}]})
    return {"title": "Reef Lights", "views": views}


def group_tanks(lights: list[Light], spec: str | None) -> list[Group]:
    """'Display=123,456;Frag=789' -> [('Display',[...]), ('Frag',[...])]."""
    if not spec:
        return [("Lights", lights)]
    by_serial = {str(l["serial"]): l for l in lights}
    groups: list[Group] = []
    used: set[str] = set()
    for chunk in spec.split(";"):
        if "=" not in chunk:
            continue
        name, serials = chunk.split("=", 1)
        members = [by_serial[s.strip()] for s in serials.split(",") if s.strip() in by_serial]
        used.update(str(l["serial"]) for l in members)
        if members:
            groups.append((name.strip(), members))
    leftover = [l for l in lights if str(l["serial"]) not in used]
    if leftover:
        groups.append(("Other lights", leftover))
    return groups
