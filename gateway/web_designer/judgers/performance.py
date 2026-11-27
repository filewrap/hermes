"""Performance judger (stage 21): budgets and Core Web Vitals.

Budgets are acceptance conditions set before build: JS <= 200KB total,
images over ~200KB need a stated reason, responsive images with
srcset/sizes and modern formats, hero fetchpriority=high and never
loading=lazy on the LCP image, width/height or aspect-ratio on every
img. LCP/INP/CLS (R083-R085) are ``needs_live``: read from
``live_metrics`` when supplied (LCP <= 2.5s, INP <= 200ms, CLS <= 0.1
at p75); R045 (Doherty, feedback latency <= 400ms) is likewise
``needs_live`` and reads ``feedback_latency_ms`` (or ``feedback_ms``)
from ``live_metrics`` — INP is never used as a proxy for feedback
latency. When not supplied the live rules are listed in ``unchecked``
and ``measured`` stays empty — numbers are never invented.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import (
    Project, file_sizes, load_project, make_finding, make_verdict,
    unchecked_for, unjudged_verdict,
)
from ..types import Finding, Verdict

JUDGER = "performance"
JS_BUDGET_BYTES = 200 * 1024
IMAGE_FLAG_BYTES = 200 * 1024
_LIVE_RULES = (("R083", "lcp", 2.5, "s"), ("R084", "inp", 200.0, "ms"),
               ("R085", "cls", 0.1, ""))
_FEEDBACK_LIMIT_MS = 400.0
_FEEDBACK_KEYS = ("feedback_latency_ms", "feedback_ms")
_BUDGET_FILENAMES = {"budgets.json", "budget.json", ".budgetrc",
                     "performance-budget.json", "lighthouse-budget.json"}


def _f(rule: str, severity: str, location: str, expected: str,
       actual: str, evidence: str) -> Finding:
    return make_finding(rule, JUDGER, severity, location, expected, actual,
                        evidence)


def _live_value(live_metrics: Any, key: str) -> float | None:
    if not isinstance(live_metrics, dict):
        return None
    for candidate in (key, key.upper(), f"{key}_p75"):
        value = live_metrics.get(candidate)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _check_live_metrics(live_metrics: Any, out: list[Finding],
                        measured: dict[str, Any],
                        checked: list[str]) -> set[str]:
    """Evaluate R083-R085 from supplied live metrics; return measured ids."""
    done = set()
    for rule, key, limit, unit in _LIVE_RULES:
        value = _live_value(live_metrics, key)
        if value is None:
            continue
        done.add(rule)
        checked.append(rule)
        measured[key] = value
        if value > limit:
            out.append(_f(rule, "defect", "live metrics",
                          f"{key.upper()} <= {limit}{unit} at p75",
                          f"{key.upper()} is {value}{unit}",
                          f"live {key}={value}{unit} (p75)"))
    return done


def _check_feedback_latency(live_metrics: Any, out: list[Finding],
                            measured: dict[str, Any],
                            checked: list[str]) -> bool:
    """R045 (Doherty threshold): feedback latency <= 400ms.

    Reads ``feedback_latency_ms`` or ``feedback_ms`` from live metrics;
    INP is a different measurement and is never used as a proxy. Returns
    True only when that exact metric was supplied, so a partial
    live_metrics dict leaves R045 unchecked."""
    if not isinstance(live_metrics, dict):
        return False
    value: float | None = None
    for key in _FEEDBACK_KEYS:
        raw = live_metrics.get(key)
        if isinstance(raw, (int, float)):
            value = float(raw)
            break
    if value is None:
        return False
    checked.append("R045")
    measured["feedback_latency_ms"] = value
    if value > _FEEDBACK_LIMIT_MS:
        out.append(_f("R045", "defect", "live metrics",
                      "system response under 400ms to hold flow "
                      "(Doherty threshold)",
                      f"feedback latency is {value:g}ms",
                      f"live feedback_latency_ms={value:g}ms"))
    return True


def _check_budgets(project: Project, out: list[Finding],
                   measured: dict[str, Any]) -> None:
    sizes = file_sizes(project.root)
    measured.update({k: v for k, v in sizes.items()})
    if sizes["js_bytes"] > JS_BUDGET_BYTES:
        out.append(_f("R086", "defect", "assets",
                      "JS budget <= 200KB total",
                      f"JS total is {sizes['js_bytes'] / 1024:.0f}KB",
                      f"js_bytes={sizes['js_bytes']}"))
    has_config = any((project.root / name).is_file()
                     for name in _BUDGET_FILENAMES)
    if not has_config:
        for path in project.root.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and (
                    "budgets" in data or "performanceBudget" in data):
                has_config = True
                break
    if not has_config:
        out.append(_f("R086", "defect", "budgets",
                      "explicit budgets set before build (JS <= 200KB, "
                      "LCP <= 2.5s, requests <= 50)",
                      "no budget config file in the project",
                      "no budgets.json / budget.json / package.json budgets"))


def _check_images(project: Project, out: list[Finding]) -> None:
    modern = False
    for rel, doc in project.html:
        for el in doc.find("img"):
            src = el.attrs.get("src", "")
            if src.lower().endswith((".webp", ".avif")):
                modern = True
            if el.attrs.get("srcset"):
                modern = True
            if not el.attrs.get("srcset") and not el.attrs.get("sizes"):
                out.append(_f("R087", "defect", f"{rel}:{el.line}",
                              "responsive images with srcset/sizes and modern "
                              "formats (WebP/AVIF with fallback)",
                              f"img {src!r} has no srcset/sizes",
                              f"<img src={src!r}> at {rel}:{el.line}"))
            if "width" not in el.attrs and "height" not in el.attrs:
                style = el.attrs.get("style", "")
                if "aspect-ratio" not in style:
                    has_ratio = any(
                        "aspect-ratio" in d.value
                        for r in project.css_rules
                        for d in r.declarations
                        if "img" in " ".join(r.selectors))
                    if not has_ratio:
                        out.append(_f("R089", "defect", f"{rel}:{el.line}",
                                      "width and height (or aspect-ratio) on "
                                      "every img",
                                      f"img {src!r} has neither width/height "
                                      "nor aspect-ratio",
                                      f"<img src={src!r}> at {rel}:{el.line}"))
        for el in doc.find("source"):
            if el.attrs.get("type") in {"image/webp", "image/avif"}:
                modern = True
        for el in doc.find("picture"):
            modern = True
    if not modern and any(doc.find("img") for _, doc in project.html):
        out.append(_f("R087", "change", "images",
                      "modern formats (WebP/AVIF with fallback)",
                      "no WebP/AVIF image or <picture> source found",
                      "no modern-format image in project HTML"))
    for path in project.root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > IMAGE_FLAG_BYTES:
                out.append(_f("R087", "change",
                              str(path.relative_to(project.root)),
                              "images over ~200KB need a stated reason",
                              f"image is {size / 1024:.0f}KB",
                              f"{path.name}: {size} bytes"))


def _check_hero(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        images = doc.find("img")
        if not images:
            continue
        hero = images[0]  # first image is the LCP candidate
        if hero.attrs.get("loading") == "lazy":
            out.append(_f("R088", "defect", f"{rel}:{hero.line}",
                          "loading=lazy only for below-fold images, never "
                          "for the LCP image",
                          f"hero/LCP img {hero.attrs.get('src', '')!r} is "
                          "loading=lazy",
                          f"<img src={hero.attrs.get('src', '')!r}> at "
                          f"{rel}:{hero.line}"))
        if hero.attrs.get("fetchpriority") != "high":
            out.append(_f("R088", "defect", f"{rel}:{hero.line}",
                          "hero (LCP) image gets fetchpriority=high",
                          f"hero img {hero.attrs.get('src', '')!r} has "
                          f"fetchpriority={hero.attrs.get('fetchpriority', 'none')!r}",
                          f"<img src={hero.attrs.get('src', '')!r}> at "
                          f"{rel}:{hero.line}"))


def judge(project_dir: str | Path, *, tokens: Any = None,
          screenshots: Any = None, live_metrics: Any = None,
          brief: Any = None, content_draft: Any = None,
          layout: Any = None, axe_results: Any = None) -> Verdict:
    """Judge one built site for performance. See module docstring.

    ``layout`` and ``axe_results`` are accepted (and ignored) so the
    coordinator can pass the same optional inputs to every judger
    through :func:`judge_all` without a per-judger filter.
    """
    project = load_project(project_dir)
    if project is None:
        return unjudged_verdict(JUDGER)
    findings: list[Finding] = []
    measured: dict[str, Any] = {}
    checked: list[str] = []
    done = _check_live_metrics(live_metrics, findings, measured, checked)
    if _check_feedback_latency(live_metrics, findings, measured, checked):
        done.add("R045")
    _check_budgets(project, findings, measured)
    _check_images(project, findings)
    _check_hero(project, findings)
    checked.extend(("R086", "R087", "R088", "R089"))
    # Live-only rules stay unchecked unless that exact metric was supplied;
    # a partial live_metrics dict must not clear the unmeasured rules.
    unchecked = unchecked_for(JUDGER, live_measured=False, remove=done)
    return make_verdict(JUDGER, findings, unchecked, measured=measured,
                        checked=sorted(set(checked), key=lambda r: int(r[1:])))
