"""Responsive and cross-viewport judger (stage 23).

Breakpoints only where content breaks (not fixed device widths),
tap targets >= 44px (WCAG floor 24px) with >= 8px gaps, primary CTA in
the thumb zone on mobile, mobile-first base styles, no hover-only
essential content, safe-area insets and dvh/svh. The viewport sweep is
1440/768/375: without screenshots the sweep is static (CSS-based) and
pixel-dependent checks (R096 thumb zone) go in ``unchecked``. R040
(Fitts) reads declared target sizes and corner placement from CSS and,
when the coordinator supplies layout boxes, measures the real boxes and
the trigger-to-dismiss distance. R096 honours layout boxes at viewport
375 whenever they arrive alongside screenshots.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .common import (
    SCREENSHOT_DEPENDENT, Project, decls_for, load_project, make_finding,
    make_verdict, px_value, screenshot_status, unchecked_for,
    unjudged_verdict,
)
from ..types import Finding, Verdict

JUDGER = "responsive"
_DEP = SCREENSHOT_DEPENDENT[JUDGER]
VIEWPORTS = (1440, 768, 375)
_DEVICE_WIDTHS = {320, 360, 375, 390, 414, 430, 768, 820, 1024, 1280, 1440}
_TAP_MIN = 44.0
_TAP_FLOOR = 24.0


def _f(rule: str, severity: str, location: str, expected: str,
       actual: str, evidence: str) -> Finding:
    return make_finding(rule, JUDGER, severity, location, expected, actual,
                        evidence)


def _breakpoint_widths(project: Project) -> list[tuple[float, str, int]]:
    out = []
    for rule in project.css_rules:
        if not rule.media:
            continue
        for match in re.finditer(r"(?:min|max)-width\s*:\s*([\d.]+)px",
                                 rule.media):
            out.append((float(match.group(1)), rule.source, rule.line))
    return out


def _check_breakpoints(project: Project, out: list[Finding]) -> None:
    for width, source, line in _breakpoint_widths(project):
        if width in _DEVICE_WIDTHS:
            out.append(_f("R006", "change", f"{source}:{line}",
                          "a breakpoint only where the content actually "
                          "breaks, not at a fixed device width",
                          f"breakpoint at {width:g}px — a fixed device width "
                          "with no stated content reason",
                          f"@media breakpoint {width:g}px in {source}:{line}"))


def _tap_size(decls: dict[str, str]) -> float | None:
    for prop in ("min-height", "height"):
        if prop in decls:
            size = px_value(decls[prop].strip())
            if size is not None:
                return size
    return None


def _check_tap_targets(project: Project, out: list[Finding]) -> None:
    checked_any = False
    for rule in project.css_rules:
        selectors = " ".join(rule.selectors)
        if not any(word in selectors for word in
                   ("button", ".btn", "a[", "input", ".nav", "nav a")):
            continue
        size = _tap_size(rule.decls())
        if size is None:
            continue
        checked_any = True
        if size < _TAP_FLOOR:
            out.append(_f("R095", "defect", f"{rule.source}:{rule.line}",
                          "touch targets at least 44x44px (WCAG 2.2 AA "
                          "floor 24x24px)",
                          f"tap target {size:g}px tall",
                          f"{selectors}: height {size:g}px"))
        elif size < _TAP_MIN:
            out.append(_f("R095", "change", f"{rule.source}:{rule.line}",
                          "touch targets at least 44x44px",
                          f"tap target {size:g}px tall (below 44px, above "
                          "the 24px WCAG floor)",
                          f"{selectors}: height {size:g}px"))
    if not checked_any:
        out.append(_f("R095", "change", "tap targets",
                      "touch targets at least 44x44px with 8px gaps",
                      "no tap-target size declared for buttons/links",
                      "no height/min-height on button/link rules"))


def _check_thumb_zone(project: Project, out: list[Finding],
                      *, layout: Any = None,
                      measured: dict[str, Any] | None = None) -> None:
    """R096: primary CTA in the bottom-centre thumb zone on mobile.

    With layout boxes at viewport 375 the real primary-CTA box is
    measured (centre below 60% of the viewport height, x centre within
    the middle 60% horizontally); otherwise the static check runs (the
    primary CTA must live in a bottom-anchored container). Sets
    ``measured["layout_basis"]`` to "layout" or "css".
    """
    width, boxes = _layout_viewport(layout)
    if boxes:
        height, basis = _viewport_height(boxes, layout, width)
        cta = next((b for b in boxes if b["name"] == "primary_cta"), None)
        if cta is not None and height is not None:
            if measured is not None:
                measured["layout_basis"] = "layout"
                measured["viewport_height_px"] = height
                measured["viewport_height_basis"] = basis
            cx = cta["x"] + cta["width"] / 2
            cy = cta["y"] + cta["height"] / 2
            if not (cy > 0.6 * height
                    and 0.2 * width <= cx <= 0.8 * width):
                out.append(_f("R096", "change", f"mobile {width}px",
                              "primary actions in the bottom-centre thumb "
                              "zone (centre below 60% of viewport height, "
                              "within the middle 60% horizontally)",
                              f"primary CTA centre at ({cx:g}, {cy:g})px — "
                              "outside the thumb zone",
                              f"primary_cta box x={cta['x']:g} "
                              f"y={cta['y']:g} w={cta['width']:g} "
                              f"h={cta['height']:g}; viewport "
                              f"{width}x{height:g}px ({basis})"))
            return
    if measured is not None:
        measured["layout_basis"] = "css"
    bottomed = any(
        "fixed" in r.decls().get("position", "")
        and "bottom" in r.decls()
        and any("cta" in s or "btn-primary" in s for s in r.selectors)
        for r in project.css_rules)
    if not bottomed:
        out.append(_f("R096", "change", "mobile",
                      "primary actions in the bottom-centre thumb zone",
                      "primary CTA is not in a bottom-anchored container",
                      "no bottom-anchored .cta/.btn-primary rule in CSS"))


def _layout_viewport(layout: Any) -> tuple[int, list[dict[str, float]]]:
    """(viewport width, boxes) for the mobile layout: 375 when supplied,
    else the narrowest supplied width; ([], no boxes) without layout."""
    if not isinstance(layout, dict):
        return 375, []
    widths: list[int] = []
    for key in layout:
        try:
            widths.append(int(str(key)))
        except ValueError:
            continue
    if not widths:
        return 375, []
    width = 375 if 375 in widths else min(widths)
    return width, _layout_boxes(layout, width)


def _layout_boxes(layout: Any, width: int) -> list[dict[str, float]]:
    """Defensively parse the box list for one viewport width.

    Boxes are dicts with name/x/y/width/height in CSS px; anything
    malformed is skipped, never guessed at.
    """
    if not isinstance(layout, dict):
        return []
    boxes = layout.get(str(width))
    if boxes is None:
        for key, value in layout.items():
            if str(key) == str(width):
                boxes = value
                break
    if not isinstance(boxes, list):
        return []
    out: list[dict[str, float]] = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        try:
            out.append({"name": str(box.get("name", "")),
                        "x": float(box["x"]), "y": float(box["y"]),
                        "width": float(box["width"]),
                        "height": float(box["height"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _viewport_height(boxes: list[dict[str, float]], layout: Any,
                     width: int) -> tuple[float | None, str]:
    """Viewport height for the thumb-zone y test, with its basis stated.

    The layout contract carries no viewport height, so: an explicit
    ``viewport_heights`` entry or a box named "viewport" wins when
    supplied; otherwise the tallest box bottom is the visible extent
    (every supplied box lies within the viewport, so this is a lower
    bound on the true height and the y test is lenient, never strict).
    """
    if isinstance(layout, dict):
        heights = layout.get("viewport_heights")
        if isinstance(heights, dict):
            raw = heights.get(str(width))
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw), "supplied viewport_heights"
    for box in boxes:
        if box["name"] == "viewport" and box["height"] > 0:
            return box["height"], "viewport box"
    bottoms = [b["y"] + b["height"] for b in boxes]
    if bottoms:
        return max(bottoms), "tallest box bottom"
    return None, "unknown"


def _is_primary_target(selectors: list[str]) -> bool:
    """Buttons, links and close/dismiss controls are primary targets."""
    joined = " ".join(selectors).lower()
    if "button" in joined or ".btn" in joined or "a[" in joined:
        return True
    return _has_target_word(joined, "close") \
        or _has_target_word(joined, "dismiss") or "a" in selectors


def _has_target_word(joined: str, word: str) -> bool:
    """``word`` as a class/attribute segment (``.modal-close`` matches
    "close"; ``.enclosed`` does not)."""
    return re.search(rf"(?:^|[\.\-_\[]){word}\b", joined) is not None


def _target_size(decls: dict[str, str]) -> float | None:
    """Smallest declared px dimension (width/min-width/height/min-height),
    or None when no dimension is declared — never inferred."""
    sizes = []
    for prop in ("width", "min-width", "height", "min-height"):
        if prop in decls:
            size = px_value(decls[prop].strip())
            if size is not None:
                sizes.append(size)
    return min(sizes) if sizes else None


def _check_fitts(project: Project, out: list[Finding],
                 layout: Any = None) -> None:
    """R040 (Fitts): important targets large and close.

    Size violations are defects: any declared primary-target dimension
    under 44px. Distance violations are change findings: a small
    dismiss/close control pinned to a far top corner of a modal (static
    position declarations as evidence), or — with layout boxes — a small
    close control measured far from the primary CTA that triggers it.
    """
    for rule in project.css_rules:
        if not _is_primary_target(rule.selectors):
            continue
        size = _target_size(rule.decls())
        if size is None or size >= _TAP_MIN:
            continue
        out.append(_f("R040", "defect", f"{rule.source}:{rule.line}",
                      "important targets at least 44px (Fitts: larger "
                      "targets are faster to acquire)",
                      f"primary target {' '.join(rule.selectors)} is "
                      f"{size:g}px",
                      f"{' '.join(rule.selectors)}: smallest declared "
                      f"dimension {size:g}px in {rule.source}:{rule.line}"))
    _check_fitts_corner(project, out)
    _check_fitts_layout(layout, out)


def _check_fitts_corner(project: Project, out: list[Finding]) -> None:
    """Static half of the R040 distance check: a small close/dismiss
    control absolutely positioned in a top corner of a modal. The
    modal's trigger lives in page content, so a corner-pinned dismiss
    control is far from it by construction; the position declarations
    are the measured evidence."""
    has_modal = any(
        e.tag == "dialog" or e.attrs.get("role") == "dialog"
        or "modal" in e.attrs.get("class", "")
        for _, doc in project.html for e in doc.elements)
    if not has_modal:
        return
    for rule in project.css_rules:
        joined = " ".join(rule.selectors).lower()
        if not _has_target_word(joined, "close") \
                and not _has_target_word(joined, "dismiss"):
            continue
        decls = rule.decls()
        if decls.get("position") not in {"absolute", "fixed"}:
            continue
        top = px_value(decls.get("top", ""))
        if top is None or top > 16:
            continue
        side = next((prop for prop in ("left", "right")
                     if (v := px_value(decls.get(prop, ""))) is not None
                     and v <= 16), None)
        if side is None:
            continue
        size = _target_size(decls)
        if size is None or size >= _TAP_MIN:
            continue
        out.append(_f("R040", "change", f"{rule.source}:{rule.line}",
                      "small dismiss controls close to the control that "
                      "opens them, not pinned to a far corner",
                      f"{size:g}px close control pinned to the top-{side} "
                      f"corner of a modal (top: {top:g}px)",
                      f"position: {decls['position']}; top: "
                      f"{decls.get('top')}; {side}: {decls.get(side)} in "
                      f"{rule.source}:{rule.line}"))


def _check_fitts_layout(layout: Any, out: list[Finding]) -> None:
    """Layout half of R040: measure real target boxes at the mobile
    viewport, and the trigger-to-dismiss distance where both boxes are
    supplied. A box is 'far' from its trigger when the centre distance
    exceeds one viewport width."""
    width, boxes = _layout_viewport(layout)
    if not boxes:
        return
    for box in boxes:
        if box["name"] != "primary_cta" and "close" not in box["name"] \
                and "dismiss" not in box["name"]:
            continue
        size = min(box["width"], box["height"])
        if size < _TAP_MIN:
            out.append(_f("R040", "defect", f"mobile {width}px",
                          "important targets at least 44px",
                          f"{box['name']} is {box['width']:g}x"
                          f"{box['height']:g}px",
                          f"layout box {box['name']}: "
                          f"{box['width']:g}x{box['height']:g}px at "
                          f"{width}px viewport"))
    trigger = next((b for b in boxes if b["name"] == "primary_cta"), None)
    if trigger is None:
        return
    tx = trigger["x"] + trigger["width"] / 2
    ty = trigger["y"] + trigger["height"] / 2
    for box in boxes:
        if "close" not in box["name"] and "dismiss" not in box["name"]:
            continue
        if min(box["width"], box["height"]) >= _TAP_MIN:
            continue
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        distance = math.hypot(cx - tx, cy - ty)
        if distance <= width:
            continue
        out.append(_f("R040", "change", f"mobile {width}px",
                      "small dismiss controls close to the control that "
                      "opens them",
                      f"{min(box['width'], box['height']):g}px "
                      f"{box['name']} is {distance:.0f}px from the "
                      "primary CTA",
                      f"{box['name']} centre ({cx:g}, {cy:g}); "
                      f"primary_cta centre ({tx:g}, {ty:g}); distance "
                      f"{distance:.0f}px at {width}px viewport"))


def _check_mobile_first(project: Project, out: list[Finding]) -> None:
    media = [r.media or "" for r in project.css_rules if r.media]
    has_min = any("min-width" in m for m in media)
    has_max = any("max-width" in m for m in media)
    if has_max and not has_min:
        out.append(_f("R097", "defect", "responsive",
                      "design for the smallest screen first, then add "
                      "complexity as space grows (mobile-first)",
                      "only max-width queries: desktop-first",
                      "max-width media queries with no min-width queries"))


def _check_hover_only(project: Project, out: list[Finding]) -> None:
    for rule in project.css_rules:
        if ":hover" not in " ".join(rule.selectors):
            continue
        decls = rule.decls()
        reveals = decls.get("display") in {"block", "flex", "grid"} or \
            decls.get("visibility") == "visible" or \
            decls.get("opacity") in {"1", "1.0"}
        if not reveals:
            continue
        base = " ".join(rule.selectors).replace(":hover", "")
        has_focus = any(":focus" in " ".join(r.selectors)
                        and base.strip() in " ".join(r.selectors)
                        for r in project.css_rules)
        if not has_focus:
            out.append(_f("R098", "defect", f"{rule.source}:{rule.line}",
                          "never hide essential information or actions "
                          "behind hover",
                          ":hover reveals content with no :focus equivalent",
                          f"{' '.join(rule.selectors)} in {rule.source}:{rule.line}"))


def _check_safe_area(project: Project, out: list[Finding]) -> None:
    css = project.css_text
    for rule in project.css_rules:
        decls = rule.decls()
        if decls.get("position") in {"fixed", "sticky"}:
            joined = " ".join(f"{d.property}: {d.value}"
                              for d in rule.declarations)
            if "safe-area-inset" not in joined:
                out.append(_f("R099", "change", f"{rule.source}:{rule.line}",
                              "env(safe-area-inset-*) for fixed/sticky elements",
                              f"{' '.join(rule.selectors)} is "
                              f"{decls['position']} with no safe-area inset",
                              f"position: {decls['position']} in "
                              f"{rule.source}:{rule.line}"))
                break
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property == "height" and decl.value.strip() == "100vh":
                out.append(_f("R099", "defect", f"{rule.source}:{rule.line}",
                              "dvh/svh instead of vh for full-screen layouts",
                              "height: 100vh with no dvh/svh",
                              f"height: 100vh in {rule.source}:{rule.line}"))
                return
    if "100dvh" not in css and "100svh" not in css and "100vh" in css:
        pass  # covered above by the per-rule finding


def judge(project_dir: str | Path, *, tokens: Any = None,
          screenshots: Any = None, live_metrics: Any = None,
          brief: Any = None, content_draft: Any = None,
          layout: Any = None, axe_results: Any = None) -> Verdict:
    """Judge one built site across the 1440/768/375 sweep (static when no
    screenshots). See module docstring. ``layout`` is the optional
    layout-box dict ({viewport width: [boxes]}) from the live browser;
    it extends R040 and R096 with measured boxes whenever supplied.
    ``axe_results`` is accepted (and ignored) so the coordinator can
    pass the same optional inputs to every judger through
    :func:`judge_all` without a per-judger filter."""
    project = load_project(project_dir)
    if project is None:
        return unjudged_verdict(JUDGER)
    findings: list[Finding] = []
    checked: list[str] = []
    measured: dict[str, Any] = {"pages": len(project.html),
                                "viewports": list(VIEWPORTS)}
    status, missing = screenshot_status(screenshots)
    extra: set[str] = set()
    if status == "missing":
        extra |= _DEP
    elif status == "broken":
        for rule in sorted(_DEP):
            findings.append(_f(rule, "defect", "screenshots",
                               "rendered screenshots at 1440/768/375",
                               f"screenshot evidence unreadable: "
                               f"{', '.join(missing)}",
                               f"missing screenshot(s): {', '.join(missing)}"))
            checked.append(rule)
    else:
        checked.extend(sorted(_DEP))
        _check_thumb_zone(project, findings, layout=layout,
                          measured=measured)
    _check_breakpoints(project, findings)
    _check_tap_targets(project, findings)
    _check_fitts(project, findings, layout)
    _check_mobile_first(project, findings)
    _check_hover_only(project, findings)
    _check_safe_area(project, findings)
    checked.extend(("R006", "R040", "R095", "R097", "R098", "R099"))
    return make_verdict(
        JUDGER, findings, unchecked_for(JUDGER, extra=extra),
        measured=measured,
        checked=sorted(set(checked), key=lambda r: int(r[1:])),
    )
