"""Live checkers for the web-designer tool registry (Phase 3).

Stdlib + PIL only, no network, no new dependencies. Every checker is a
pure function returning a plain dict::

    {"available": bool, "reason": str,
     "violations": [{"rule_id": str | None, "severity": str, "location": str,
                     "expected": str, "actual": str, "evidence": str}],
     "measured": {...}}

``rule_id`` is set ONLY where the audit maps the check to a numbered rule;
otherwise it is None — a gate result recorded in ``measured``, never forced
onto a rule. The judgers consume these dicts: ``apply_visual_checkers``
wires the visual slots into the visual judger, and the content judger
calls ``check_links`` directly. Intended call sites:

- ``check_html_parse`` -> visual judger, recorded in
  ``measured["html_parse_errors"]``. Local HTML structure parse only; the
  W3C Markup Validation Service itself is never called.
- ``check_css_validity`` -> visual judger, recorded in
  ``measured["css_validation_errors"]``. Local CSS validity only; the W3C
  CSS Validation Service itself is never called.
- ``check_links`` -> content judger (R107), recorded alongside
  ``measured["external_links_unchecked"]``. This is the shared link
  implementation; ``content._check_links`` calls it.
- ``check_font_sources`` -> visual judger (R013).
- ``check_icon_consistency`` -> visual judger (R069).
- ``diff_screenshots`` -> visual judger, recorded in
  ``measured["visual_diff"]``. A LOCAL PIL pixel comparison of baseline vs
  current screenshots — the BackstopJS binary is NOT run and must never be
  implied to run.
- ``run_lighthouse`` -> performance judger, recorded in
  ``measured["live_metrics"]``. Runs the ``lighthouse`` CLI only when
  ``shutil.which`` finds it; otherwise ``available`` is False.
- ``run_axe`` -> a11y judger, recorded in ``measured["axe_results"]``.
  Runs the ``axe`` CLI only when ``shutil.which`` finds it; otherwise
  ``available`` is False.
- ``contrast`` (registry checker name for the WebAIM Contrast Checker
  slot) is NOT a function here: it is the existing
  ``judgers.common.contrast_ratio`` WCAG math, used directly by the a11y
  and visual judgers. No contrast math is reimplemented in this module.

``run_lighthouse`` / ``run_axe`` return a different shape — ``{"available",
"reason", "live_metrics" | "axe_results"}`` — because there are no local
violations to report when the binary is absent, and numbers or violations
must never be invented.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .common import Project, contrast_ratio  # re-exported: the "contrast" checker

__all__ = [
    "apply_visual_checkers", "check_css_validity", "check_font_sources",
    "check_html_parse", "check_icon_consistency", "check_links",
    "contrast_ratio", "diff_screenshots", "run_axe", "run_lighthouse",
]

_ICON_SIZES = (16, 20, 24, 32)
_ALLOWED_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com",
                       "fontshare")

# Standard CSS properties (CSS 2.1 + CSS3 modules in common use). Custom
# properties (--*) and vendor-prefixed properties (-webkit-*, -moz-*, ...)
# are always allowed; anything else must be in this set.
_KNOWN_PROPERTIES: frozenset[str] = frozenset({
    "accent-color", "align-content", "align-items", "align-self",
    "all", "animation", "animation-delay", "animation-direction",
    "animation-duration", "animation-fill-mode", "animation-iteration-count",
    "animation-name", "animation-play-state", "animation-timing-function",
    "appearance", "aspect-ratio", "backdrop-filter", "backface-visibility",
    "background", "background-attachment", "background-blend-mode",
    "background-clip", "background-color", "background-image",
    "background-origin", "background-position", "background-position-x",
    "background-position-y", "background-repeat", "background-size",
    "block-size", "border", "border-block", "border-block-color",
    "border-block-end", "border-block-end-color", "border-block-end-style",
    "border-block-end-width", "border-block-start", "border-block-start-color",
    "border-block-start-style", "border-block-start-width", "border-bottom",
    "border-bottom-color", "border-bottom-left-radius",
    "border-bottom-right-radius", "border-bottom-style", "border-bottom-width",
    "border-collapse", "border-color", "border-end-end-radius",
    "border-end-start-radius", "border-image", "border-image-outset",
    "border-image-repeat", "border-image-slice", "border-image-source",
    "border-image-width", "border-inline", "border-inline-color",
    "border-inline-end", "border-inline-end-color", "border-inline-end-style",
    "border-inline-end-width", "border-inline-start",
    "border-inline-start-color", "border-inline-start-style",
    "border-inline-start-width", "border-left", "border-left-color",
    "border-left-style", "border-left-width", "border-radius", "border-right",
    "border-right-color", "border-right-style", "border-right-width",
    "border-spacing", "border-start-end-radius", "border-start-start-radius",
    "border-style", "border-top", "border-top-color", "border-top-left-radius",
    "border-top-right-radius", "border-top-style", "border-top-width",
    "border-width", "bottom", "box-decoration-break", "box-shadow",
    "box-sizing", "break-after", "break-before", "break-inside", "caption-side",
    "caret-color", "clear", "clip", "clip-path", "color", "color-scheme",
    "column-count", "column-fill", "column-gap", "column-rule",
    "column-rule-color", "column-rule-style", "column-rule-width",
    "column-span", "column-width", "columns", "contain", "contain-intrinsic-size",
    "contain-intrinsic-block-size", "contain-intrinsic-height",
    "contain-intrinsic-inline-size", "contain-intrinsic-width", "content",
    "content-visibility", "counter-increment", "counter-reset",
    "counter-set", "cursor", "direction", "display", "empty-cells",
    "field-sizing", "filter", "flex", "flex-basis", "flex-direction",
    "flex-flow", "flex-grow", "flex-shrink", "flex-wrap", "float", "font",
    "font-family", "font-feature-settings", "font-kerning", "font-language-override",
    "font-optical-sizing", "font-palette", "font-size", "font-size-adjust",
    "font-stretch", "font-style", "font-synthesis", "font-variant",
    "font-variant-alternates", "font-variant-caps", "font-variant-east-asian",
    "font-variant-emoji", "font-variant-ligatures", "font-variant-numeric",
    "font-variant-position", "font-variation-settings", "font-weight",
    "forced-color-adjust", "gap", "grid", "grid-area", "grid-auto-columns",
    "grid-auto-flow", "grid-auto-rows", "grid-column", "grid-column-end",
    "grid-column-start", "grid-row", "grid-row-end", "grid-row-start",
    "grid-template", "grid-template-areas", "grid-template-columns",
    "grid-template-rows", "hanging-punctuation", "height", "hyphenate-character",
    "hyphenate-limit-chars", "hyphens", "image-rendering", "inline-size",
    "inset", "inset-block", "inset-block-end", "inset-block-start",
    "inset-inline", "inset-inline-end", "inset-inline-start", "isolation",
    "justify-content", "justify-items", "justify-self", "left", "letter-spacing",
    "line-break", "line-height", "line-height-step", "list-style",
    "list-style-image", "list-style-position", "list-style-type", "margin",
    "margin-block", "margin-block-end", "margin-block-start", "margin-bottom",
    "margin-inline", "margin-inline-end", "margin-inline-start", "margin-left",
    "margin-right", "margin-top", "mask", "mask-clip", "mask-composite",
    "mask-image", "mask-mode", "mask-origin", "mask-position", "mask-repeat",
    "mask-size", "mask-type", "max-block-size", "max-height", "max-inline-size",
    "max-width", "min-block-size", "min-height", "min-inline-size", "min-width",
    "mix-blend-mode", "object-fit", "object-position", "offset", "offset-anchor",
    "offset-distance", "offset-path", "offset-position", "offset-rotate",
    "opacity", "order", "orphans", "outline", "outline-color", "outline-offset",
    "outline-style", "outline-width", "overflow", "overflow-anchor",
    "overflow-block", "overflow-clip-margin", "overflow-inline", "overflow-wrap",
    "overflow-x", "overflow-y", "overscroll-behavior", "overscroll-behavior-block",
    "overscroll-behavior-inline", "overscroll-behavior-x", "overscroll-behavior-y",
    "padding", "padding-block", "padding-block-end", "padding-block-start",
    "padding-bottom", "padding-inline", "padding-inline-end",
    "padding-inline-start", "padding-left", "padding-right", "padding-top",
    "page-break-after", "page-break-before", "page-break-inside", "paint-order",
    "perspective", "perspective-origin", "place-content", "place-items",
    "place-self", "pointer-events", "position", "print-color-adjust",
    "quotes", "resize", "right", "rotate", "row-gap", "ruby-align",
    "ruby-position", "scale", "scroll-behavior", "scroll-margin",
    "scroll-margin-block", "scroll-margin-block-end", "scroll-margin-block-start",
    "scroll-margin-bottom", "scroll-margin-inline", "scroll-margin-inline-end",
    "scroll-margin-inline-start", "scroll-margin-left", "scroll-margin-right",
    "scroll-margin-top", "scroll-padding", "scroll-padding-block",
    "scroll-padding-block-end", "scroll-padding-block-start",
    "scroll-padding-bottom", "scroll-padding-inline", "scroll-padding-inline-end",
    "scroll-padding-inline-start", "scroll-padding-left", "scroll-padding-right",
    "scroll-padding-top", "scroll-snap-align", "scroll-snap-stop",
    "scroll-snap-type", "scrollbar-color", "scrollbar-gutter", "scrollbar-width",
    "shape-image-threshold", "shape-margin", "shape-outside", "tab-size",
    "table-layout", "text-align", "text-align-last", "text-combine-upright",
    "text-decoration", "text-decoration-color", "text-decoration-line",
    "text-decoration-skip-ink", "text-decoration-style",
    "text-decoration-thickness", "text-emphasis", "text-emphasis-color",
    "text-emphasis-position", "text-emphasis-style", "text-indent",
    "text-justify", "text-orientation", "text-overflow", "text-rendering",
    "text-shadow", "text-transform", "text-underline-offset",
    "text-underline-position", "text-wrap", "text-wrap-mode", "top",
    "touch-action", "transform", "transform-box", "transform-origin",
    "transform-style", "transition", "transition-delay", "transition-duration",
    "transition-property", "transition-timing-function", "translate",
    "unicode-bidi", "user-select", "vertical-align", "view-transition-name",
    "visibility", "white-space", "white-space-collapse", "widows", "width",
    "will-change", "word-break", "word-spacing", "word-wrap", "writing-mode",
    "z-index", "zoom",
})

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


def _violation(rule_id: str | None, severity: str, location: str,
               expected: str, actual: str, evidence: str) -> dict[str, Any]:
    return {"rule_id": rule_id, "severity": severity, "location": location,
            "expected": expected, "actual": actual, "evidence": evidence}


def _result(violations: list[dict[str, Any]],
            measured: dict[str, Any]) -> dict[str, Any]:
    return {"available": True, "reason": "", "violations": violations,
            "measured": measured}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "violations": [],
            "measured": {}}


# --- HTML structure parse (W3C Markup Validation Service slot) -----------

class _TagStackParser(HTMLParser):
    """Stack-based tag matching on top of html.parser (which is lenient)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.errors: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID_TAGS:
            line, _ = self.getpos()
            self.stack.append((tag, line))

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        line, _ = self.getpos()
        if tag in _VOID_TAGS:
            return
        if not self.stack:
            self.errors.append((f"closing </{tag}> with no open element",
                                line))
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        open_tags = [t for t, _ in self.stack]
        if tag in open_tags:
            while self.stack and self.stack[-1][0] != tag:
                unclosed, opened_line = self.stack.pop()
                self.errors.append(
                    (f"<{unclosed}> opened at line {opened_line} never "
                     f"closed before </{tag}>", line))
            self.stack.pop()
        else:
            self.errors.append((f"closing </{tag}> with no matching open "
                                f"element", line))


def check_html_parse(project: Project) -> dict[str, Any]:
    """Local HTML structure parse: mismatched / unclosed tags.

    The W3C service itself is never called; this is the local parse the
    registry's ``html_parse`` slot stands for.
    """
    violations: list[dict[str, Any]] = []
    error_count = 0
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        parser = _TagStackParser()
        try:
            parser.feed(text)
        except (ValueError, TypeError):
            violations.append(_violation(
                None, "defect", rel, "HTML parses without errors",
                "HTML parser raised an error on this document",
                f"parse error in {rel}"))
            error_count += 1
            continue
        for tag, line in parser.stack:
            violations.append(_violation(
                None, "defect", f"{rel}:{line}",
                "every opened element is closed",
                f"<{tag}> opened at line {line} is never closed",
                f"<{tag}> at {rel}:{line}"))
        for message, line in parser.errors:
            violations.append(_violation(
                None, "defect", f"{rel}:{line}",
                "every opened element is closed",
                message, f"{message} at {rel}:{line}"))
        error_count += len(parser.stack) + len(parser.errors)
    return _result(violations, {"html_parse_errors": error_count})


# --- CSS validity (W3C CSS Validation Service slot) ------------------------

def _css_brace_errors(text: str) -> list[str]:
    depth = 0
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for char in line:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    errors.append(f"unmatched closing brace at line {lineno}")
                    depth = 0
    if depth > 0:
        errors.append(f"{depth} unclosed brace(s) at end of stylesheet")
    return errors


def check_css_validity(project: Project) -> dict[str, Any]:
    """Local CSS validity: unknown properties, malformed declarations,
    unbalanced braces.

    The W3C service itself is never called; this is the local check the
    registry's ``css_validity`` slot stands for. Custom properties
    (``--*``) and vendor-prefixed properties are always allowed.
    """
    violations: list[dict[str, Any]] = []
    unknown: list[str] = []
    errors = 0
    for rule in project.css_rules:
        for decl in rule.declarations:
            prop = decl.property
            if prop.startswith("--") or prop.startswith("-"):
                continue
            if prop not in _KNOWN_PROPERTIES:
                unknown.append(prop)
                violations.append(_violation(
                    None, "defect", f"{rule.source}:{decl.line}",
                    "only standard CSS properties are used",
                    f"unknown CSS property {prop!r}",
                    f"{prop}: {decl.value} in {rule.source}:{decl.line}"))
                errors += 1
    for rel, text in project.files.items():
        if not rel.endswith(".css"):
            continue
        stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        for message in _css_brace_errors(stripped):
            violations.append(_violation(
                None, "defect", rel, "braces are balanced",
                message, message))
            errors += 1
        # Malformed declarations: a ;-separated segment inside a rule body
        # with no colon and no selector/at-rule shape.
        for match in re.finditer(r"\{([^{}]*)\}", stripped):
            body = match.group(1)
            line = stripped.count("\n", 0, match.start()) + 1
            for part in body.split(";"):
                part = part.strip()
                if not part or ":" in part or part.startswith("@"):
                    continue
                if re.fullmatch(r"[a-zA-Z-]+", part):
                    violations.append(_violation(
                        None, "defect", f"{rel}:{line}",
                        "every declaration has a property and a value",
                        f"malformed declaration {part!r} (no value)",
                        f"{part!r} in {rel}:{line}"))
                    errors += 1
    return _result(violations, {
        "css_errors": errors,
        "unknown_properties": sorted(set(unknown)),
    })


# --- Links (Dead Link Checker slot) ------------------------------------------

def _resolve_inside(project: Project, href: str) -> bool:
    """Same resolution logic as content._check_links: the target must be a
    file inside the project root."""
    target = (project.root / href.split("#")[0].split("?")[0]).resolve()
    try:
        inside = target.is_relative_to(project.root.resolve())
    except ValueError:
        inside = False
    return inside and target.is_file()


def check_links(project: Project) -> dict[str, Any]:
    """Internal links/assets must resolve inside the project (R107).

    External links are recorded in ``measured["external_links_unchecked"]``
    and never claimed checked — no network is used.
    """
    violations: list[dict[str, Any]] = []
    external: list[str] = []
    for rel, doc in project.html:
        for el in doc.find("a"):
            href = el.attrs.get("href", "")
            if not href or href.startswith(("#", "mailto:", "tel:")):
                continue
            if href.startswith(("http://", "https://", "//")):
                external.append(href)
                continue
            if not _resolve_inside(project, href):
                violations.append(_violation(
                    "R107", "defect", f"{rel}:{el.line}",
                    "no broken links in a delivered build",
                    f"relative link {href!r} does not resolve inside the "
                    "project",
                    f"<a href={href!r}> at {rel}:{el.line}"))
        for el in doc.find("img"):
            src = el.attrs.get("src", "")
            if not src or src.startswith(("http://", "https://", "data:")):
                continue
            if not _resolve_inside(project, src):
                violations.append(_violation(
                    "R107", "defect", f"{rel}:{el.line}",
                    "no missing images in a delivered build",
                    f"image {src!r} does not resolve inside the project",
                    f"<img src={src!r}> at {rel}:{el.line}"))
    return _result(violations, {
        "external_links_unchecked": sorted(set(external)),
        "broken_links": len(violations),
    })


# --- Font sources (Google Fonts / Fontshare slots) ---------------------------

_FONT_URL_RE = re.compile(
    r"@font-face\s*\{[^}]*?url\(\s*['\"]?([^'\"\)]+)['\"]?\s*\)"
    r"|@import\s+(?:url\(\s*['\"]?([^'\"\)]+)['\"]?\s*\)"
    r"|['\"]([^'\"]+)['\"])", re.DOTALL | re.IGNORECASE)


def _font_url_allowed(url: str) -> bool:
    lowered = url.lower()
    if lowered.startswith(("data:", "blob:")):
        return True
    if "://" not in lowered and not lowered.startswith("//"):
        # Local file: only font files count as a font source.
        return lowered.split("?")[0].split("#")[0].endswith((".woff2", ".woff"))
    host = lowered.split("://", 1)[-1].lstrip("/").split("/")[0].split(":")[0]
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in _ALLOWED_FONT_HOSTS)


def check_font_sources(project: Project) -> dict[str, Any]:
    """@font-face src / @import URLs must come from allowed open sources
    or local WOFF2/WOFF files (R013).

    Allowed: fonts.googleapis.com, fonts.gstatic.com, Fontshare, local
    .woff2/.woff files. Any other source is flagged with the URL as
    evidence.
    """
    violations: list[dict[str, Any]] = []
    sources: list[str] = []
    for rel, text in project.files.items():
        if not rel.endswith(".css"):
            continue
        for match in _FONT_URL_RE.finditer(text):
            url = next((g for g in match.groups() if g), None)
            if url is None:
                continue
            sources.append(url)
            if not _font_url_allowed(url):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(_violation(
                    "R013", "defect", f"{rel}:{line}",
                    "fonts come from allowed open sources (Google Fonts, "
                    "Fontshare) or local WOFF2/WOFF files",
                    f"font loaded from unlicensed/unknown source {url!r}",
                    f"@font-face/@import URL {url!r} in {rel}:{line}"))
    return _result(violations, {"font_sources": sorted(set(sources))})


# --- Icon consistency (visual judger, R069) ----------------------------------

_SVG_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.DOTALL | re.IGNORECASE)
_BUTTON_RE = re.compile(r"<button\b[^>]*>.*?</button>",
                        re.DOTALL | re.IGNORECASE)
_ATTR_RE = re.compile(r"([a-zA-Z-]+)\s*=\s*['\"]([^'\"]*)['\"]")


def _attrs(tag_text: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR_RE.findall(tag_text)}


def check_icon_consistency(project: Project) -> dict[str, Any]:
    """Inline SVGs share one stroke-width, use the 16/20/24/32px scale,
    and icon-only buttons carry an accessible name (R069)."""
    violations: list[dict[str, Any]] = []
    stroke_widths: list[str] = []
    icon_count = 0
    for rel, text in project.files.items():
        if not rel.endswith(".html"):
            continue
        for match in _SVG_RE.finditer(text):
            icon_count += 1
            svg = match.group(0)
            line = text.count("\n", 0, match.start()) + 1
            stroke_widths.extend(re.findall(
                r'stroke-width\s*=\s*["\']([\d.]+)["\']', svg))
            open_tag = svg[:svg.find(">") + 1]
            attrs = _attrs(open_tag)
            for dim in ("width", "height"):
                raw = attrs.get(dim, "")
                num = re.fullmatch(r"([\d.]+)(px)?", raw.strip())
                if num and float(num.group(1)) not in _ICON_SIZES:
                    violations.append(_violation(
                        "R069", "defect", f"{rel}:{line}",
                        "icons use the 16/20/24/32px size scale",
                        f"icon {dim} is {raw} (outside the 16/20/24/32px "
                        "scale)",
                        f"<svg {dim}={raw!r}> at {rel}:{line}"))
        for match in _BUTTON_RE.finditer(text):
            button = match.group(0)
            if "<svg" not in button.lower():
                continue
            open_tag = button[:button.find(">") + 1]
            attrs = _attrs(open_tag)
            inner_text = re.sub(r"<[^>]+>", "", button).strip()
            if not inner_text and not attrs.get("aria-label") \
                    and not attrs.get("aria-labelledby") \
                    and not attrs.get("title"):
                line = text.count("\n", 0, match.start()) + 1
                violations.append(_violation(
                    "R069", "defect", f"{rel}:{line}",
                    "icon-only buttons have an accessible name",
                    "icon-only button has no accessible name",
                    f"<button> with inline SVG and no aria-label at "
                    f"{rel}:{line}"))
    distinct_widths = sorted(set(stroke_widths), key=float)
    if len(distinct_widths) > 1:
        violations.append(_violation(
            "R069", "defect", "icons",
            "one stroke-width across the icon set",
            f"{len(distinct_widths)} different stroke-widths in use: "
            f"{', '.join(distinct_widths)}",
            f"stroke-width values: {', '.join(distinct_widths)}"))
    return _result(violations, {"icon_count": icon_count})


# --- Visual diff (BackstopJS slot: LOCAL PIL equivalent) ----------------------

def _screenshot_for(directory: Path, width: int) -> Path | None:
    for candidate in sorted(directory.glob(f"*{width}*.png")):
        if candidate.is_file():
            return candidate
    return None


def diff_screenshots(baseline_dir: str | Path, current_dir: str | Path,
                     viewports: tuple[int, ...] = (1440, 768, 375)
                     ) -> dict[str, Any]:
    """PIL pixel comparison of baseline vs current screenshots per viewport.

    This is a LOCAL visual diff, NOT BackstopJS: the BackstopJS binary is
    never run. A missing baseline or current file for a viewport makes the
    whole result unavailable (never a zero mismatch). ``measured`` holds
    ``{"visual_diff": {"<width>": {"mismatch_pct": float}}}``.
    """
    try:
        from PIL import Image
        from PIL.ImageChops import difference
    except ImportError:
        return _unavailable("PIL is not installed; local visual diff "
                            "cannot run")
    baseline = Path(baseline_dir)
    current = Path(current_dir)
    diffs: dict[str, dict[str, float]] = {}
    for width in viewports:
        base_file = _screenshot_for(baseline, width)
        curr_file = _screenshot_for(current, width)
        if base_file is None or curr_file is None:
            missing = "baseline" if base_file is None else "current"
            return _unavailable(
                f"no {missing} screenshot for viewport {width}px in "
                f"{baseline_dir if base_file is None else current_dir}")
        try:
            with Image.open(base_file) as base_img, \
                    Image.open(curr_file) as curr_img:
                base_rgb = base_img.convert("RGB")
                curr_rgb = curr_img.convert("RGB")
                if base_rgb.size != curr_rgb.size:
                    curr_rgb = curr_rgb.resize(base_rgb.size)
                diff = difference(base_rgb, curr_rgb)
                pixels = list(diff.getdata())
                total = sum(sum(px) for px in pixels)
                mismatch = total / (len(pixels) * 3 * 255) * 100 if pixels \
                    else 0.0
        except (OSError, ValueError) as exc:
            return _unavailable(f"unreadable screenshot for viewport "
                                f"{width}px: {exc}")
        diffs[str(width)] = {"mismatch_pct": round(mismatch, 4)}
    return _result([], {"visual_diff": diffs})


# --- Lighthouse / axe (binary slots: never invent numbers) --------------------

def run_lighthouse(url_or_path: str | Path) -> dict[str, Any]:
    """Run the ``lighthouse`` CLI when present; parse LCP/INP/CLS.

    When the binary is absent returns ``available`` False. Numbers come
    only from the CLI's JSON output — never invented.
    """
    binary = shutil.which("lighthouse")
    if binary is None:
        return {"available": False,
                "reason": "lighthouse binary not found on PATH",
                "live_metrics": None}
    try:
        proc = subprocess.run(
            [binary, str(url_or_path), "--output=json",
             "--output-path=stdout", "--quiet",
             "--chrome-flags=--headless --no-sandbox"],
            capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"lighthouse run failed: {exc}",
                "live_metrics": None}
    if proc.returncode != 0:
        return {"available": False,
                "reason": f"lighthouse exited {proc.returncode}: "
                          f"{proc.stderr.strip()[:200]}",
                "live_metrics": None}
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": False,
                "reason": "lighthouse output was not valid JSON",
                "live_metrics": None}
    audits = report.get("audits", {})
    lcp = audits.get("largest-contentful-paint", {}).get("numericValue")
    inp = audits.get("interaction-to-next-paint", {}).get("numericValue")
    cls = audits.get("cumulative-layout-shift", {}).get("numericValue")
    if lcp is None and inp is None and cls is None:
        return {"available": False,
                "reason": "lighthouse report has no Core Web Vitals audits",
                "live_metrics": None}
    return {"available": True, "reason": "",
            "live_metrics": {
                "lcp": (lcp / 1000.0 if isinstance(lcp, (int, float))
                        else None),
                "inp": (inp / 1000.0 if isinstance(inp, (int, float))
                        else None),
                "cls": cls if isinstance(cls, (int, float)) else None,
            }}


def run_axe(url_or_path: str | Path) -> dict[str, Any]:
    """Run the ``axe`` CLI when present; return violations in the shape
    ``{"violations": [{"rule", "impact", "description", "nodes"}]}``.

    When the binary is absent returns ``available`` False. Violations come
    only from the CLI's JSON output — never invented.
    """
    binary = shutil.which("axe")
    if binary is None:
        return {"available": False,
                "reason": "axe binary not found on PATH",
                "axe_results": None}
    try:
        proc = subprocess.run(
            [binary, str(url_or_path), "--stdout"],
            capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": f"axe run failed: {exc}",
                "axe_results": None}
    if proc.returncode != 0:
        return {"available": False,
                "reason": f"axe exited {proc.returncode}: "
                          f"{proc.stderr.strip()[:200]}",
                "axe_results": None}
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": False,
                "reason": "axe output was not valid JSON",
                "axe_results": None}
    violations = []
    for item in report.get("violations", []):
        nodes = []
        for node in item.get("nodes", []):
            target = node.get("target", [])
            nodes.append(", ".join(str(t) for t in target))
        violations.append({
            "rule": item.get("id", ""),
            "impact": item.get("impact") or "unknown",
            "description": item.get("description", ""),
            "nodes": nodes,
        })
    return {"available": True, "reason": "", "axe_results": {"violations": violations}}


# --- Visual wiring -----------------------------------------------------------


def _screenshots_dir(screenshots: Any) -> str | None:
    """Common directory of the screenshot paths, or None.

    The visual diff compares baseline and current screenshots per
    viewport, so it needs one directory holding the current shots.
    """
    paths = [Path(str(p)) for p in screenshots] if screenshots else []
    if not paths:
        return None
    parents = {p.parent for p in paths}
    return str(parents.pop()) if len(parents) == 1 else None


def apply_visual_checkers(project: Project, findings: list[Any],
                          measured: dict[str, Any], make_finding: Any,
                          baselines: Any = None,
                          screenshots: Any = None) -> None:
    """Merge the live checker slots into a visual verdict.

    ``make_finding`` is the visual judger's ``_f`` (rule, severity,
    location, expected, actual, evidence). Rule-mapped violations
    become findings; gate-only results land in ``measured``, never
    invented. The visual judger's own DOM check already flags an
    unlabelled icon button, so a checker copy of the same R069 button
    is skipped.
    """
    seen_r069 = {(f.rule_id, f.location) for f in findings
                 if f.rule_id == "R069"}
    for result in (check_font_sources(project),
                   check_icon_consistency(project)):
        for violation in result["violations"]:
            if (violation["rule_id"], violation["location"]) in seen_r069:
                continue
            findings.append(make_finding(
                violation["rule_id"], violation["severity"],
                violation["location"], violation["expected"],
                violation["actual"], violation["evidence"]))
        measured.update(result["measured"])
    css_result = check_css_validity(project)
    measured["css_validation_errors"] = [
        v["actual"] for v in css_result["violations"]]
    measured.update(css_result["measured"])
    if baselines is not None and screenshots:
        current_dir = _screenshots_dir(screenshots)
        if current_dir is not None:
            diff = diff_screenshots(baselines, current_dir)
            if diff["available"]:
                measured["visual_diff"] = diff["measured"]["visual_diff"]
