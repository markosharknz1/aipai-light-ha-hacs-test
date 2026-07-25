"""Portable light configurations for import/export (pure logic, no HA imports).

A shared config is **label-keyed**, never positional. Channel 4 is Blue2 on an
A8 and something else entirely on another fixture, so a document that said
``[0, 90, 0, 0, 90, ...]`` would light someone else's tank wrongly while looking
perfectly valid. Matching on names ("Blue2": 90) means an 8-channel schedule
imports sensibly onto a 6-channel light: shared channels carry over, the rest
are reported rather than silently mangled.

Import is deliberately loud about mismatches. Someone pasting a config from a
forum needs to know that their fixture has no UV channel before they wonder why
the tank looks wrong, not after.
"""
from __future__ import annotations

from typing import Any

SHARE_VERSION = 1
SHARE_KIND = "aipai_light_config"


def _clamp(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _hour(value: Any) -> int:
    try:
        return max(0, min(23, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def points_to_labelled(points: list[dict], labels: list[str]) -> list[dict]:
    """Positional time points -> label-keyed, for export."""
    out = []
    for p in points or []:
        levels = p.get("levels") or []
        out.append({
            "hour": _hour(p.get("hour")),
            "levels": {
                label: _clamp(levels[i])
                for i, label in enumerate(labels)
                if i < len(levels)
            },
        })
    return sorted(out, key=lambda p: p["hour"])


def points_from_labelled(points: list[dict], labels: list[str]) -> list[dict]:
    """Label-keyed time points -> positional for this fixture, for import.

    Channels the document doesn't mention are left at 0 rather than guessed at.
    """
    lower = [(lab or "").lower() for lab in labels]
    out = []
    for p in points or []:
        wanted = {str(k).lower(): v for k, v in (p.get("levels") or {}).items()}
        out.append({
            "hour": _hour(p.get("hour")),
            "levels": [_clamp(wanted.get(lab, 0)) for lab in lower],
        })
    return sorted(out, key=lambda p: p["hour"])


def build_document(
    *,
    labels: list[str],
    points: list[dict] | None = None,
    slots: list[dict | None] | None = None,
    model: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """A self-describing config document, safe to paste anywhere."""
    doc: dict[str, Any] = {
        "kind": SHARE_KIND,
        "version": SHARE_VERSION,
        "channels": list(labels),
    }
    if name:
        doc["name"] = str(name)
    if model:
        doc["model"] = str(model)
    if points is not None:
        doc["points"] = points_to_labelled(points, labels)
    if slots is not None:
        doc["slots"] = [
            None if s is None else {
                "name": str(s.get("name", "")),
                "levels": {
                    str(k): _clamp(v) for k, v in (s.get("levels") or {}).items()
                },
            }
            for s in slots
        ]
    return doc


def describe_mismatch(doc: dict, labels: list[str]) -> dict[str, list[str]]:
    """Which channels don't line up between this document and this fixture."""
    doc_channels = {str(c).lower(): str(c) for c in (doc.get("channels") or [])}
    # A document may name channels only inside its points.
    for p in doc.get("points") or []:
        for key in (p.get("levels") or {}):
            doc_channels.setdefault(str(key).lower(), str(key))
    mine = {(lab or "").lower(): lab for lab in labels}
    return {
        "missing": sorted(v for k, v in doc_channels.items() if k not in mine),
        "extra": sorted(v for k, v in mine.items() if k not in doc_channels),
    }


def read_document(doc: Any, labels: list[str]) -> dict[str, Any]:
    """Validate and convert a shared document for this fixture.

    Returns ``{ok, points, slots, warnings, error}``. Never raises: a bad paste
    should produce a message, not a stack trace.
    """
    result: dict[str, Any] = {
        "ok": False, "points": [], "slots": [], "warnings": [], "error": None,
    }
    if isinstance(doc, str):
        import json
        try:
            doc = json.loads(doc)
        except ValueError as err:
            result["error"] = f"Not valid JSON: {err}"
            return result
    if not isinstance(doc, dict):
        result["error"] = "Config must be a JSON object"
        return result
    if doc.get("kind") != SHARE_KIND:
        result["error"] = "This doesn't look like an AIPAI light config"
        return result
    version = doc.get("version")
    if not isinstance(version, int) or version > SHARE_VERSION:
        result["error"] = (
            f"Config version {version} is newer than this integration understands "
            f"(max {SHARE_VERSION}) - update the integration first"
        )
        return result

    points = doc.get("points")
    if points is not None:
        if not isinstance(points, list) or not points:
            result["error"] = "Config has a 'points' key but no usable time points"
            return result
        result["points"] = points_from_labelled(points, labels)

    slots = doc.get("slots")
    if isinstance(slots, list):
        result["slots"] = [
            None if s is None else {
                "name": str(s.get("name", "")) or "Imported",
                "levels": {str(k): _clamp(v) for k, v in (s.get("levels") or {}).items()},
            }
            for s in slots
        ]

    mismatch = describe_mismatch(doc, labels)
    if mismatch["missing"]:
        result["warnings"].append(
            "Your light has no " + ", ".join(mismatch["missing"])
            + " channel(s) - those settings were dropped"
        )
    if mismatch["extra"]:
        result["warnings"].append(
            "The config says nothing about your "
            + ", ".join(mismatch["extra"]) + " channel(s) - left at 0"
        )
    result["ok"] = True
    return result
