"""Accessibility judger (stage 20): WCAG 2.2 AA.

Real contrast math for every text/background pair found in CSS and
tokens (4.5:1 normal, 3:1 large text and non-text/focus), semantic HTML,
alt text, skip link, visible focus, keyboard operability, focus order,
200% resize resilience and flashing. An automated scan alone is never a
pass — these are re-checks on the built page itself (audit 6.20), and
any AA failure is a blocker. When the coordinator supplies axe results
(``axe_results``), its violations are mapped onto the same rules as
second-source evidence; without them, axe stays a reference/binary tool
and behaviour is unchanged.
"""
from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from .checkers import check_html_parse
from .common import (
    HtmlElement, Project, contrast_ratio, custom_properties, load_project,
    make_finding, make_verdict, parse_color, px_or_relative,
    unchecked_for, unjudged_verdict,
)
from ..tokens import flatten_token_leaves
from ..types import Finding, Verdict

JUDGER = "a11y"
_LARGE_PX = 24.0
_LARGE_BOLD_PX = 18.66

# axe -> audit-rule mapping for the ``axe_results`` wiring. Impact maps
# to severity: critical/serious -> blocker, moderate -> defect,
# minor -> change. An axe rule id with no audit rule here is skipped:
# it has no owning rule to report against.
_AXE_SEVERITY = {"critical": "blocker", "serious": "blocker",
                 "moderate": "defect", "minor": "change"}
_AXE_EXPECTED = {
    "R072": "normal text at least 4.5:1 contrast",
    "R073": "large text at least 3:1 contrast",
    "R075": "all functionality operable by keyboard alone",
    "R076": "visible focus indicator",
    "R077": "use the right element for the job",
    "R078": "every informative image needs descriptive alt text",
}


def _f(rule: str, severity: str, location: str, expected: str,
       actual: str, evidence: str) -> Finding:
    return make_finding(rule, JUDGER, severity, location, expected, actual,
                        evidence)


def _text_pairs(project: Project) -> list[tuple[str, str, str, float, bool, str, int]]:
    """(fg, bg, location, font_px, bold, selector, line) for text rules.

    A rule's background falls back to its own theme's body background:
    light rules to the light body background, dark-media rules to the
    dark body background. Mixing the two would invent failing pairs.
    """
    pairs = []
    props = custom_properties(project.css_rules)
    light_body = _body_defaults(project, dark=False)
    dark_body = _body_defaults(project, dark=True)
    for rule in project.css_rules:
        decls = rule.decls()
        if "color" not in decls:
            continue
        dark = bool(rule.media and "prefers-color-scheme: dark" in rule.media)
        default_bg = (dark_body if dark else light_body)[0]
        fg = decls["color"]
        bg = decls.get("background-color") or decls.get("background") or default_bg
        fg = props.get(fg[4:-1], fg) if fg.startswith("var(") else fg
        bg = props.get(bg[4:-1], bg) if bg.startswith("var(") else bg
        size = px_or_relative(decls.get("font-size", "")) or 16.0
        bold = decls.get("font-weight", "") in {"700", "800", "900", "bold"}
        for selector in rule.selectors:
            pairs.append((fg.split()[0], bg.split()[0], selector, size, bold,
                          ", ".join(rule.selectors), rule.line))
    if not pairs:
        fg, bg = light_body[1], light_body[0]
        pairs.append((fg.split()[0], bg.split()[0], "body",
                      16.0, False, "body", 0))
    return pairs


def _body_defaults(project: Project, *, dark: bool) -> tuple[str, str]:
    """(background, color) of the body rule for one theme."""
    bg, fg = "#ffffff", "#000000"
    for rule in project.css_rules:
        is_dark = bool(rule.media
                       and "prefers-color-scheme: dark" in rule.media)
        if is_dark != dark:
            continue
        if "body" not in rule.selectors:
            continue
        decls = rule.decls()
        bg = decls.get("background-color") or decls.get("background") or bg
        fg = decls.get("color") or fg
    return bg, fg


def _check_contrast(project: Project, tokens: Any, out: list[Finding]) -> None:
    checked_pairs: set[tuple[str, str]] = set()
    for fg, bg, _selector, size, bold, location, line in _text_pairs(project):
        fg_rgb = parse_color(fg)
        bg_rgb = parse_color(bg)
        if fg_rgb is None or bg_rgb is None or (fg, bg) in checked_pairs:
            continue
        checked_pairs.add((fg, bg))
        ratio = contrast_ratio(fg_rgb, bg_rgb)
        large = size >= _LARGE_PX or (bold and size >= _LARGE_BOLD_PX)
        needed = 3.0 if large else 4.5
        if ratio < needed:
            out.append(_f("R072" if not large else "R073", "blocker",
                          location,
                          f"{'large' if large else 'normal'} text at least "
                          f"{needed}:1 contrast",
                          f"{fg} on {bg} is {ratio:.2f}:1 at {size:g}px",
                          f"contrast {ratio:.2f}:1, {location}:{line}"))
        elif ratio < 7.0 and not large:
            out.append(_f("R073", "observation", location,
                          "AAA 7:1 for normal text",
                          f"{fg} on {bg} is {ratio:.2f}:1 (passes AA, below AAA)",
                          f"contrast {ratio:.2f}:1, {location}:{line}"))
    if tokens:
        for path, leaf in flatten_token_leaves(tokens).items():
            if "text" not in path or "color" not in path:
                continue
            value = leaf.get("value", "")
            if not isinstance(value, str) or not value.startswith("#"):
                continue
            fg_rgb = parse_color(value)
            bg_rgb = parse_color("#ffffff")
            if fg_rgb is None:
                continue
            ratio = contrast_ratio(fg_rgb, bg_rgb)
            if ratio < 4.5:
                out.append(_f("R072", "blocker", f"tokens:{path}",
                              "normal text at least 4.5:1 against the surface",
                              f"token {path} {value} is {ratio:.2f}:1 on white",
                              f"token {path} = {value}"))


def _tabindex_of(el: HtmlElement) -> int:
    try:
        return int(el.attrs.get("tabindex", "0") or 0)
    except ValueError:
        return 0


def _check_non_text_contrast(project: Project, out: list[Finding]) -> None:
    for rule in project.css_rules:
        decls = rule.decls()
        for prop in ("border-color", "outline-color"):
            if prop not in decls:
                continue
            fg_rgb = parse_color(decls[prop].split()[0])
            bg_rgb = parse_color(
                decls.get("background-color", "#ffffff").split()[0])
            if fg_rgb is None or bg_rgb is None:
                continue
            ratio = contrast_ratio(fg_rgb, bg_rgb)
            if ratio < 3.0:
                out.append(_f("R074", "blocker", f"{rule.source}:{rule.line}",
                              "UI boundaries and focus indicators at least "
                              "3:1 against adjacent colours",
                              f"{prop} {decls[prop]} is {ratio:.2f}:1",
                              f"{prop}: {decls[prop]} in {rule.source}:{rule.line}"))
                break


def _check_semantics(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        for el in doc.elements:
            if el.tag == "div" and "onclick" in el.attrs:
                out.append(_f("R077", "blocker", f"{rel}:{el.line}",
                              "button for actions, never a div as a button",
                              "div with onclick handler",
                              f"<div onclick> at {rel}:{el.line}"))
            if el.tag == "div" and el.attrs.get("role") == "button":
                out.append(_f("R077", "blocker", f"{rel}:{el.line}",
                              "button for actions, never a div as a button",
                              "div with role=button",
                              f"<div role=button> at {rel}:{el.line}"))
        landmarks = {e.tag for e in doc.elements}
        missing = {"header", "nav", "main", "footer"} - landmarks
        if missing and doc.find("body"):
            out.append(_f("R077", "change", rel,
                          "landmarks: header/nav/main/footer",
                          f"missing landmarks: {', '.join(sorted(missing))}",
                          f"landmarks present: {', '.join(sorted(landmarks & {'header', 'nav', 'main', 'footer'})) or 'none'}"))
        levels = [int(e.tag[1]) for e in doc.elements
                  if re.fullmatch(r"h[1-6]", e.tag)]
        if levels and levels[0] != 1:
            out.append(_f("R077", "defect", rel,
                          "heading order: page starts with h1",
                          f"first heading is h{levels[0]}",
                          f"first heading h{levels[0]} in {rel}"))
        for prev, cur in pairwise(levels):
            if cur > prev + 1:
                out.append(_f("R077", "defect", rel,
                              "heading order: never skip a level",
                              f"h{prev} followed by h{cur}",
                              f"heading sequence {levels} in {rel}"))
                break
        if sum(1 for e in doc.elements if e.tag == "h1") > 1:
            out.append(_f("R077", "defect", rel,
                          "one h1 per page",
                          f"{sum(1 for e in doc.elements if e.tag == 'h1')} h1 elements",
                          f"multiple <h1> in {rel}"))


def _check_alt_text(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        for el in doc.find("img"):
            src = el.attrs.get("src", "")
            alt = el.attrs.get("alt")
            decorative = el.attrs.get("role") in {"presentation", "none"} or \
                el.attrs.get("aria-hidden") == "true"
            if alt is None:
                out.append(_f("R078", "blocker", f"{rel}:{el.line}",
                              "every informative image needs descriptive alt "
                              "text; decorative images get alt=\"\"",
                              f"img {src!r} has no alt attribute",
                              f"<img src={src!r}> at {rel}:{el.line}"))
            elif alt == "" and not decorative:
                out.append(_f("R078", "blocker", f"{rel}:{el.line}",
                              "every informative image needs descriptive alt "
                              "text; decorative images get alt=\"\"",
                              f"informative img {src!r} has an empty alt",
                              f"<img src={src!r} alt=\"\"> at {rel}:{el.line}"))
            elif decorative and alt != "":
                out.append(_f("R078", "change", f"{rel}:{el.line}",
                              "decorative images get alt=\"\"",
                              f"decorative img has alt={alt!r}",
                              f"<img src={src!r}> at {rel}:{el.line}"))
            elif alt and alt.strip().lower() in {src.split("/")[-1].lower(),
                                                 "image", "photo", "picture"}:
                out.append(_f("R078", "defect", f"{rel}:{el.line}",
                              "alt text describes the image, never its filename",
                              f"alt={alt!r} for {src!r}",
                              f"<img src={src!r}> at {rel}:{el.line}"))
            elif alt and len(alt) > 125:
                out.append(_f("R078", "change", f"{rel}:{el.line}",
                              "alt text concise (<=125 chars)",
                              f"alt is {len(alt)} chars",
                              f"<img src={src!r}> at {rel}:{el.line}"))


def _check_skip_link(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        focusable = [e for e in doc.elements
                     if e.tag == "a" and e.attrs.get("href")
                     or e.tag in {"button", "input", "select", "textarea"}]
        if not focusable:
            continue
        first = focusable[0]
        is_skip = first.tag == "a" and (
            "skip" in first.attrs.get("class", "").lower()
            or "skip" in next((t for t, ln in doc.texts if ln == first.line), "").lower())
        if not is_skip:
            out.append(_f("R079", "defect", rel,
                          "skip-to-content link as the first focusable element",
                          f"first focusable element is <{first.tag}>",
                          f"first focusable <{first.tag}> at {rel}:{first.line}"))


def _check_focus(project: Project, out: list[Finding]) -> None:
    """A visible focus indicator must exist; outline:none is a blocker
    unless some focus rule replaces it with a visible indicator."""
    has_replacement = any(
        (":focus-visible" in " ".join(r.selectors)
         or ":focus" in " ".join(r.selectors))
        and any(d.property in {"outline", "border", "box-shadow"}
                and d.value not in {"none", "0", "0px"}
                for d in r.declarations)
        for r in project.css_rules)
    for rule in project.css_rules:
        decls = rule.decls()
        if decls.get("outline") in {"none", "0", "0px"} and not has_replacement:
            out.append(_f("R076", "blocker", f"{rule.source}:{rule.line}",
                          "visible focus indicator; never outline:none "
                          "without a replacement",
                          "outline:none with no :focus-visible replacement",
                          f"outline: {decls['outline']} in {rule.source}:{rule.line}"))
    if not any(":focus-visible" in " ".join(r.selectors) or ":focus" in " ".join(r.selectors)
               for r in project.css_rules):
        out.append(_f("R076", "change", "focus",
                      "visible focus indicator (:focus-visible outline)",
                      "no :focus or :focus-visible rule in CSS",
                      "no focus styles in project CSS"))


def _check_keyboard(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        for el in doc.elements:
            tabindex = el.attrs.get("tabindex")
            if tabindex is not None:
                try:
                    if int(tabindex) > 0:
                        out.append(_f("R075", "defect", f"{rel}:{el.line}",
                                      "no positive tabindex — DOM order is "
                                      "the tab order",
                                      f"<{el.tag}> tabindex={tabindex}",
                                      f"tabindex={tabindex} at {rel}:{el.line}"))
                except ValueError:
                    pass
        has_modal = any(
            e.attrs.get("role") == "dialog" or e.tag == "dialog"
            or "modal" in e.attrs.get("class", "") for e in doc.elements)
        if has_modal and "Escape" not in project.js_text and \
                "keydown" not in project.js_text:
            out.append(_f("R080", "defect", rel,
                          "Esc closes modals, menus and dialogs",
                          "modal/dialog present but no Escape/keydown handler "
                          "in JS",
                          "modal in HTML, no Escape handler in JS"))
        focusable = [e for e in doc.elements
                     if e.tag in {"a", "button", "input", "select", "textarea"}
                     and _tabindex_of(e) != -1]
        if focusable and any(_tabindex_of(e) > 0 for e in focusable):
            out.append(_f("R081", "defect", rel,
                          "focus order follows the visual reading order",
                          "positive tabindex reorders focus away from DOM order",
                          "positive tabindex in " + rel))


def _check_resize_and_flash(project: Project, out: list[Finding]) -> None:
    for rule in project.css_rules:
        decls = rule.decls()
        height = decls.get("height", "")
        overflow = decls.get("overflow", "")
        if height.endswith("px") and overflow in {"hidden", "clip"}:
            out.append(_f("R082", "defect", f"{rule.source}:{rule.line}",
                          "content survives 200% text resize without clipping",
                          f"fixed height {height} with overflow:{overflow} "
                          "clips enlarged text",
                          f"height: {height}; overflow: {overflow}"))
    for rule in project.css_rules:
        for decl in rule.declarations:
            if decl.property == "animation-duration":
                match = re.fullmatch(r"([\d.]+)(m?s)", decl.value.strip())
                if not match:
                    continue
                ms = float(match.group(1)) * (1 if match.group(2) == "ms" else 1000)
                iteration = rule.decls().get("animation-iteration-count", "")
                if ms < 334 and iteration == "infinite":
                    out.append(_f("R082", "defect", f"{rule.source}:{rule.line}",
                                  "nothing flashes more than 3 times per second",
                                  f"infinite animation at {ms:.0f}ms per cycle",
                                  f"animation-duration: {decl.value} infinite"))
                    return


def _axe_rule_id(violation: dict[str, Any]) -> str | None:
    """Map one axe violation to its audit rule, or None when no audit
    rule owns that axe rule id."""
    rule = str(violation.get("rule", ""))
    if rule == "color-contrast":
        return "R073" if _axe_is_large_text(violation) else "R072"
    if rule in {"link-name", "button-name", "input-button-name"}:
        return "R077"
    if rule == "image-alt":
        return "R078"
    if rule.startswith("aria-"):
        return "R077"
    if rule == "keyboard" or rule.startswith("keyboard-"):
        return "R075"
    if rule == "focus" or rule.startswith("focus-"):
        return "R076"
    if rule.startswith("landmark-"):
        return "R077"
    if rule == "heading-order":
        return "R077"
    return None


def _axe_is_large_text(violation: dict[str, Any]) -> bool:
    """True when a color-contrast violation carries a large-text hint:
    the string "large", or a size at/above 24px (18pt == 24px)."""
    size = violation.get("size", "")
    if isinstance(size, (int, float)):
        return size >= _LARGE_PX
    if not isinstance(size, str):
        return False
    if "large" in size.lower():
        return True
    match = re.fullmatch(r"([\d.]+)\s*(px|pt)?", size.strip().lower())
    if not match:
        return False
    value = float(match.group(1))
    if match.group(2) == "pt":
        value = value * 96 / 72
    return value >= _LARGE_PX


def _check_axe_results(axe_results: Any, out: list[Finding],
                       measured: dict[str, Any]) -> None:
    """Map supplied axe violations onto audit-rule findings.

    Shape: {"violations": [{"rule", "impact", "description", "nodes"}]}.
    ``measured["axe_violations"]`` records how many violations were
    supplied; only violations with a mapped audit rule become findings.
    """
    if not isinstance(axe_results, dict):
        return
    violations = axe_results.get("violations")
    if not isinstance(violations, list):
        violations = []
    measured["axe_violations"] = len(violations)
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        rule_id = _axe_rule_id(violation)
        if rule_id is None:
            continue
        impact = str(violation.get("impact", "")).lower()
        severity = _AXE_SEVERITY.get(impact, "defect")
        axe_rule = str(violation.get("rule", ""))
        description = str(violation.get("description") or axe_rule)
        nodes = [str(n) for n in violation.get("nodes") or []
                 if isinstance(n, str)]
        location = nodes[0] if nodes else f"axe:{axe_rule}"
        out.append(_f(rule_id, severity, location,
                      _AXE_EXPECTED[rule_id],
                      f"axe violation {axe_rule} "
                      f"({impact or 'unknown impact'}): {description}",
                      f"axe {axe_rule} on "
                      f"{', '.join(nodes) if nodes else 'no node listed'}"
                      f": {description}"))


def judge(project_dir: str | Path, *, tokens: Any = None,
          screenshots: Any = None, live_metrics: Any = None,
          brief: Any = None, content_draft: Any = None,
          axe_results: Any = None, layout: Any = None) -> Verdict:
    """Judge one built site for WCAG 2.2 AA. See module docstring.
    ``axe_results`` is the optional axe-runner output dict; when
    supplied its violations are mapped onto audit rules as findings.
    ``layout`` is accepted (and ignored) so the coordinator can pass
    the same optional inputs to every judger through :func:`judge_all`.
    """
    project = load_project(project_dir)
    if project is None:
        return unjudged_verdict(JUDGER)
    findings: list[Finding] = []
    measured: dict[str, Any] = {
        "pages": len(project.html),
        "contrast_pairs_checked": len(_text_pairs(project))}
    _check_contrast(project, tokens, findings)
    _check_non_text_contrast(project, findings)
    _check_semantics(project, findings)
    _check_alt_text(project, findings)
    _check_skip_link(project, findings)
    _check_focus(project, findings)
    _check_keyboard(project, findings)
    _check_resize_and_flash(project, findings)
    _check_axe_results(axe_results, findings, measured)
    # W3C Markup slot (local parse): no audit rule maps to tag balance,
    # so parse errors land in measured for the coordinator, never as a
    # finding under a rule they do not violate.
    html_result = check_html_parse(project)
    measured["html_parse_errors"] = (
        html_result["measured"]["html_parse_errors"])
    checked = ["R072", "R073", "R074", "R075", "R076", "R077", "R078",
               "R079", "R080", "R081", "R082"]
    return make_verdict(
        JUDGER, findings, unchecked_for(JUDGER),
        measured=measured,
        checked=checked,
    )
