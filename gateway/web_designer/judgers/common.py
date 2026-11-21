"""Shared helpers for the five web-designer judgers (stages 19-23).

Everything here is standard library only: ``html.parser`` for markup,
a small ``re``-based CSS parser (rules, declarations, media queries,
custom properties), real WCAG colour math, and file-size accounting.
No new dependencies — nothing here needs a browser or the network.

A judger's contract: ``judge(project_dir, ...) -> Verdict``. A verdict
with ``unjudged=True`` means the judger could not run at all (no project
files); a single failed rule is a failed verdict, never unjudged.
``unchecked`` always covers the judger's unimplemented rules plus any
rule that needs a measurement that was not supplied (live metrics,
screenshots). ``measured["checked_rules"]`` lists every rule the judger
actually evaluated, so coverage is observable from the verdict alone.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

from .. import rules as rules_mod
from ..types import Finding, Verdict

logger = logging.getLogger(__name__)

SOURCE_EXTS = {".html", ".css", ".js", ".ts", ".tsx", ".jsx"}
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv"}

# Rules that need rendered layout (a screenshot) to evaluate at all.
# Without screenshots they go in ``unchecked``; with screenshots the
# check is that the rendered evidence exists for the rule — pixel
# measurement needs a live browser (Phase 3), never an invented number.
# Visual R001/R005/R037 used to live here: they are now measured from
# CSS declarations and, when supplied, live layout boxes, so they are
# checked with or without screenshots. R096 (thumb zone) still needs
# the 375px render — its static check is only a fallback.
SCREENSHOT_DEPENDENT: dict[str, frozenset[str]] = {
    "visual": frozenset(),
    "responsive": frozenset({"R096"}),
}

_NAMED_COLOURS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "orange": (255, 165, 0), "purple": (128, 0, 128),
    "pink": (255, 192, 203), "brown": (165, 42, 42), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "teal": (0, 128, 128),
    "navy": (0, 0, 128), "olive": (128, 128, 0), "maroon": (128, 0, 0),
    "aqua": (0, 255, 255), "silver": (192, 192, 192), "lime": (0, 255, 0),
    "transparent": (255, 255, 255),
}


# --- CSS ----------------------------------------------------------------------

@dataclass
class CssDecl:
    property: str
    value: str
    line: int


@dataclass
class CssRule:
    selectors: list[str]
    declarations: list[CssDecl]
    media: str | None
    line: int
    source: str

    def decls(self) -> dict[str, str]:
        """Last declaration wins, as in the cascade within one rule."""
        out: dict[str, str] = {}
        for decl in self.declarations:
            out[decl.property] = decl.value
        return out


def _strip_css_comments(text: str) -> str:
    """Remove /* */ comments, keeping line numbers (newlines preserved)."""
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.DOTALL)


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text) - 1


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _parse_rule_block(
    text: str, media: str | None, source: str, out: list[CssRule],
) -> None:
    pos = 0
    while True:
        match = re.compile(r"([^{}]+)\{").search(text, pos)
        if not match:
            return
        selector_text = match.group(1).strip()
        brace = match.start() + len(match.group(0)) - 1
        close = _matching_brace(text, brace)
        body = text[brace + 1:close]
        line = _line_of(text, match.start())
        if selector_text.startswith("@"):
            _parse_rule_block(body, selector_text, source, out)
        else:
            selectors = [s.strip() for s in selector_text.split(",") if s.strip()]
            decls = []
            for part in body.split(";"):
                if ":" not in part:
                    continue
                prop, _, value = part.partition(":")
                decls.append(CssDecl(prop.strip().lower(), value.strip(),
                                     line))
            if selectors:
                out.append(CssRule(selectors, decls, media, line, source))
        pos = close + 1


def parse_css(text: str, source: str = "") -> list[CssRule]:
    """Parse a stylesheet into rules with media context and line numbers."""
    out: list[CssRule] = []
    _parse_rule_block(_strip_css_comments(text), None, source, out)
    return out


def media_queries(rules: list[CssRule]) -> list[str]:
    return sorted({r.media for r in rules if r.media})


def decls_for(rules: list[CssRule], selector: str) -> dict[str, str]:
    """Merged declarations for rules matching a selector exactly.

    Base (non-media) rules only: a media-query rule describes one width,
    not the base style, so it must not override the base value in a
    static check. Media rules are read directly where they matter
    (breakpoints, dark mode, reduced motion).
    """
    merged: dict[str, str] = {}
    for rule in rules:
        if rule.media is None and selector in rule.selectors:
            merged.update(rule.decls())
    return merged


def values_for_property(rules: list[CssRule], prop: str) -> list[tuple[str, CssRule]]:
    out = []
    for rule in rules:
        for decl in rule.declarations:
            if decl.property == prop:
                out.append((decl.value, rule))
    return out


def custom_properties(rules: list[CssRule]) -> dict[str, str]:
    props: dict[str, str] = {}
    for rule in rules:
        for decl in rule.declarations:
            if decl.property.startswith("--"):
                props[decl.property] = decl.value
    return props


# --- HTML ---------------------------------------------------------------------

@dataclass
class HtmlElement:
    tag: str
    attrs: dict[str, str]
    line: int


@dataclass
class HtmlDoc:
    elements: list[HtmlElement] = field(default_factory=list)
    texts: list[tuple[str, int]] = field(default_factory=list)  # (text, line)
    ends: list[tuple[str, int]] = field(default_factory=list)  # (tag, line)

    def find(self, tag: str) -> list[HtmlElement]:
        return [e for e in self.elements if e.tag == tag]

    def text(self) -> str:
        return " ".join(t for t, _ in self.texts)


class _HtmlCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = HtmlDoc()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line, _ = self.getpos()
        self.doc.elements.append(HtmlElement(
            tag, {k: v or "" for k, v in attrs}, line))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        line, _ = self.getpos()
        self.doc.ends.append((tag, line))

    def handle_data(self, data: str) -> None:
        if data.strip():
            line, _ = self.getpos()
            self.doc.texts.append((data.strip(), line))


def parse_html(text: str) -> HtmlDoc:
    """Parse markup into elements (tag/attrs/line) and text runs."""
    collector = _HtmlCollector()
    try:
        collector.feed(text)
    except (ValueError, TypeError):
        logger.warning("web-designer: HTML parse failed for a document",
                       exc_info=True)
    return collector.doc


# --- Colour math (WCAG 2.2) ---------------------------------------------------

def parse_color(value: str) -> tuple[int, int, int] | None:
    """Parse #rgb/#rrggbb/#rrggbbaa, rgb()/rgba(), hsl()/hsla(), names."""
    text = value.strip().lower()
    if text in _NAMED_COLOURS:
        return _NAMED_COLOURS[text]
    match = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", text)
    if match:
        digits = match.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(
        r"rgba?\(\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*,\s*([\d.]+%?)"
        r"(?:\s*,\s*[\d.]+)?\s*\)", text)
    if match:
        channels = []
        for part in match.groups():
            num = float(part.rstrip("%"))
            channels.append(round(num * 255 / 100) if part.endswith("%") else round(num))
        return (channels[0], channels[1], channels[2])
    match = re.fullmatch(
        r"hsla?\(\s*([\d.]+)\s*,\s*([\d.]+)%\s*,\s*([\d.]+)%"
        r"(?:\s*,\s*[\d.]+)?\s*\)", text)
    if match:
        return _hsl_to_rgb(float(match.group(1)), float(match.group(2)) / 100,
                           float(match.group(3)) / 100)
    return None


def _hsl_to_rgb(h: float, s: float, li: float) -> tuple[int, int, int]:
    h = (h % 360) / 360
    if s == 0:
        grey = round(li * 255)
        return (grey, grey, grey)
    q = li * (1 + s) if li < 0.5 else li + s - li * s
    p = 2 * li - q

    def channel(t: float) -> int:
        t %= 1.0
        if t < 1 / 6:
            c = p + (q - p) * 6 * t
        elif t < 1 / 2:
            c = q
        elif t < 2 / 3:
            c = p + (q - p) * (2 / 3 - t) * 6
        else:
            c = p
        return round(c * 255)

    return (channel(h + 1 / 3), channel(h), channel(h - 1 / 3))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    """WCAG contrast ratio: 1:1 (identical) to 21:1 (black on white)."""
    lighter = max(relative_luminance(fg), relative_luminance(bg))
    darker = min(relative_luminance(fg), relative_luminance(bg))
    return (lighter + 0.05) / (darker + 0.05)


def hue_of(rgb: tuple[int, int, int]) -> float:
    """Hue in degrees (0-360); grey colours report -1 (no hue)."""
    r, g, b = (c / 255 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return -1.0
    delta = mx - mn
    if mx == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif mx == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)
    return hue % 360


def is_neutral(rgb: tuple[int, int, int]) -> bool:
    """True for greys: max-min channel spread below 12% of full scale."""
    return max(rgb) - min(rgb) < 32


# --- Lengths ------------------------------------------------------------------

def px_value(value: str) -> float | None:
    """``"16px"`` -> 16.0; anything else (em, rem, %, var()) -> None."""
    match = re.fullmatch(r"([\d.]+)px", value.strip())
    return float(match.group(1)) if match else None


def px_or_relative(value: str, base_px: float = 16.0) -> float | None:
    """Resolve a length to px: px, em/rem (against base), unitless number."""
    text = value.strip()
    px = px_value(text)
    if px is not None:
        return px
    match = re.fullmatch(r"([\d.]+)(em|rem)", text)
    if match:
        return float(match.group(1)) * base_px
    if re.fullmatch(r"[\d.]+", text):
        return float(text) * base_px
    return None


# --- Project loading ----------------------------------------------------------

@dataclass
class Project:
    root: Path
    files: dict[str, str]  # relative posix path -> text
    html: list[tuple[str, HtmlDoc]]
    css_rules: list[CssRule]
    js_text: str

    @property
    def css_text(self) -> str:
        return "\n".join(
            text for rel, text in self.files.items() if rel.endswith(".css"))

    @property
    def all_text(self) -> str:
        return "\n".join(self.files.values())


def load_project(project_dir: str | Path) -> Project | None:
    """Load a built site's source files, or None when there is nothing to judge."""
    root = Path(project_dir)
    if not root.is_dir():
        return None
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_EXTS:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            files[str(path.relative_to(root))] = path.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not files:
        return None
    html = [(rel, parse_html(text)) for rel, text in files.items()
            if rel.endswith(".html")]
    css_rules: list[CssRule] = []
    for rel, text in files.items():
        if rel.endswith(".css"):
            css_rules.extend(parse_css(text, source=rel))
    js_text = "\n".join(text for rel, text in files.items()
                        if rel.endswith((".js", ".ts", ".tsx", ".jsx")))
    return Project(root=root, files=files, html=html, css_rules=css_rules,
                   js_text=js_text)


_SIZE_BUCKETS: dict[str, str] = {
    ".js": "js_bytes", ".ts": "js_bytes", ".tsx": "js_bytes",
    ".jsx": "js_bytes", ".mjs": "js_bytes", ".css": "css_bytes",
    ".png": "image_bytes", ".jpg": "image_bytes", ".jpeg": "image_bytes",
    ".webp": "image_bytes", ".avif": "image_bytes", ".gif": "image_bytes",
    ".svg": "image_bytes", ".html": "html_bytes", ".htm": "html_bytes",
}


def file_sizes(project_dir: str | Path) -> dict[str, int]:
    """Byte totals by kind: js, css, images, html, total."""
    root = Path(project_dir)
    totals = {"js_bytes": 0, "css_bytes": 0, "image_bytes": 0,
              "html_bytes": 0, "total_bytes": 0}
    if not root.is_dir():
        return totals
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        totals["total_bytes"] += size
        bucket = _SIZE_BUCKETS.get(path.suffix.lower())
        if bucket is not None:
            totals[bucket] += size
    return totals


# --- Verdict assembly ---------------------------------------------------------

def _rule_sort_key(rule_id: str) -> int:
    try:
        return int(rule_id[1:])
    except (ValueError, IndexError):
        return 9999


def unchecked_for(judger: str, *, live_measured: bool = False,
                  extra: frozenset[str] | set[str] = frozenset(),
                  remove: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Unchecked rule ids: unimplemented + live-only without a live
    measurement, plus conditional extras, minus rules actually measured."""
    base = set(rules_mod.unchecked_rule_ids(judger, live_measured=live_measured))
    base |= set(extra)
    base -= set(remove)
    return sorted(base, key=_rule_sort_key)


def screenshot_status(
    screenshots: Any,
) -> tuple[str, list[str]]:
    """('missing'|'present'|'broken', missing paths).

    ``screenshots`` is a list of paths to rendered screenshots (1440/768/
    375). Missing means the judger must leave screenshot-dependent rules
    unchecked; broken means paths were supplied but unreadable.
    """
    if not screenshots:
        return "missing", []
    paths = [str(p) for p in screenshots]
    missing = [p for p in paths if not Path(p).is_file()
               or Path(p).stat().st_size == 0]
    return ("broken", missing) if missing else ("present", [])


def make_finding(rule_id: str, judger: str, severity: str, location: str,
                 expected: str, actual: str, evidence: str) -> Finding:
    return Finding(rule_id=rule_id, judger=judger, severity=severity,
                   location=location, expected=expected, actual=actual,
                   evidence=evidence)


def make_verdict(judger: str, findings: list[Finding], unchecked: list[str],
                 *, measured: dict[str, Any] | None = None,
                 checked: list[str] | None = None,
                 unjudged: bool = False) -> Verdict:
    """Assemble a verdict. ``passed`` is False when any open blocker or
    defect stands; change/observation findings are logged, not blocking.
    ``checked`` records the rules actually evaluated."""
    data = dict(measured or {})
    if checked is not None:
        data["checked_rules"] = sorted(checked, key=_rule_sort_key)
    passed = not any(f.severity in {"blocker", "defect"} and f.status == "open"
                     for f in findings)
    return Verdict(judger=judger, passed=passed, findings=findings,
                   unchecked=unchecked, measured=data, unjudged=unjudged)


def unjudged_verdict(judger: str) -> Verdict:
    """The judger could not run at all (no project files / no preview)."""
    return Verdict(
        judger=judger, passed=False, findings=[], unjudged=True,
        unchecked=unchecked_for(judger),
        measured={"checked_rules": [], "reason": "no project files to judge"},
    )


def implemented_ids(judger: str) -> list[str]:
    return rules_mod.implemented_rule_ids(judger)
