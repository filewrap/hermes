"""Visual judger (stage 19): design fidelity and layout.

Checks the visual rules R001-R005, R007-R008, R009-R018, R019-R032,
R033-R039, R062-R070, R090-R094 against the built site's HTML/CSS and
tokens. R002 needs a human eye and is always unchecked. R001, R005 and
R037 need rendered layout: without screenshots they are unchecked; with
screenshots the check is that rendered evidence exists (pixel
measurement needs a live browser, Phase 3 — never an invented number).

Phase 3 measures for real. R001, R005 and R037 are checked from static
CSS when no layout is supplied (``measured["layout_basis"] == "css"``)
and from real element boxes when the coordinator passes ``layout``
(``"layout"``) — see ``rendered.layout_boxes`` for the contract. R019
is measured from screenshot pixels with PIL when screenshots are
supplied (``measured["r019_basis"] == "rendered"``) and otherwise stays
the CSS declaration-count estimate, labelled as an estimate. The
Gestalt rules R062-R070 and R093 are checked from CSS, DOM structure
and paired animation declarations.

Severity: a blocker breaks layout, hides content, or contradicts the
approved direction; visual drift from tokens is a defect, not an
observation (audit 6.19).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Any

from .common import (
    SCREENSHOT_DEPENDENT, HtmlElement, Project, contrast_ratio, custom_properties,
    decls_for, hue_of, is_neutral, load_project, make_finding, make_verdict,
    parse_color, px_or_relative, px_value, relative_luminance,
    screenshot_status, unchecked_for, unjudged_verdict,
)
from .checkers import apply_visual_checkers
from .rendered import box_by_name, box_centre, layout_boxes, measure_colour_shares
from ..tokens import token_drift
from ..types import Finding, Verdict

JUDGER = "visual"
_DEP = SCREENSHOT_DEPENDENT[JUDGER]
_VALUE_NAME_RE = re.compile(
    r"^--(?:color-)?(?:blue|red|green|gray|grey|amber|purple|pink|orange|"
    r"yellow|teal|cyan|indigo|violet|lime|stone|zinc|slate|neutral)-\d{2,3}$",
    re.IGNORECASE,
)
_ALLOWED_RATIOS = (1.125, 1.2, 1.25, 1.333)
_DURATION_RE = re.compile(r"([\d.]+)(m?s)")
_PX_NUM_RE = re.compile(r"([\d.]+)px")
_LAYOUT_PROPS = ("width", "height", "top", "left", "right", "bottom",
                 "margin", "padding")
_SEMANTIC_HUES = {"error": (345, 15), "success": (100, 160),
                  "warning": (30, 55), "info": (200, 250)}


def _f(rule: str, severity: str, location: str, expected: str,
       actual: str, evidence: str) -> Finding:
    return make_finding(rule, JUDGER, severity, location, expected, actual,
                        evidence)


def _body_font(project: Project) -> tuple[float | None, float | None]:
    """(body font-size px, body line-height unitless) from body/html rules."""
    size = line = None
    for selector in ("body", "html", ":root"):
        decls = decls_for(project.css_rules, selector)
        if size is None and "font-size" in decls:
            size = px_or_relative(decls["font-size"])
        if line is None and "line-height" in decls:
            line = _line_height_unitless(decls["line-height"], size)
    return size, line


def _line_height_unitless(value: str, font_px: float | None) -> float | None:
    text = value.strip()
    if re.fullmatch(r"[\d.]+", text):
        return float(text)
    px = px_value(text)
    if px is not None and font_px:
        return px / font_px
    return None


def _heading_sizes(project: Project) -> dict[str, float]:
    out = {}
    for tag in ("h1", "h2", "h3"):
        decls = decls_for(project.css_rules, tag)
        if "font-size" in decls:
            size = px_or_relative(decls["font-size"])
            if size is not None:
                out[tag] = size
    return out


def _palette(project: Project) -> Counter:
    """Counts of resolved colours in CSS, excluding semantic-signal rules."""
    counts: Counter = Counter()
    props = custom_properties(project.css_rules)
    for rule in project.css_rules:
        if any(word in " ".join(rule.selectors)
               for word in ("success", "error", "warning", "info", "alert", "danger")):
            continue
        for decl in rule.declarations:
            if decl.property not in {"color", "background-color", "background",
                                     "border-color"}:
                continue
            value = decl.value
            var = re.search(r"var\(\s*(--[\w-]+)", value)
            if var and var.group(1) in props:
                value = props[var.group(1)]
            rgb = parse_color(value.split()[0])
            if rgb is not None:
                counts[rgb] += 1
    return counts


def _hues(counts: Counter) -> list[float]:
    hues = set()
    for rgb in counts:
        if is_neutral(rgb):
            continue
        hue = hue_of(rgb)
        if hue >= 0:
            hues.add(round(hue))
    return sorted(hues)


def _check_grid(project: Project, out: list[Finding]) -> None:
    grids = [r for r in project.css_rules
             if "grid-template-columns" in r.decls()]
    twelve = any("repeat(12" in r.decls()["grid-template-columns"]
                 or len(r.decls()["grid-template-columns"].split()) == 12
                 for r in grids)
    if not twelve:
        out.append(_f("R003", "defect", "layout",
                      "12-column grid with 20-24px gutters",
                      "no 12-column grid found in CSS",
                      "grid-template-columns: none with 12 tracks"))
        return
    for rule in grids:
        gap = rule.decls().get("gap") or rule.decls().get("column-gap", "")
        px = px_value(gap.split()[0]) if gap else None
        if px is not None and not 20 <= px <= 24:
            out.append(_f("R003", "defect", f"{rule.source}:{rule.line}",
                          "gutters 20-24px", f"gutter {px}px",
                          f"gap: {gap} in {rule.source}:{rule.line}"))
            return


def _check_baseline(project: Project, out: list[Finding]) -> None:
    size, line = _body_font(project)
    if size is None or line is None:
        return
    baseline = size * line
    if abs(baseline % 4) > 0.01 and abs(baseline % 4 - 4) > 0.01:
        out.append(_f("R004", "defect", "body",
                      "vertical spacing on a 4pt baseline (line-height "
                      "divisible by 4)",
                      f"body line-height computes to {baseline:.1f}px",
                      f"font-size {size}px, line-height {line}"))


def _check_grid_vs_flex(project: Project, out: list[Finding]) -> None:
    has_grid = any("grid" in d.value for r in project.css_rules
                   for d in r.declarations if d.property == "display")
    if has_grid:
        return
    for selector in ("body", "main", ".page", ".layout"):
        decls = decls_for(project.css_rules, selector)
        if decls.get("display") == "flex":
            out.append(_f("R007", "defect", selector,
                          "CSS Grid for 2D page structure, Flexbox for 1D "
                          "component internals",
                          f"{selector} builds the page with Flexbox alone",
                          f"display: flex in {selector}"))
            return


def _check_subgrid(project: Project, out: list[Finding]) -> None:
    has_card = any("card" in " ".join(r.selectors) for r in project.css_rules)
    if not has_card:
        return
    if "subgrid" not in project.css_text:
        out.append(_f("R008", "change", ".card",
                      "subgrid so titles, descriptions and CTAs align "
                      "across cards in a row",
                      "cards exist but no subgrid is used",
                      "no 'subgrid' in project CSS"))


def _check_body_type(project: Project, out: list[Finding]) -> None:
    size, line = _body_font(project)
    if size is not None and size < 16:
        out.append(_f("R009", "defect", "body",
                      "body text at 16px minimum",
                      f"body font-size {size}px",
                      f"font-size: {size}px on body"))
    if line is not None and not 1.5 <= line <= 1.7:
        out.append(_f("R010", "defect", "body",
                      "body line-height 1.5-1.7 (unitless)",
                      f"body line-height {line}",
                      f"line-height: {line} on body"))
    for selector in ("body", "main", ".content", ".container", "article"):
        decls = decls_for(project.css_rules, selector)
        width = decls.get("max-width", "")
        if not width:
            continue
        ch = re.fullmatch(r"([\d.]+)ch", width.strip())
        if ch and not 45 <= float(ch.group(1)) <= 75:
            out.append(_f("R011", "defect", selector,
                          "body lines 45-75 characters (66 ideal)",
                          f"max-width {width}", f"max-width: {width}"))
        px = px_value(width.strip())
        if px is not None and not 360 <= px <= 900:
            out.append(_f("R011", "defect", selector,
                          "body lines 45-75 characters (66 ideal)",
                          f"max-width {width}", f"max-width: {width}"))
        break
    else:
        out.append(_f("R011", "change", "body",
                      "body lines 45-75 characters (max-width ~66ch)",
                      "no max-width on body text containers",
                      "no max-width declaration on body/main/.content"))


def _check_type_scale(project: Project, out: list[Finding]) -> None:
    sizes = sorted({px_or_relative(v) for r in project.css_rules
                    if r.media is None
                    for d in r.declarations
                    if d.property == "font-size"
                    for v in [d.value] if px_or_relative(v) is not None})
    if len(sizes) < 3:
        return
    allowed = set(_ALLOWED_RATIOS)
    for a in _ALLOWED_RATIOS:
        for b in _ALLOWED_RATIOS:
            allowed.add(round(a * b, 3))
    for low, high in pairwise(sizes):
        ratio = high / low
        if min(abs(ratio - a) for a in allowed) > 0.03:
            out.append(_f("R012", "defect", "type scale",
                          "font sizes from one ratio (1.125/1.2/1.25/1.333)",
                          f"size step {low:g}px -> {high:g}px is ratio "
                          f"{ratio:.2f}",
                          f"font sizes: {', '.join(f'{s:g}' for s in sizes)}"))
            return


def _check_families(project: Project, out: list[Finding]) -> None:
    families = set()
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property == "font-family":
                first = decl.value.split(",")[0].strip().strip("'\"")
                families.add(first.lower())
    if len(families) > 2:
        out.append(_f("R013", "defect", "typography",
                      "at most two typefaces per site",
                      f"{len(families)} families: {', '.join(sorted(families))}",
                      "font-family declarations across project CSS"))


def _check_heading_type(project: Project, out: list[Finding]) -> None:
    sizes = _heading_sizes(project)
    for tag in ("h1", "h2", "h3"):
        decls = decls_for(project.css_rules, tag)
        if "line-height" not in decls:
            continue
        line = _line_height_unitless(decls["line-height"], sizes.get(tag))
        if line is not None and not 1.05 <= line <= 1.25:
            out.append(_f("R014", "defect", tag,
                          "heading line-height 1.05-1.25",
                          f"{tag} line-height {line}",
                          f"line-height: {decls['line-height']} on {tag}"))
    for tag in ("h1", "h2", "h3"):
        decls = decls_for(project.css_rules, tag)
        size = sizes.get(tag)
        tracking = decls.get("letter-spacing", "")
        if size is not None and size >= 32:
            em = re.fullmatch(r"(-?[\d.]+)em", tracking.strip())
            if not em or not -0.05 <= float(em.group(1)) <= 0:
                out.append(_f("R015", "change", tag,
                              "large type tightened -0.01 to -0.04em",
                              f"{tag} at {size:g}px has letter-spacing "
                              f"{tracking or 'none'}",
                              f"{tag} font-size {size:g}px"))
        if decls.get("text-transform") == "uppercase":
            em = re.fullmatch(r"([\d.]+)em", tracking.strip())
            if not em or float(em.group(1)) < 0.05:
                out.append(_f("R015", "change", tag,
                              "ALL-CAPS text tracked +0.05 to +0.1em",
                              f"{tag} uppercase with letter-spacing "
                              f"{tracking or 'none'}",
                              f"text-transform: uppercase on {tag}"))
    if any("clamp(" in d.value for r in project.css_rules
           for d in r.declarations if d.property == "font-size"):
        for rule in project.css_rules:
            for decl in rule.declarations:
                if decl.property != "font-size":
                    continue
                clamp = re.search(r"clamp\(\s*([\d.]+)px", decl.value)
                if clamp and float(clamp.group(1)) < 16:
                    out.append(_f("R016", "defect", f"{rule.source}:{rule.line}",
                                  "clamp() never drops body text below 16px",
                                  f"clamp minimum {clamp.group(1)}px",
                                  f"font-size: {decl.value}"))
                    return
    else:
        out.append(_f("R016", "change", "typography",
                      "scale type fluidly with clamp()",
                      "no clamp() used for any font-size",
                      "no clamp() in project CSS"))
    ordered = [sizes[t] for t in ("h1", "h2", "h3") if t in sizes]
    if len(ordered) >= 2 and any(a <= b for a, b in pairwise(ordered)):
        out.append(_f("R017", "defect", "headings",
                      "H1 largest, then H2, H3, body",
                      "heading sizes not strictly decreasing: "
                      + ", ".join(f"{s:g}px" for s in ordered),
                      "h1/h2/h3 font-size declarations"))
    for selector in ("body", "p"):
        decls = decls_for(project.css_rules, selector)
        if decls.get("text-align") in {"justify", "center"}:
            out.append(_f("R018", "defect", selector,
                          "left-align (start-align) body copy",
                          f"{selector} text-align: {decls['text-align']}",
                          f"text-align: {decls['text-align']} on {selector}"))


def _check_colour(project: Project, out: list[Finding]) -> None:
    # R019 (60-30-10) lives in _check_r019: rendered pixel measurement
    # when screenshots are supplied, otherwise the labelled CSS estimate.
    counts = _palette(project)
    hues = _hues(counts)
    if len(hues) > 3:
        out.append(_f("R022", "defect", "colour",
                      "at most 3 hues plus neutrals",
                      f"{len(hues)} hues: {', '.join(str(h) for h in hues)}",
                      "hue counts from CSS colour declarations"))
    if len(hues) >= 2:
        dominant = hue_of(counts.most_common(1)[0][0]) if counts else -1
        others = [h for h in hues if h != round(dominant)]
        accent = min(others, key=lambda h: counts.get(h, 0)) if others else None
        if dominant >= 0 and accent is not None:
            dist = abs((accent - dominant + 180) % 360 - 180)
            if 45 < dist < 135:
                out.append(_f("R020", "change", "colour",
                              "a single accent uses the complementary hue "
                              "(180 degrees opposite the dominant)",
                              f"accent hue {accent} is {dist:.0f} degrees "
                              f"from dominant hue {round(dominant)}",
                              "hue analysis of CSS colour declarations"))
        span = (max(hues) - min(hues)) if hues else 0
        complementary = any(
            abs((b - a + 180) % 360 - 180) > 165
            for a in hues for b in hues if a != b)
        if span > 90 and not complementary:
            out.append(_f("R021", "change", "colour",
                          "analogous hues (within ~30 degrees) or one hue",
                          f"palette hues span {span} degrees with no "
                          "complementary pair",
                          f"hues: {', '.join(str(h) for h in hues)}"))
    for rule in project.css_rules:
        selectors = " ".join(rule.selectors)
        for role, (low, high) in _SEMANTIC_HUES.items():
            if role not in selectors:
                continue
            for decl in rule.declarations:
                if decl.property not in {"color", "background-color"}:
                    continue
                rgb = parse_color(decl.value.split()[0])
                if rgb is None or is_neutral(rgb):
                    continue
                hue = hue_of(rgb)
                in_range = low <= hue <= high if low < high else hue >= low or hue <= high
                if not in_range:
                    out.append(_f("R023", "defect", f"{rule.source}:{rule.line}",
                                  f"semantic meanings consistent: {role} "
                                  "keeps its hue",
                                  f".{role} rule uses hue {hue:.0f}",
                                  f"{decl.property}: {decl.value}"))
                    break
    for rule in project.css_rules:
        selectors = " ".join(rule.selectors)
        if not any(w in selectors for w in ("error", "success", "warning")):
            continue
        decls = rule.decls()
        if "color" in decls and not any(
                k in decls for k in ("border", "border-left", "font-weight")):
            if not any("::before" in s for s in rule.selectors):
                out.append(_f("R024", "change", f"{rule.source}:{rule.line}",
                              "pair every colour signal with an icon, label, "
                              "shape or pattern — never colour alone",
                              "colour-only signal (no border, weight or icon)",
                              f"{selectors}: color {decls['color']}"))
                break


def _check_dark_mode(project: Project, tokens: Any, out: list[Finding]) -> None:
    dark_rules = [r for r in project.css_rules
                  if (r.media and "prefers-color-scheme: dark" in r.media)
                  or any('[data-theme="dark"]' in s or "[data-theme='dark']" in s
                         for s in r.selectors)]
    dark_tokens = isinstance(tokens, dict) and bool(
        (tokens.get("themes") or {}).get("dark"))
    if not dark_rules and not dark_tokens:
        return
    for rule in dark_rules:
        decls = rule.decls()
        bg = decls.get("background-color") or decls.get("background", "")
        rgb = parse_color(bg.split()[0]) if bg else None
        if rgb == (0, 0, 0):
            out.append(_f("R025", "defect", f"{rule.source}:{rule.line}",
                          "dark base near-black (#121212), not pure black",
                          "dark background is pure #000000",
                          f"background: {bg} in dark theme"))
        text = decls.get("color", "")
        rgb = parse_color(text.split()[0]) if text else None
        if rgb == (255, 255, 255):
            out.append(_f("R026", "defect", f"{rule.source}:{rule.line}",
                          "dark text off-white (~87% opacity), not pure white",
                          "dark text is pure #ffffff",
                          f"color: {text} in dark theme"))


def _check_token_naming(project: Project, out: list[Finding]) -> None:
    for name in custom_properties(project.css_rules):
        if _VALUE_NAME_RE.match(name):
            out.append(_f("R027", "defect", "tokens",
                          "name colours by role (--color-surface), never by "
                          "value (--blue-500)",
                          f"value-named custom property {name}",
                          f"custom property {name} in project CSS"))
            return


def _spacing_values(project: Project) -> list[tuple[float, str, int]]:
    out = []
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property not in {"margin", "margin-top", "margin-bottom",
                                     "margin-left", "margin-right", "padding",
                                     "padding-top", "padding-bottom",
                                     "padding-left", "padding-right", "gap",
                                     "row-gap", "column-gap"}:
                continue
            for match in _PX_NUM_RE.finditer(decl.value):
                out.append((float(match.group(1)), rule.source, rule.line))
    return out


def _check_spacing(project: Project, out: list[Finding]) -> None:
    values = _spacing_values(project)
    bad = [(v, s, ln) for v, s, ln in values if v % 4 != 0]
    if bad:
        value, source, line = bad[0]
        out.append(_f("R028", "defect", f"{source}:{line}",
                      "margins, padding, gaps multiples of 8px (4px only "
                      "as a half-step)",
                      f"spacing {value:g}px is not a multiple of 4 "
                      f"({len(bad)} offending value(s))",
                      f"{value:g}px in {source}:{line}"))
    paddings = [v for r in project.css_rules
                if any("card" in s or "button" in s or "btn" in s
                       for s in r.selectors)
                for v, _, _ in [(float(m.group(1)), r.source, r.line)
                                for m in _PX_NUM_RE.finditer(
                                    r.decls().get("padding", ""))]]
    margins = [v for r in project.css_rules
               if any(s in ("section", ".section", "main") for s in r.selectors)
               for v, _, _ in [(float(m.group(1)), r.source, r.line)
                               for m in _PX_NUM_RE.finditer(
                                   r.decls().get("margin", ""))]]
    if paddings and margins and max(paddings) > min(margins):
        out.append(_f("R029", "change", "spacing",
                      "space inside a component <= space between components",
                      f"component padding up to {max(paddings):g}px exceeds "
                      f"section margin {min(margins):g}px",
                      "component padding vs section margin in project CSS"))
    gaps = set()
    for rule in project.css_rules:
        for key in ("gap", "column-gap"):
            raw = rule.decls().get(key, "")
            if raw:
                px = px_value(raw.split()[0])
                if px is not None:
                    gaps.add(px)
    if len(gaps) > 1:
        out.append(_f("R030", "defect", "layout",
                      "one gutter size within a layout",
                      f"{len(gaps)} gutter sizes: "
                      + ", ".join(f"{g:g}px" for g in sorted(gaps)),
                      "gap/column-gap declarations in project CSS"))
    size, line = _body_font(project)
    if size is not None and line is not None:
        expected = size * line
        decls = decls_for(project.css_rules, "p")
        margin = decls.get("margin-bottom", "")
        px = px_value(margin.strip())
        if px is not None and not 0.75 * expected - 0.5 <= px <= expected + 0.5:
            out.append(_f("R031", "change", "p",
                          f"paragraph spacing 75-100% of line-height "
                          f"({0.75 * expected:.0f}-{expected:.0f}px)",
                          f"paragraph margin-bottom {px:g}px",
                          f"margin-bottom: {margin} on p"))
    for selector in (".hero", "header.hero", ".hero-section"):
        decls = decls_for(project.css_rules, selector)
        padding = decls.get("padding", "")
        amounts = [float(m.group(1)) for m in _PX_NUM_RE.finditer(padding)]
        if amounts and max(amounts) < 32:
            out.append(_f("R032", "change", selector,
                          "generous whitespace around focal content",
                          f"hero padding only {max(amounts):g}px",
                          f"padding: {padding} on {selector}"))
            break


def _primary_ctas(doc: Any) -> list[HtmlElement]:
    out = []
    for el in doc.elements:
        classes = el.attrs.get("class", "")
        if el.tag in {"a", "button"} and (
                "btn-primary" in classes or "cta-primary" in classes
                or "primary-cta" in classes):
            out.append(el)
    return out


def _check_hierarchy(project: Project, out: list[Finding]) -> None:
    _check_hierarchy_order(project, out)
    _check_primary_cta_count(project, out)
    _check_accent_use(project, out)
    _check_scanning_aids(project, out)
    _check_first_viewport(project, out)


def _check_hierarchy_order(project: Project, out: list[Finding]) -> None:
    sizes = _heading_sizes(project)
    body_size, _ = _body_font(project)
    ordered = [s for s in (sizes.get("h1"), sizes.get("h2"), body_size)
               if s is not None]
    if len(ordered) >= 2 and any(a <= b for a, b in pairwise(ordered)):
        out.append(_f("R033", "defect", "hierarchy",
                      "order elements by importance: H1 > H2 > body",
                      "sizes not strictly decreasing: "
                      + ", ".join(f"{s:g}px" for s in ordered),
                      "h1/h2/body font-size declarations"))
    primary = decls_for(project.css_rules, ".btn-primary")
    secondary = decls_for(project.css_rules, ".btn-secondary")
    if primary and secondary:
        bg_p = primary.get("background-color") or primary.get("background", "")
        bg_s = secondary.get("background-color") or secondary.get("background", "")
        if bg_p and bg_p == bg_s:
            out.append(_f("R033", "defect", ".btn-primary",
                          "primary action visually dominant over secondary",
                          "primary and secondary CTAs share a background",
                          f"background {bg_p} on both"))


def _check_primary_cta_count(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        ctas = _primary_ctas(doc)
        if len(ctas) > 1:
            out.append(_f("R034", "defect", rel,
                          "one visually dominant primary action per view",
                          f"{len(ctas)} primary CTAs on the page",
                          f"{len(ctas)} .btn-primary/.cta-primary elements "
                          f"in {rel}"))
        elif not ctas:
            out.append(_f("R034", "change", rel,
                          "one visually dominant primary action per view",
                          "no primary CTA found",
                          f"no .btn-primary/.cta-primary element in {rel}"))


def _count_accent_backgrounds(project: Project) -> int:
    counts = _palette(project)
    if not counts:
        return 0
    accent = min((rgb for rgb in counts if not is_neutral(rgb)),
                 key=lambda rgb: counts[rgb], default=None)
    if accent is None:
        return 0
    total = 0
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property in {"background-color", "background"}:
                rgb = parse_color(decl.value.split()[0])
                if rgb == accent:
                    total += 1
    return total


def _check_accent_use(project: Project, out: list[Finding]) -> None:
    accent_rules = _count_accent_backgrounds(project)
    if accent_rules > 4:
        out.append(_f("R035", "change", "hierarchy",
                      "only one item visually distinct at a time",
                      f"accent colour used as background in {accent_rules} "
                      "rules",
                      "background declarations using the accent colour"))


def _check_scanning_aids(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        long_paras = [t for t, _ in doc.texts if len(t.split()) > 60]
        has_aids = bool(doc.find("h2") or doc.find("h3") or doc.find("ul"))
        if long_paras and not has_aids:
            out.append(_f("R036", "change", rel,
                          "headings, bullets and front-loaded lines so users "
                          "scan instead of F-scanning",
                          f"{len(long_paras)} long paragraph(s) with no "
                          "subheads or bullets",
                          f"paragraph of {len(long_paras[0].split())} words"))
            break
    for rel, doc in project.html:
        headings = [e for e in doc.elements if e.tag in {"h1", "h2", "h3"}]
        for el in headings:
            text = next((t for t, ln in doc.texts if ln == el.line), "")
            if not text.strip() or text.strip().lower() in {
                    "heading", "title", "welcome", "hello", "lorem ipsum"}:
                out.append(_f("R038", "defect", f"{rel}:{el.line}",
                              "strong, information-carrying headings",
                              f"{el.tag} is empty or generic: {text!r}",
                              f"<{el.tag}> at {rel}:{el.line}"))


def _non_hero_sections(elements: list, before: int) -> int:
    return sum(1 for e in elements[:before]
               if e.tag == "section"
               and "hero" not in e.attrs.get("class", ""))


def _check_first_viewport(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        if not doc.find("h1"):
            out.append(_f("R039", "defect", rel,
                          "most important information and primary action in "
                          "the first viewport",
                          "no h1 on the page",
                          f"no <h1> in {rel}"))
            continue
        # Static proxy for "first viewport": the headline and the primary
        # action must not be buried below a full section of other content.
        # A short page's nav pushes the h1 past half the element count, so
        # element position alone is not the test — sections are.
        elements = doc.elements
        h1_index = next(i for i, e in enumerate(elements) if e.tag == "h1")
        before_h1 = _non_hero_sections(elements, h1_index)
        if before_h1:
            out.append(_f("R039", "defect", rel,
                          "headline in the first viewport",
                          f"h1 appears after {before_h1} non-hero section(s)",
                          f"<h1> at {rel}:{doc.find('h1')[0].line}"))
        ctas = _primary_ctas(doc)
        if ctas:
            cta_index = next(i for i, e in enumerate(elements) if e in ctas)
            before_cta = _non_hero_sections(elements, cta_index)
            if before_cta >= 2:
                out.append(_f("R039", "defect", rel,
                              "primary action in the first viewport",
                              f"primary CTA appears after {before_cta} sections",
                              f"primary CTA at {rel}:{ctas[0].line}"))


def _duration_ms(value: str) -> float | None:
    match = _DURATION_RE.search(value)
    if not match:
        return None
    amount = float(match.group(1))
    return amount if match.group(2) == "ms" else amount * 1000


def _check_motion(project: Project, out: list[Finding]) -> None:
    _check_motion_durations(project, out)
    _check_layout_animation(project, out)
    if not any(r.media and "prefers-reduced-motion" in r.media
               for r in project.css_rules):
        out.append(_f("R092", "defect", "motion",
                      "respect prefers-reduced-motion: disable or replace "
                      "non-essential motion",
                      "no prefers-reduced-motion media query in CSS",
                      "no @media (prefers-reduced-motion) in project CSS"))
    _check_paired_durations(project, out)


def _overlong_duration(project: Project) -> tuple[str, int, str, float] | None:
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property not in {"transition-duration",
                                     "animation-duration", "transition",
                                     "animation"}:
                continue
            ms = _duration_ms(decl.value)
            if ms is not None and ms > 300:
                return rule.source, rule.line, decl.property, ms
    return None


def _check_motion_durations(project: Project, out: list[Finding]) -> None:
    found = _overlong_duration(project)
    if found is None:
        return
    source, line, prop, ms = found
    out.append(_f("R090", "defect", f"{source}:{line}",
                  "UI animations under 300ms",
                  f"{prop} of {ms:.0f}ms",
                  f"{prop}: {ms:.0f}ms in {source}:{line}"))


def _layout_animation(project: Project) -> tuple[str, int, str, str] | None:
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property not in {"transition", "transition-property",
                                     "animation"}:
                continue
            animated = [p for p in _LAYOUT_PROPS if p in decl.value]
            if animated:
                return rule.source, rule.line, decl.property, ", ".join(animated)
    return None


def _check_layout_animation(project: Project, out: list[Finding]) -> None:
    found = _layout_animation(project)
    if found is None:
        return
    source, line, prop, animated = found
    out.append(_f("R091", "defect", f"{source}:{line}",
                  "animate only transform and opacity, never layout properties",
                  f"animates {animated}",
                  f"{prop} animates {animated} in {source}:{line}"))


def _paired_durations(project: Project) -> dict[str, float]:
    pairs = (("modal", "overlay"), ("tooltip", "arrow"), ("drawer", "backdrop"))
    durations: dict[str, float] = {}
    for rule in project.css_rules:
        joined = " ".join(rule.selectors)
        for group in pairs:
            for name in group:
                if name not in joined:
                    continue
                for decl in rule.declarations:
                    if decl.property not in {"transition-duration",
                                             "animation-duration", "transition",
                                             "animation"}:
                        continue
                    ms = _duration_ms(decl.value)
                    if ms is not None:
                        durations.setdefault(name, ms)
    return durations


def _check_paired_durations(project: Project, out: list[Finding]) -> None:
    pairs = (("modal", "overlay"), ("tooltip", "arrow"), ("drawer", "backdrop"))
    durations = _paired_durations(project)
    for first, second in pairs:
        if first in durations and second in durations and \
                durations[first] != durations[second]:
            out.append(_f("R094", "defect", "motion",
                          "elements that move together share easing and "
                          "duration",
                          f"{first} {durations[first]:.0f}ms vs {second} "
                          f"{durations[second]:.0f}ms",
                          f"paired durations for {first}/{second}"))
            return


# --- Phase 3: DOM tree, CSS length helpers, measured checks -----------------
#
# The flat HtmlDoc in common.py records elements in document order but no
# containment, so Gestalt checks that need a shared ancestor (R064) or
# first-in-header (R037) build a small parent-pointer tree here instead.

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
              "link", "meta", "param", "source", "track", "wbr"}


@dataclass(eq=False)
class _DomNode:
    tag: str
    attrs: dict[str, str]
    line: int
    parent: _DomNode | None = None
    children: list[_DomNode] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    index: int = 0  # document order across the whole file

    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def text(self) -> str:
        parts = list(self.text_parts)
        for child in self.children:
            parts.append(child.text())
        return " ".join(p for p in parts if p).strip()


class _DomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _DomNode("document", {}, 0)
        self._stack = [self.root]
        self._count = 0

    def _push(self, tag: str, attrs: list[tuple[str, str | None]],
              push: bool = True) -> None:
        line, _ = self.getpos()
        node = _DomNode(tag, {k: v or "" for k, v in attrs}, line,
                        parent=self._stack[-1])
        node.index = self._count
        self._count += 1
        self._stack[-1].children.append(node)
        if push and tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs)

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        self._push(tag, attrs, push=False)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text_parts.append(data.strip())


def _parse_dom(text: str) -> _DomNode:
    """Parse markup into a parent-pointer tree (tolerant of bad nesting)."""
    parser = _DomParser()
    try:
        parser.feed(text)
    except (ValueError, TypeError):
        pass
    return parser.root


def _walk(node: _DomNode) -> Any:
    yield node
    for child in node.children:
        yield from _walk(child)


def _is_descendant(node: _DomNode, ancestor: _DomNode) -> bool:
    current = node.parent
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _is_first_in(node: _DomNode, ancestor: _DomNode) -> bool:
    """True when node is the first element child at every level up to ancestor."""
    current = node
    while current is not None and current is not ancestor:
        parent = current.parent
        if parent is None or not parent.children or parent.children[0] is not current:
            return False
        current = parent
    return current is ancestor


def _lca_of_two(a: _DomNode, b: _DomNode) -> _DomNode | None:
    a_ancestors = set()
    node: _DomNode | None = a
    while node is not None:
        a_ancestors.add(id(node))
        node = node.parent
    node = b
    while node is not None:
        if id(node) in a_ancestors:
            return node
        node = node.parent
    return None


def _lowest_common_ancestor(nodes: list[_DomNode]) -> _DomNode | None:
    if not nodes:
        return None
    common: _DomNode | None = nodes[0]
    for other in nodes[1:]:
        if common is None:
            return None
        common = _lca_of_two(common, other)
    return common


def _decls_for_element(project: Project, node: _DomNode) -> dict[str, str]:
    """Merged base declarations for one element: tag, then each class."""
    merged: dict[str, str] = {}
    merged.update(decls_for(project.css_rules, node.tag))
    for cls in node.classes():
        merged.update(decls_for(project.css_rules, f".{cls}"))
        merged.update(decls_for(project.css_rules, f"{node.tag}.{cls}"))
    return merged


def _shorthand_side(value: str, side: str) -> str | None:
    """One side of a 1-4 value margin/padding shorthand."""
    parts = value.split()
    if not parts:
        return None
    if side == "left":
        index = {1: 0, 2: 1, 3: 1}.get(len(parts), 3)
    else:  # bottom
        index = {1: 0, 2: 0, 3: 2}.get(len(parts), 2)
    return parts[index]


def _side_px(decls: dict[str, str], longhand: str, shorthand: str,
             side: str) -> float | None:
    if longhand in decls:
        return px_value(decls[longhand].strip())
    if shorthand in decls:
        raw = _shorthand_side(decls[shorthand], side)
        return px_value(raw.strip()) if raw else None
    return None


def _left_offset(decls: dict[str, str]) -> float | None:
    """margin-left + padding-left in px, or None when neither is numeric."""
    total = 0.0
    found = False
    for longhand, shorthand in (("margin-left", "margin"),
                                ("padding-left", "padding")):
        px = _side_px(decls, longhand, shorthand, "left")
        if px is not None:
            total += px
            found = True
    return total if found else None


def _margin_bottom_px(decls: dict[str, str]) -> float | None:
    return _side_px(decls, "margin-bottom", "margin", "bottom")


def _fr_ratios(value: str) -> list[float]:
    """fr track sizes from grid-template-columns (repeat() expanded)."""
    match = re.search(r"repeat\(\s*(\d+)\s*,\s*([\d.]+)fr\s*\)", value)
    if match:
        return [float(match.group(2))] * int(match.group(1))
    return [float(m.group(1)) for m in re.finditer(r"([\d.]+)fr", value)]


def _aspect_ratio(value: str) -> float | None:
    match = re.search(r"([\d.]+)\s*/\s*([\d.]+)", value)
    if match and float(match.group(2)) != 0:
        return float(match.group(1)) / float(match.group(2))
    if re.fullmatch(r"[\d.]+", value.strip()):
        return float(value.strip())
    return None


_NEAR_RATIOS = (1.0, 1.5, 1.618)  # 1:1, 2:3, golden ratio


def _near_ratio(ratio: float) -> bool:
    return any(abs(ratio - target) <= 0.2 for target in _NEAR_RATIOS)


def _widest_layout_boxes(layout: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """(viewport, boxes) for the widest supplied viewport, or (None, [])."""
    if not layout:
        return None, []
    viewports = []
    for key in layout:
        try:
            viewports.append(int(key))
        except (TypeError, ValueError):
            continue
    if not viewports:
        return None, []
    widest = max(viewports)
    return str(widest), layout_boxes(layout, widest)


def _first_viewport_height(boxes: list[dict[str, Any]]) -> float | None:
    """First-viewport height proxy: the hero's bottom edge when a hero
    box is supplied (heroes commonly fill the first viewport), else the
    lowest box bottom. Only ever a proxy for thirds/bands, never a
    claimed viewport size."""
    hero = box_by_name(boxes, "hero")
    if hero is not None:
        return hero["y"] + hero["height"]
    if not boxes:
        return None
    return max(box["y"] + box["height"] for box in boxes)


def _check_golden_ratio(project: Project, out: list[Finding],
                        layout: Any) -> None:
    """R001: measure key proportions from CSS and, when supplied, layout boxes."""
    ratios: list[tuple[float, str, str]] = []
    proportion_found = False
    for rule in project.css_rules:
        decls = rule.decls()
        if "grid-template-columns" in decls:
            frs = _fr_ratios(decls["grid-template-columns"])
            if frs:
                proportion_found = True
                for low, high in pairwise(sorted(frs)):
                    if low > 0 and high > low:
                        ratios.append((high / low,
                                       f"grid columns {low:g}fr : {high:g}fr",
                                       f"{rule.source}:{rule.line}"))
        if "aspect-ratio" in decls:
            ratio = _aspect_ratio(decls["aspect-ratio"])
            if ratio is not None:
                proportion_found = True
                ratios.append((ratio, f"aspect-ratio {decls['aspect-ratio']}",
                               f"{rule.source}:{rule.line}"))
    hero_decls: dict[str, str] = {}
    for selector in (".hero", "header.hero", ".hero-section"):
        hero_decls.update(decls_for(project.css_rules, selector))
    width = px_value(hero_decls.get("width", ""))
    height = px_value(hero_decls.get("height", ""))
    if width and height:
        proportion_found = True
        ratios.append((max(width, height) / min(width, height),
                       f"hero {width:g}x{height:g}px",
                       "hero width/height in CSS"))
    if len(_heading_sizes(project)) >= 2:
        # Type-size steps are a deliberate proportion; R012 owns their value.
        proportion_found = True
    viewport, boxes = _widest_layout_boxes(layout)
    if boxes:
        hero = box_by_name(boxes, "hero")
        if hero is not None and hero["width"] > 0 and hero["height"] > 0:
            proportion_found = True
            ratio = max(hero["width"], hero["height"]) / min(
                hero["width"], hero["height"])
            ratios.append((ratio,
                           f"rendered hero {hero['width']:g}x{hero['height']:g}px",
                           f"layout box hero @{viewport}"))
        content = box_by_name(boxes, "content")
        sidebar = box_by_name(boxes, "sidebar")
        if content is not None and sidebar is not None \
                and content["width"] > 0 and sidebar["width"] > 0:
            proportion_found = True
            ratio = max(content["width"], sidebar["width"]) / min(
                content["width"], sidebar["width"])
            ratios.append((ratio,
                           f"rendered content {content['width']:g}px : "
                           f"sidebar {sidebar['width']:g}px",
                           f"layout boxes content/sidebar @{viewport}"))
    for ratio, description, evidence in ratios[:3]:
        if not _near_ratio(ratio):
            out.append(_f("R001", "change", "proportions",
                          "key proportions near 1.618 (golden ratio), 1:1 or 2:3",
                          f"{description} is ratio {ratio:.2f}",
                          evidence))
    if not proportion_found:
        out.append(_f("R001", "change", "proportions",
                      "a deliberate key proportion (golden-ratio grid, "
                      "aspect-ratio or hero dimensions)",
                      "no ratio-bearing declaration found in CSS or layout",
                      "no grid fr columns, aspect-ratio, hero dimensions "
                      "or type-size steps"))


def _margin_auto(decls: dict[str, str]) -> bool:
    if decls.get("margin-left") == "auto" \
            and decls.get("margin-right") == "auto":
        return True
    parts = decls.get("margin", "").split()
    if len(parts) == 1 and parts[0] == "auto":
        return True
    if len(parts) == 2 and parts[1] == "auto":
        return True
    if len(parts) == 3 and parts[1] == "auto":
        return True
    if len(parts) == 4 and parts[1] == "auto" and parts[3] == "auto":
        return True
    return False


def _focal_centred(project: Project, node: _DomNode,
                   container_decls: dict[str, str]) -> bool:
    """Dead-centre: text centred (or inherited from the section) for
    headings; margin auto or a centred container for image/button."""
    decls = _decls_for_element(project, node)
    text_centred = decls.get("text-align") == "center" \
        or container_decls.get("text-align") == "center"
    if node.tag in {"h1", "h2", "h3"}:
        return text_centred
    return _margin_auto(decls) or text_centred


def _check_rule_of_thirds(project: Project, out: list[Finding],
                          layout: Any) -> None:
    """R002: flag heroes/feature sections where every focal element is dead-centre."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        for section in _walk(root):
            classes = section.classes()
            is_hero = "hero" in classes or "feature" in classes or (
                section.tag == "section"
                and any(d.tag == "h1" for d in _walk(section)))
            if not is_hero:
                continue
            container_decls = _decls_for_element(project, section)
            focal = [d for d in _walk(section) if d is not section and (
                d.tag in {"h1", "h2", "img"}
                or (d.tag in {"a", "button"} and any(
                    c in {"btn", "btn-primary", "cta", "cta-primary"}
                    or c.endswith("-cta") for c in d.classes())))]
            if len(focal) >= 2 and all(
                    _focal_centred(project, el, container_decls)
                    for el in focal):
                out.append(_f(
                    "R002", "change", rel,
                    "focal points on third-lines/intersections, not all "
                    "dead-centre",
                    "every focal element in this hero/feature section sits "
                    "dead-centre: "
                    + ", ".join(f"<{el.tag}>" for el in focal),
                    f"hero/feature section at {rel}:{section.line}"))
                break
    viewport, boxes = _widest_layout_boxes(layout)
    if boxes:
        height = _first_viewport_height(boxes)
        width = float(viewport) if viewport else None
        focal = [b for b in boxes
                 if b["name"] in {"hero", "primary_cta", "content"}]
        if len(focal) >= 2 and height and width:
            def in_middle(box: dict[str, Any]) -> bool:
                cx, cy = box_centre(box)
                return width / 3 <= cx <= 2 * width / 3 \
                    and height / 3 <= cy <= 2 * height / 3

            if all(in_middle(box) for box in focal):
                out.append(_f(
                    "R002", "change", f"layout @{viewport}",
                    "focal points on third-lines, not all dead-centre",
                    "every focal box centre falls in the middle third of "
                    "both axes: "
                    + ", ".join(f"{b['name']} centre "
                                f"{box_centre(b)[0]:.0f},{box_centre(b)[1]:.0f}"
                                for b in focal),
                    f"layout boxes @{viewport}"))


def _check_shared_axes(project: Project, out: list[Finding],
                       layout: Any) -> None:
    """R005: flag left offsets under ~8px that are not on the 4/8 grid."""
    for rule in project.css_rules:
        offset = _left_offset(rule.decls())
        if offset is not None and 0 < offset < 8 and offset % 4 != 0:
            out.append(_f("R005", "defect", f"{rule.source}:{rule.line}",
                          "shared axes: left offsets on the 4/8 grid",
                          f"left offset {offset:g}px is not on the 4px grid",
                          f"margin-left/padding-left {offset:g}px in "
                          f"{rule.source}:{rule.line}"))
    viewport, boxes = _widest_layout_boxes(layout)
    if boxes:
        edges = sorted({0.0} | {box["x"] for box in boxes})
        for box in boxes:
            for edge in edges:
                dx = abs(box["x"] - edge)
                if 0 < dx < 8 and dx % 4 != 0:
                    other = next((b for b in boxes if b["x"] == edge), None)
                    label = "the viewport edge" if other is None \
                        else f"{other['name']} (x={edge:g}px)"
                    out.append(_f(
                        "R005", "defect", f"layout @{viewport}",
                        "shared axes: x-edges on the 4/8 grid",
                        f"{box['name']} left edge x={box['x']:g}px sits "
                        f"{dx:g}px from {label}",
                        f"layout boxes @{viewport}: {box['name']} "
                        f"x={box['x']:g}px, {label} x={edge:g}px"))
                    break


def _find_logo(root: _DomNode) -> _DomNode | None:
    for node in _walk(root):
        if node is root:
            continue
        if any("logo" in token for token in node.classes()):
            return node
        if node.tag == "img" and "logo" in node.attrs.get("alt", "").lower():
            return node
    return None


def _primary_cta_nodes(root: _DomNode) -> list[_DomNode]:
    return [n for n in _walk(root)
            if n.tag in {"a", "button"} and any(
                c in {"btn-primary", "cta-primary", "primary-cta"}
                for c in n.classes())]


def _check_z_pattern(project: Project, out: list[Finding],
                     layout: Any) -> None:
    """R037: logo top-left, primary CTA near the end of the flow."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        logo = _find_logo(root)
        if logo is None:
            continue
        headers = [n for n in _walk(root) if n.tag == "header"]
        if headers:
            header = headers[0]
            if _is_descendant(logo, header) and not _is_first_in(logo, header):
                out.append(_f("R037", "defect", f"{rel}:{logo.line}",
                              "logo top-left: first element in the header",
                              "logo is not the first element in the header",
                              f"logo at {rel}:{logo.line}"))
            elif not _is_descendant(logo, header) \
                    and logo.index > header.index:
                out.append(_f("R037", "defect", f"{rel}:{logo.line}",
                              "logo top-left, before the page header",
                              "logo appears after the header instead of "
                              "top-left",
                              f"logo at {rel}:{logo.line}"))
        for cta in _primary_cta_nodes(root):
            if cta.index < logo.index:
                out.append(_f("R037", "defect", f"{rel}:{cta.line}",
                              "primary CTA near the end of the Z, after the logo",
                              "primary CTA appears before the logo "
                              "(inverted Z)",
                              f"primary CTA at {rel}:{cta.line}, logo at "
                              f"{rel}:{logo.line}"))
                break
    viewport, boxes = _widest_layout_boxes(layout)
    if boxes:
        width = float(viewport) if viewport else None
        height = _first_viewport_height(boxes)
        logo = box_by_name(boxes, "logo")
        if logo is not None and width and height:
            if not (logo["x"] <= 0.25 * width
                    and logo["y"] <= 0.15 * height):
                out.append(_f("R037", "defect", f"layout @{viewport}",
                              "logo box top-left in the first viewport",
                              f"logo box at x={logo['x']:g}px, "
                              f"y={logo['y']:g}px is not top-left",
                              f"layout box logo @{viewport}"))
        cta = box_by_name(boxes, "primary_cta")
        if cta is not None and width and height:
            cx, cy = box_centre(cta)
            if not (cx >= 2 * width / 3 and cy >= 2 * height / 3):
                out.append(_f("R037", "defect", f"layout @{viewport}",
                              "primary CTA box near bottom-right of the "
                              "first viewport",
                              f"primary CTA box centre "
                              f"{cx:.0f},{cy:.0f} is not bottom-right",
                              f"layout box primary_cta @{viewport}"))


def _colour_roles(counts: Counter) -> dict[str, tuple[int, int, int]]:
    """Palette roles from CSS declaration counts: dominant is most-used,
    accent is least-used non-neutral, secondary is most-used of the rest."""
    if not counts:
        return {}
    dominant = counts.most_common(1)[0][0]
    non_neutral = [(rgb, n) for rgb, n in counts.items()
                   if not is_neutral(rgb)]
    accent = min(non_neutral, key=lambda kv: kv[1])[0] \
        if non_neutral else None
    rest = [(rgb, n) for rgb, n in counts.items()
            if rgb != dominant and rgb != accent]
    secondary = max(rest, key=lambda kv: kv[1])[0] if rest else None
    roles = {"dominant": dominant}
    if secondary is not None:
        roles["secondary"] = secondary
    if accent is not None:
        roles["accent"] = accent
    return roles


def _check_r019(project: Project, out: list[Finding],
                measured: dict[str, Any], screenshots: Any,
                status: str) -> None:
    """R019 (60-30-10): rendered pixel shares when a screenshot can be
    read, otherwise the CSS declaration-count estimate, labelled as one."""
    counts = _palette(project)
    roles = _colour_roles(counts)
    if status == "present" and screenshots and "accent" in roles:
        result = measure_colour_shares(screenshots[0], roles)
        if result is not None:
            measured["r019_basis"] = "rendered"
            shares = result["shares"]
            measured["r019_shares"] = {role: round(share, 3)
                                       for role, share in shares.items()}
            measured["r019_screenshot"] = result["path"]
            accent_share = shares.get("accent", 0.0)
            if accent_share > 0.15:
                out.append(_f("R019", "change", "colour",
                              "accent ~10% of the rendered page",
                              f"accent covers {accent_share:.0%} of "
                              "rendered pixels",
                              f"pixel count of {result['path']}: accent "
                              f"{accent_share:.1%}, dominant "
                              f"{shares.get('dominant', 0.0):.1%}, secondary "
                              f"{shares.get('secondary', 0.0):.1%}"))
            dominant_share = shares.get("dominant", 0.0)
            if dominant_share > 0.5 and not is_neutral(roles["dominant"]):
                out.append(_f("R019", "change", "colour",
                              "dominant ~60% and usually neutral",
                              "saturated dominant colour covers "
                              f"{dominant_share:.0%} of rendered pixels",
                              f"pixel count of {result['path']}: dominant "
                              f"{dominant_share:.1%}"))
            return
    measured["r019_basis"] = "estimated"
    total = sum(counts.values())
    if total:
        top_n = counts.most_common(1)[0][1]
        share = top_n / total
        if share < 0.30:
            out.append(_f("R019", "change", "colour",
                          "roughly 60% dominant, 30% secondary, 10% accent "
                          "(estimated from declarations)",
                          f"most-used colour is only {share:.0%} of colour "
                          "declarations (no dominant colour) — estimate "
                          "from declarations, not a rendered measurement",
                          f"{top_n}/{total} declarations use the top colour"))


def _check_proximity(project: Project, out: list[Finding]) -> None:
    """R062: a label must sit closer to its own field than the field
    sits to the next label (label margin-bottom vs field margin-bottom)."""
    label_mb: float | None = None
    label_evidence = ""
    field_mb: float | None = None
    field_evidence = ""
    for rule in project.css_rules:
        if rule.media is not None:
            continue
        for selector in rule.selectors:
            last = selector.split(":")[0].strip().split()[-1] \
                if selector.split(":")[0].strip().split() else ""
            decls = rule.decls()
            if last == "label" and label_mb is None:
                label_mb = _margin_bottom_px(decls)
                if label_mb is not None:
                    label_evidence = f"{rule.source}:{rule.line}"
            if last in {"input", "textarea", "select"} \
                    and field_mb is None:
                field_mb = _margin_bottom_px(decls)
                if field_mb is not None:
                    field_evidence = f"{rule.source}:{rule.line}"
    if label_mb is not None and field_mb is not None \
            and label_mb > field_mb:
        out.append(_f("R062", "defect", "forms",
                      "a label sits closer to its own field than the "
                      "field sits to the next label",
                      f"label margin-bottom {label_mb:g}px exceeds field "
                      f"margin-bottom {field_mb:g}px",
                      f"label margin-bottom {label_mb:g}px in "
                      f"{label_evidence}; field margin-bottom "
                      f"{field_mb:g}px in {field_evidence}"))


def _link_style_key(decls: dict[str, str]) -> tuple | None:
    style = (decls.get("color"), decls.get("text-decoration"),
             decls.get("font-weight"))
    return style if any(v is not None for v in style) else None


def _check_similarity(project: Project, out: list[Finding]) -> None:
    """R063: same-function links share one style; primary buttons share one style."""
    link_styles: dict[str, tuple] = {}
    for rule in project.css_rules:
        if rule.media is not None:
            continue
        for selector in rule.selectors:
            if ":" in selector and ":link" not in selector:
                continue  # hover/focus/visited are states, not other links
            base = selector.replace(":link", "").strip()
            last = base.split()[-1] if base.split() else ""
            if last in {"a", ".link"}:
                key = _link_style_key(rule.decls())
                if key is not None:
                    link_styles.setdefault(selector, key)
    if len(set(link_styles.values())) > 1:
        out.append(_f("R063", "defect", "links",
                      "links sharing a function share one style",
                      f"{len(set(link_styles.values()))} different link "
                      "styles on the page",
                      "; ".join(f"{sel}: color {style[0]}, decoration "
                                f"{style[1]}, weight {style[2]}"
                                for sel, style in sorted(link_styles.items()))))
    html_classes: set[str] = set()
    for _, doc in project.html:
        for el in doc.elements:
            html_classes.update(el.attrs.get("class", "").split())
    backgrounds: dict[str, str] = {}
    for cls in ("btn-primary", "cta-primary", "primary-cta", "btn--primary"):
        if cls not in html_classes:
            continue
        decls = decls_for(project.css_rules, f".{cls}")
        bg = decls.get("background-color") or decls.get("background", "")
        if bg:
            backgrounds[cls] = bg
    if len(set(backgrounds.values())) > 1:
        out.append(_f("R063", "defect", "buttons",
                      "primary actions sharing a function share one style",
                      "primary buttons use different backgrounds: "
                      + ", ".join(f".{c} {b}"
                                  for c, b in sorted(backgrounds.items())),
                      "primary-button background declarations in project CSS"))


def _is_card_container(node: _DomNode, project: Project) -> bool:
    """A grouping boundary: article/section/aside, a card/plan/tier
    class, or a class the CSS gives a border/background."""
    if node.tag in {"article", "section", "aside"}:
        return True
    for cls in node.classes():
        token = cls.lower()
        if any(word in token for word in
               ("card", "plan", "tier", "pricing", "package")):
            return True
        decls = decls_for(project.css_rules, f".{cls}")
        if any(k in decls for k in ("border", "border-radius", "box-shadow",
                                    "background", "background-color")):
            return True
    return False


def _check_common_region(project: Project, out: list[Finding]) -> None:
    """R064: one plan's price + features + CTA share a containing card."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        ordered = [n for n in _walk(root) if n is not root]
        prices = [n for n in ordered
                  if any("price" in token for token in n.classes())]
        for price in prices:
            after = ordered[ordered.index(price) + 1:]
            features = next((n for n in after if n.tag in {"ul", "ol"}
                             and any("feature" in t for t in n.classes())),
                            None)
            cta = next((n for n in after if n.tag in {"a", "button"}
                        and any(t in {"btn-primary", "cta-primary",
                                     "primary-cta"} or t.endswith("-cta")
                                for t in n.classes())), None)
            if features is None or cta is None:
                continue
            ancestor = _lowest_common_ancestor([price, features, cta])
            if ancestor is None or not _is_card_container(ancestor, project):
                out.append(_f(
                    "R064", "defect", rel,
                    "one plan's price, features and CTA share a "
                    "containing card",
                    "price, features and CTA float free with no shared "
                    "card/section container",
                    f"price at {rel}:{price.line} shares only "
                    f"<{ancestor.tag if ancestor else '?'}> with its "
                    "features and CTA"))
                break


def _check_closure(project: Project, out: list[Finding]) -> None:
    """R065: a carousel must show a sliver of the next card (peek cue)."""
    containers: dict[str, dict[str, str]] = {}
    items: dict[str, dict[str, str]] = {}
    for rule in project.css_rules:
        if rule.media is not None:
            continue
        decls = rule.decls()
        for selector in rule.selectors:
            classes = re.findall(r"\.([\w-]+)", selector)
            if not any("carousel" in c or "slider" in c for c in classes):
                continue
            if any(c in {"carousel-item", "carousel-card", "slide"}
                   or c.endswith("-slide") for c in classes):
                items.setdefault(selector, decls)
            else:
                containers.setdefault(selector, decls)
    for selector, decls in containers.items():
        overflow = decls.get("overflow", "") or decls.get("overflow-x", "")
        if "hidden" not in overflow and "clip" not in overflow:
            continue
        for item_selector, item_decls in items.items():
            width = item_decls.get("width", "")
            flex = item_decls.get("flex", "")
            if width.strip() != "100%" and "100%" not in flex:
                continue
            peek = "scroll-snap-type" in decls \
                or "scroll-snap-type" in item_decls \
                or "peek" in selector or "peek" in item_selector
            if peek:
                continue
            out.append(_f("R065", "defect", selector,
                          "a carousel shows a sliver of the next card",
                          "carousel items are exactly 100% wide with "
                          "overflow hidden and no peek cue",
                          f"{selector} overflow: {overflow}; "
                          f"{item_selector} width: {width or flex}"))
            return


def _check_continuity(project: Project, out: list[Finding]) -> None:
    """R066: step flows follow one aligned path (flex row, grid column
    or a shared left offset)."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        ordered = [n for n in _walk(root) if n is not root]
        steps = [n for n in ordered if any(
            t == "step" or t.startswith("step-") for t in n.classes())]
        if len(steps) < 2:
            continue
        containers = [n for n in ordered if any(
            t in {"steps", "stepper", "step-flow"} for t in n.classes())]
        aligned = any(
            _decls_for_element(project, container).get("display")
            in {"flex", "grid", "inline-flex"}
            for container in containers)
        if aligned:
            continue
        offsets: set[float] = set()
        for step in steps:
            offset = _left_offset(_decls_for_element(project, step))
            if offset is not None:
                offsets.add(offset)
        if len(offsets) > 1:
            out.append(_f("R066", "defect", rel,
                          "step flows follow one aligned path (flex row, "
                          "grid column or shared left offset)",
                          f"step elements sit at {len(offsets)} different "
                          "left offsets: "
                          + ", ".join(f"{o:g}px" for o in sorted(offsets)),
                          f"step offsets in {rel}"))
            break


_COLOUR_TOKEN_RE = re.compile(
    r"rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8}|\b[a-z]+\b")
_NON_COLOUR_WORDS = {"url", "none", "repeat", "no-repeat", "cover",
                     "contain", "center", "scroll", "fixed", "solid"}


def _first_colour_token(value: str) -> str | None:
    for match in _COLOUR_TOKEN_RE.finditer(value):
        token = match.group(0)
        if token.lower() not in _NON_COLOUR_WORDS:
            return token
    return None


def _rgba_alpha(value: str) -> float | None:
    text = value.strip().lower()
    match = re.fullmatch(
        r"rgba?\(\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*[\d.]+%?\s*,\s*([\d.]+)"
        r"\s*\)", text)
    if match:
        return float(match.group(1))
    match = re.fullmatch(
        r"hsla?\(\s*[\d.]+\s*,\s*[\d.]+%\s*,\s*[\d.]+%\s*,\s*([\d.]+)"
        r"\s*\)", text)
    if match:
        return float(match.group(1))
    match = re.fullmatch(r"#(?:[0-9a-f]{6})([0-9a-f]{2})", text)
    if match:
        return int(match.group(1), 16) / 255
    if text == "transparent":
        return 0.0
    return None


def _is_dimmed(value: str) -> bool:
    """A translucent dark colour: alpha >= 0.3 over a dark base."""
    token = _first_colour_token(value)
    if token is None:
        return False
    rgb = parse_color(token)
    if rgb is None:
        return False
    alpha = _rgba_alpha(token)
    if alpha is None:
        alpha = 1.0
    return alpha >= 0.3 and relative_luminance(rgb) < 0.3


def _check_figure_ground(project: Project, out: list[Finding]) -> None:
    """R067: modals sit over a dimmed page; CTAs separate from their hero."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        ordered = [n for n in _walk(root) if n is not root]
        modals = [n for n in ordered if any(
            "modal" in t and "overlay" not in t and "backdrop" not in t
            for t in n.classes())]
        if not modals:
            continue
        overlays = [n for n in ordered if any(
            "overlay" in t or "backdrop" in t for t in n.classes())]
        dimmed = any(
            _is_dimmed(_decls_for_element(project, overlay).get(
                "background-color") or _decls_for_element(
                    project, overlay).get("background", ""))
            for overlay in overlays)
        if not dimmed:
            for rule in project.css_rules:
                if not any("::backdrop" in s for s in rule.selectors):
                    continue
                bg = rule.decls().get("background-color") \
                    or rule.decls().get("background", "")
                if bg and _is_dimmed(bg):
                    dimmed = True
        if not dimmed:
            modal = modals[0]
            out.append(_f("R067", "defect", f"{rel}:{modal.line}",
                          "a modal sits over a dimmed page (translucent "
                          "dark overlay, alpha >= 0.3)",
                          "modal present with no dimmed overlay/backdrop "
                          "element",
                          f"<{modal.tag}> class="
                          f"\"{' '.join(modal.classes())}\" at "
                          f"{rel}:{modal.line}"))
    hero_decls: dict[str, str] = {}
    for selector in (".hero", "header.hero", ".hero-section"):
        hero_decls.update(decls_for(project.css_rules, selector))
    cta_decls = decls_for(project.css_rules, ".btn-primary")
    hero_token = _first_colour_token(
        hero_decls.get("background-color")
        or hero_decls.get("background", "")) or ""
    cta_token = _first_colour_token(
        cta_decls.get("background-color")
        or cta_decls.get("background", "")) or ""
    hero_rgb = parse_color(hero_token) if hero_token else None
    cta_rgb = parse_color(cta_token) if cta_token else None
    if hero_rgb is not None and cta_rgb is not None:
        ratio = contrast_ratio(hero_rgb, cta_rgb)
        if ratio < 3:
            out.append(_f("R067", "defect", ".hero",
                          "the primary CTA stands apart from its hero "
                          "background (contrast >= 3:1)",
                          f"CTA background contrasts {ratio:.1f}:1 with "
                          "the hero background",
                          f"hero background {hero_token}; .btn-primary "
                          f"background {cta_token}"))


_TIMING_RE = re.compile(
    r"\b(ease|ease-in|ease-out|ease-in-out|linear)\b"
    r"|cubic-bezier\([^)]*\)|steps\([^)]*\)")


def _easing_of(decls: dict[str, str]) -> str | None:
    for prop in ("transition-timing-function", "animation-timing-function"):
        if prop in decls:
            match = _TIMING_RE.search(decls[prop])
            return match.group(0) if match else decls[prop].strip()
    for prop in ("transition", "animation"):
        if prop in decls:
            match = _TIMING_RE.search(decls[prop])
            if match:
                return match.group(0)
    return None


def _duration_of(decls: dict[str, str]) -> float | None:
    for prop in ("transition-duration", "animation-duration"):
        if prop in decls:
            return _duration_ms(decls[prop])
    for prop in ("transition", "animation"):
        if prop in decls:
            return _duration_ms(decls[prop])
    return None


def _motion_name(token: str) -> str | None:
    """Map a class token to a paired-motion member (R068/R094 pairs)."""
    if token in {"modal", "overlay", "tooltip", "arrow", "drawer",
                 "backdrop"}:
        return token
    for suffix, name in (("-overlay", "overlay"), ("-backdrop", "backdrop"),
                         ("-arrow", "arrow"), ("-modal", "modal"),
                         ("-tooltip", "tooltip"), ("-drawer", "drawer")):
        if token.endswith(suffix):
            return name
    return None


def _paired_motion(project: Project) -> dict[str, tuple[float | None, str | None]]:
    """(duration ms, easing) per paired-motion member, matched on whole
    class tokens so .modal-overlay counts as the overlay, not the modal."""
    motion: dict[str, tuple[float | None, str | None]] = {}
    for rule in project.css_rules:
        if rule.media is not None:
            continue
        for selector in rule.selectors:
            for token in re.findall(r"\.([\w-]+)", selector):
                name = _motion_name(token)
                if name is None or name in motion:
                    continue
                decls = rule.decls()
                duration = _duration_of(decls)
                easing = _easing_of(decls)
                if duration is not None or easing is not None:
                    motion[name] = (duration, easing)
    return motion


def _check_common_fate(project: Project, out: list[Finding]) -> None:
    """R068: paired animations (modal+overlay, tooltip+arrow,
    drawer+backdrop) share easing AND duration."""
    pairs = (("modal", "overlay"), ("tooltip", "arrow"), ("drawer", "backdrop"))
    motion = _paired_motion(project)
    for first, second in pairs:
        if first not in motion or second not in motion:
            continue
        d_first, e_first = motion[first]
        d_second, e_second = motion[second]
        problems = []
        if d_first is not None and d_second is not None \
                and d_first != d_second:
            problems.append(f"duration {d_first:.0f}ms vs {d_second:.0f}ms")
        if (e_first or "ease") != (e_second or "ease"):
            problems.append(f"easing {e_first or 'ease'} vs "
                            f"{e_second or 'ease'}")
        if problems:
            out.append(_f("R068", "defect", "motion",
                          "elements that move together share easing and "
                          "duration",
                          f"{first} and {second} move apart: "
                          + "; ".join(problems),
                          f"paired motion for {first}/{second}"))
            return


def _check_pragnanz(project: Project, out: list[Finding]) -> None:
    """R069: icon-only buttons need an accessible name; icon sets share
    one stroke-width."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        for node in _walk(root):
            if node.tag != "button":
                continue
            labelled = bool(node.text()) \
                or bool(node.attrs.get("aria-label", "").strip()) \
                or bool(node.attrs.get("aria-labelledby", "").strip()) \
                or bool(node.attrs.get("title", "").strip())
            if not labelled:
                out.append(_f("R069", "defect", f"{rel}:{node.line}",
                              "every button has an accessible name (visible "
                              "text or aria-label)",
                              "icon-only button with no accessible name",
                              f"<button> at {rel}:{node.line}"))
        strokes = {node.attrs["stroke-width"].strip()
                   for node in _walk(root)
                   if node.tag == "svg" and node.attrs.get("stroke-width")}
        if len(strokes) > 1:
            out.append(_f("R069", "defect", rel,
                          "icons in a set share one stroke-width/style",
                          f"icons use {len(strokes)} different "
                          "stroke-widths: " + ", ".join(sorted(strokes)),
                          f"stroke-width attributes in {rel}"))


def _check_symmetry(project: Project, out: list[Finding]) -> None:
    """R070 (merged R070/R071): steppers/tabs need a visual connector;
    footer/nav columns must not be wildly unequal for no focal purpose."""
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        root = _parse_dom(text)
        for node in _walk(root):
            if node is root or not any(
                    t in {"stepper", "steps", "tabs", "tab-list"}
                    for t in node.classes()):
                continue
            connected = any(
                any(word in t for word in
                    ("connector", "step-line", "tab-line", "divider"))
                for desc in _walk(node) for t in desc.classes())
            if not connected:
                for rule in project.css_rules:
                    if any("::before" in s or "::after" in s
                           for s in rule.selectors) and any(
                               tok in s for s in rule.selectors
                               for tok in (".step", ".stepper", ".tab")):
                        decls = rule.decls()
                        if any(k in decls for k in (
                                "border", "border-top", "border-bottom",
                                "background", "background-color", "content")):
                            connected = True
                container_decls = _decls_for_element(project, node)
                if any(k in container_decls
                       for k in ("border-top", "border-bottom")):
                    connected = True
            if not connected:
                out.append(_f("R070", "defect", f"{rel}:{node.line}",
                              "steppers/tabs have a visual connector "
                              "between steps",
                              "stepper with no connecting line between steps",
                              f"stepper at {rel}:{node.line}"))
                break
    for rule in project.css_rules:
        if rule.media is not None:
            continue
        for selector in rule.selectors:
            if selector.strip() not in {"footer", "nav", ".footer",
                                        ".site-footer", ".nav"}:
                continue
            decls = rule.decls()
            if "grid-template-columns" in decls:
                frs = _fr_ratios(decls["grid-template-columns"])
                if len(frs) >= 2 and min(frs) > 0 \
                        and max(frs) / min(frs) > 3:
                    out.append(_f("R070", "change",
                                  f"{rule.source}:{rule.line}",
                                  "footer/nav columns balanced unless one "
                                  "is deliberately focal",
                                  f"footer/nav columns {max(frs):g}fr : "
                                  f"{min(frs):g}fr are wildly unequal",
                                  "grid-template-columns: "
                                  f"{decls['grid-template-columns']} in "
                                  f"{rule.source}:{rule.line}"))
                    return


_STATE_CLASSES = {"is-visible", "in-view", "visible", "entered", "revealed",
                  "animate-in", "is-animating", "show", "shown", "active"}


def _check_animation_job(project: Project, out: list[Finding]) -> None:
    """R093: entrance animations on all cards on page load, with no
    trigger (hover/focus/active or a state class), are decorative."""
    for rule in project.css_rules:
        if rule.media is not None \
                and "prefers-reduced-motion" in rule.media:
            continue
        decls = rule.decls()
        value = decls.get("animation") or decls.get("animation-name", "")
        if not value or value.strip() == "none":
            continue
        for selector in rule.selectors:
            if any(pseudo in selector
                   for pseudo in (":hover", ":focus", ":active")):
                continue
            classes = re.findall(r"\.([\w-]+)", selector)
            if not any(c == "card" or c.endswith("-card") for c in classes):
                continue
            if any(c in _STATE_CLASSES for c in classes):
                continue
            out.append(_f("R093", "change", f"{rule.source}:{rule.line}",
                          "every animation has a job (orientation, "
                          "continuity or feedback)",
                          "entrance animation on all cards on page load "
                          "with no trigger",
                          f"{selector} animation: {value} in "
                          f"{rule.source}:{rule.line}"))
            return


def judge(project_dir: str | Path, *, tokens: Any = None,
          screenshots: Any = None, live_metrics: Any = None,
          brief: Any = None, content_draft: Any = None,
          layout: Any = None, axe_results: Any = None,
          baselines: Any = None) -> Verdict:
    """Judge one built site visually. See module docstring for the rules.

    ``layout`` is an optional dict ``{viewport_width_as_string:
    [{name, x, y, width, height}]}`` of real element boxes from the live
    browser (see ``rendered.layout_boxes``); without it the geometry
    rules measure static CSS instead. ``baselines`` is an optional
    directory of baseline screenshots for the visual diff
    (``measured["visual_diff"]``); ``axe_results`` is accepted (and
    ignored) so the coordinator can pass the same optional inputs to
    every judger through :func:`judge_all`.
    """
    project = load_project(project_dir)
    if project is None:
        return unjudged_verdict(JUDGER)
    findings: list[Finding] = []
    checked: list[str] = []
    measured: dict[str, Any] = {
        "pages": len(project.html),
        "css_rules": len(project.css_rules),
        "layout_basis": "layout" if layout is not None else "css",
    }
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
    checks = (
        ("R003", _check_grid), ("R004", _check_baseline),
        ("R007", _check_grid_vs_flex), ("R008", _check_subgrid),
        ("R009", _check_body_type), ("R010", _check_body_type),
        ("R011", _check_body_type), ("R012", _check_type_scale),
        ("R013", _check_families), ("R014", _check_heading_type),
        ("R015", _check_heading_type), ("R016", _check_heading_type),
        ("R017", _check_heading_type), ("R018", _check_heading_type),
        ("R019", _check_colour), ("R020", _check_colour),
        ("R021", _check_colour), ("R022", _check_colour),
        ("R023", _check_colour), ("R024", _check_colour),
        ("R025", None), ("R026", None), ("R027", _check_token_naming),
        ("R028", _check_spacing), ("R029", _check_spacing),
        ("R030", _check_spacing), ("R031", _check_spacing),
        ("R032", _check_spacing), ("R033", _check_hierarchy),
        ("R034", _check_hierarchy), ("R035", _check_hierarchy),
        ("R036", _check_hierarchy), ("R038", _check_hierarchy),
        ("R039", _check_hierarchy), ("R090", _check_motion),
        ("R091", _check_motion), ("R092", _check_motion),
        ("R094", _check_motion),
    )
    seen: set[str] = set()
    seen_fns: set[object] = set()
    for rule, fn in checks:
        if rule in seen:
            continue
        seen.add(rule)
        checked.append(rule)
        if fn is not None and fn not in seen_fns:
            seen_fns.add(fn)
            fn(project, findings)
    _check_dark_mode(project, tokens, findings)
    checked.extend(("R025", "R026"))
    # Phase 3: measured geometry, Gestalt and motion checks.
    _check_golden_ratio(project, findings, layout)
    _check_rule_of_thirds(project, findings, layout)
    _check_shared_axes(project, findings, layout)
    _check_z_pattern(project, findings, layout)
    _check_r019(project, findings, measured, screenshots, status)
    _check_proximity(project, findings)
    _check_similarity(project, findings)
    _check_common_region(project, findings)
    _check_closure(project, findings)
    _check_continuity(project, findings)
    _check_figure_ground(project, findings)
    _check_common_fate(project, findings)
    _check_pragnanz(project, findings)
    _check_symmetry(project, findings)
    _check_animation_job(project, findings)
    checked.extend(("R001", "R002", "R005", "R037", "R062", "R063", "R064",
                    "R065", "R066", "R067", "R068", "R069", "R070", "R093"))
    # Live checkers (registry slots) merge into the verdict via
    # checkers.apply_visual_checkers: rule-mapped violations become
    # findings, gate-only results land in measured, never invented.
    apply_visual_checkers(project, findings, measured, _f,
                          baselines=baselines, screenshots=screenshots)
    if tokens is not None:
        findings.extend(token_drift(project_dir, tokens))
    return make_verdict(
        JUDGER, findings, unchecked_for(JUDGER, extra=extra),
        measured=measured,
        checked=sorted(set(checked), key=lambda r: int(r[1:])),
    )
