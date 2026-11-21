"""Rendered measurement for the visual judger (Phase 3).

Two things the static CSS pass cannot do: measure real pixels in a
rendered screenshot (PIL only, stdlib otherwise) and read real element
boxes supplied by the coordinator from the live browser.

Layout-box contract: ``layout`` is an optional dict
``{viewport_width_as_string: [{name, x, y, width, height}]}`` — element
boxes in CSS px within the viewport. ``layout_boxes(layout, viewport)``
returns the boxes for one viewport (an empty list when absent); a
missing layout is not an error, it just means the judger measures
static CSS instead and says so in ``measured["layout_basis"]``.

No numbers are invented: an unreadable screenshot makes
``measure_colour_shares`` return None and the caller falls back to the
CSS declaration-count estimate, labelled as an estimate.
"""
from __future__ import annotations

from typing import Any

try:
    from PIL import Image
except ImportError:  # PIL ships in the dev venv; without it there is no rendered measurement.
    Image = None

# Screenshots are downscaled to this max dimension before counting:
# pixel shares, not individual pixels, are what the 60-30-10 check needs.
_MAX_SCREENSHOT_DIMENSION = 320
# A pixel further than this (RGB Euclidean distance) from every palette
# role is photo/content noise, counted as "other" rather than forced
# into the nearest role.
_NEAREST_ROLE_MAX_DISTANCE = 90.0


def layout_boxes(layout: Any, viewport: int | str) -> list[dict[str, Any]]:
    """Boxes for one viewport from a layout dict, or [] when absent.

    Tolerates string or int viewport keys and skips malformed entries
    instead of failing the whole judgement on one bad box.
    """
    if not layout:
        return []
    boxes = layout.get(str(viewport))
    if boxes is None:
        try:
            boxes = layout.get(int(viewport))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            boxes = None
    if not boxes:
        return []
    out: list[dict[str, Any]] = []
    for box in boxes:
        try:
            out.append({
                "name": str(box["name"]),
                "x": float(box["x"]),
                "y": float(box["y"]),
                "width": float(box["width"]),
                "height": float(box["height"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def box_by_name(boxes: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The first box with this element name, or None."""
    return next((box for box in boxes if box["name"] == name), None)


def box_centre(box: dict[str, Any]) -> tuple[float, float]:
    """(centre x, centre y) of a layout box."""
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def measure_colour_shares(
    path: Any, roles: dict[str, tuple[int, int, int]],
) -> dict[str, Any] | None:
    """Count screenshot pixels into palette roles (dominant/secondary/accent).

    Each pixel goes to its nearest role colour when within
    ``_NEAREST_ROLE_MAX_DISTANCE``; anything further out is "other"
    (photos, gradients, content the palette does not describe). Returns
    ``{"shares": {role: fraction, "other": fraction}, "pixels": n,
    "path": str}``, or None when the image cannot be read at all — the
    caller then falls back to the CSS estimate and says so.
    """
    if Image is None or not roles:
        return None
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if max(rgb.size) > _MAX_SCREENSHOT_DIMENSION:
                rgb.thumbnail((_MAX_SCREENSHOT_DIMENSION, _MAX_SCREENSHOT_DIMENSION))
            pixels = list(rgb.getdata())
    except OSError:
        return None
    counts = {role: 0 for role in roles}
    other = 0
    for pixel in pixels:
        best: str | None = None
        best_dist: float | None = None
        for role, target in roles.items():
            dist = ((pixel[0] - target[0]) ** 2 + (pixel[1] - target[1]) ** 2
                    + (pixel[2] - target[2]) ** 2) ** 0.5
            if best_dist is None or dist < best_dist:
                best, best_dist = role, dist
        if best is not None and best_dist is not None \
                and best_dist <= _NEAREST_ROLE_MAX_DISTANCE:
            counts[best] += 1
        else:
            other += 1
    total = len(pixels)
    shares = {role: counts[role] / total for role in roles}
    shares["other"] = other / total
    return {"shares": shares, "pixels": total, "path": str(path)}
