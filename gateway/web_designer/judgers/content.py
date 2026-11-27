"""Content and copy judger (stage 22).

Forms (single column, persistent labels, blur validation with specific
errors, only necessary fields, correct input types), navigation (5-7
items, breadcrumbs on deep pages), inverted pyramid and plain language,
'[verify]' flags (unresolved = blocker), broken relative links, and no
lorem ipsum / placeholder text in delivered copy. External links cannot
be verified without the network, so they are recorded in
``measured["external_links_unchecked"]``, never claimed checked.

UX / psychology rules (audit section 4: R041-R061, R106, R110) are
checked statically from markup, text and JS: choice counts and grouping,
conventions (brand-first header, nav, search in header), chunking,
identity-field affordances, flow endings, dead CTAs, strict input
patterns, generic labels, disclosure, recall demands, undo and
confirmation, cross-page consistency, system-status feedback, jargon,
error copy, decoration counts, choice overload, totals, serial position,
link scent and checkout trust. A rule whose trigger is absent on a page
(no checkout page, no form, no destructive button) passes silently.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .checkers import check_links
from .common import (
    Project, decls_for, load_project, make_finding, make_verdict,
    unchecked_for, unjudged_verdict,
)
from ..types import Finding, Verdict

JUDGER = "content"
_PLACEHOLDERS = ("lorem ipsum", "loremipsum", "[todo]", "xxx placeholder")
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")


def _f(rule: str, severity: str, location: str, expected: str,
       actual: str, evidence: str) -> Finding:
    return make_finding(rule, JUDGER, severity, location, expected, actual,
                        evidence)


def _form_fields(doc: Any) -> list[Any]:
    return [e for e in doc.elements
            if e.tag in {"input", "textarea", "select"}]


def _check_forms(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        fields = _form_fields(doc)
        forms = doc.find("form")
        for form in forms:
            if len(fields) > 8:
                out.append(_f("R103", "change", f"{rel}:{form.line}",
                              "ask only for fields genuinely needed (ideal "
                              "checkout is 6-8 fields)",
                              f"form has {len(fields)} fields",
                              f"{len(fields)} inputs in form at {rel}:{form.line}"))
        form_decls = decls_for(project.css_rules, "form")
        columns = form_decls.get("grid-template-columns", "")
        if columns and len(columns.split()) > 1:
            out.append(_f("R100", "defect", "form",
                          "forms in a single column with labels above each "
                          "field",
                          f"form grid has multiple columns: {columns}",
                          f"grid-template-columns: {columns} on form"))
        labels = {e.attrs.get("for", "") for e in doc.find("label")}
        for field in fields:
            field_id = field.attrs.get("id", "")
            name = field.attrs.get("name", "") or field_id
            if field_id and field_id in labels:
                continue
            if field.attrs.get("aria-label") or field.attrs.get("aria-labelledby"):
                continue
            if field.attrs.get("placeholder"):
                out.append(_f("R101", "defect", f"{rel}:{field.line}",
                              "persistent visible label outside the input; "
                              "placeholder is a hint only, never the label",
                              f"field {name!r} has a placeholder but no label",
                              f"<{field.tag} name={name!r}> at {rel}:{field.line}"))
            else:
                out.append(_f("R101", "defect", f"{rel}:{field.line}",
                              "persistent visible label outside the input",
                              f"field {name!r} has no associated label",
                              f"<{field.tag} name={name!r}> at {rel}:{field.line}"))
        if fields:
            has_blur = "blur" in project.js_text or "focusout" in project.js_text
            if not has_blur:
                out.append(_f("R102", "change", rel,
                              "validate on blur (field exit) and clear the "
                              "error live as it is corrected",
                              "form present but no blur/focusout validation "
                              "in JS",
                              "no 'blur'/'focusout' listener in project JS"))
            elif re.search(r"['\"]invalid( input)?['\"]",
                           project.js_text, re.IGNORECASE):
                out.append(_f("R102", "change", rel,
                              "errors state what is wrong and how to fix it",
                              "validation error text is generic ('invalid')",
                              "generic error string in project JS"))
            for field in fields:
                ftype = field.attrs.get("type", "text")
                name = (field.attrs.get("name", "")
                        or field.attrs.get("id", "")).lower()
                expected = None
                if "email" in name:
                    expected = "email"
                elif "phone" in name or "tel" in name:
                    expected = "tel"
                elif "password" in name:
                    expected = "password"
                if expected and ftype != expected:
                    out.append(_f("R104", "defect", f"{rel}:{field.line}",
                                  f"correct type for the field ({expected})",
                                  f"field {name!r} uses type={ftype!r}",
                                  f"<{field.tag} type={ftype!r}> at {rel}:{field.line}"))
                if "email" in name and not field.attrs.get("autocomplete"):
                    out.append(_f("R104", "change", f"{rel}:{field.line}",
                                  "autocomplete for identity fields",
                                  f"email field {name!r} has no autocomplete",
                                  f"<{field.tag} name={name!r}> at {rel}:{field.line}"))


def _check_navigation(project: Project, out: list[Finding]) -> None:
    for rel, doc in project.html:
        navs = doc.find("nav")
        for nav in navs:
            end = next((ln for tag, ln in doc.ends
                        if tag == "nav" and ln >= nav.line), None)
            links = [e for e in doc.find("a")
                     if e.line >= nav.line and (end is None or e.line <= end)]
            if links and not 5 <= len(links) <= 7:
                out.append(_f("R105", "change", f"{rel}:{nav.line}",
                              "primary navigation 5-7 top-level items",
                              f"nav has {len(links)} top-level items",
                              f"{len(links)} links in <nav> at {rel}:{nav.line}"))
            break
        hamburger = any("hamburger" in e.attrs.get("class", "")
                        for e in doc.elements)
        desktop_hidden = any(
            r.media and "min-width" in r.media
            and any("hamburger" in s for s in r.selectors)
            for r in project.css_rules)
        if hamburger and not desktop_hidden:
            out.append(_f("R107", "change", rel,
                          "main navigation visible on desktop (no hamburger "
                          "on desktop)",
                          "hamburger menu present with no desktop rule",
                          "hamburger element in HTML, no min-width rule"))
    deep = len(project.html) > 2
    if deep:
        has_crumbs = any(
            "breadcrumb" in e.attrs.get("class", "")
            or e.attrs.get("aria-label") == "breadcrumb"
            for _, doc in project.html for e in doc.elements)
        if not has_crumbs:
            out.append(_f("R107", "change", "navigation",
                          "breadcrumbs on pages deeper than two levels",
                          f"{len(project.html)} pages but no breadcrumbs",
                          f"{len(project.html)} HTML pages, no breadcrumb nav"))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip()]


def _check_copy(project: Project, content_draft: Any,
                out: list[Finding]) -> None:
    for rel, doc in project.html:
        headings = [e for e in doc.elements if e.tag in {"h1", "h2"}]
        for el in headings:
            text = next((t for t, ln in doc.texts if ln == el.line), "")
            if not text.strip():
                out.append(_f("R108", "defect", f"{rel}:{el.line}",
                              "front-load every page with the most important "
                              "information first",
                              f"{el.tag} is empty",
                              f"<{el.tag}> at {rel}:{el.line}"))
        for text, line in doc.texts:
            if len(text) < 20:
                continue
            sentences = _sentences(text)
            if not sentences:
                continue
            first = sentences[0]
            if len(first.split()) < 3:
                out.append(_f("R108", "change", f"{rel}:{line}",
                              "first sentence carries the message (inverted "
                              "pyramid)",
                              f"first sentence is only {len(first.split())} "
                              "words",
                              f"paragraph at {rel}:{line}"))
            if len(sentences) > 4:
                out.append(_f("R109", "change", f"{rel}:{line}",
                              "short paragraphs (2-4 sentences)",
                              f"paragraph has {len(sentences)} sentences",
                              f"paragraph at {rel}:{line}"))
            for sentence in sentences:
                if len(sentence.split()) > 20:
                    out.append(_f("R109", "change", f"{rel}:{line}",
                                  "short sentences (<=20 words)",
                                  f"sentence has {len(sentence.split())} words",
                                  f"sentence at {rel}:{line}: "
                                  f"{sentence[:60]!r}"))
                    break
    for rel, doc in project.html:
        text = doc.text().lower()
        for marker in _PLACEHOLDERS:
            if marker in text:
                out.append(_f("R108", "defect", rel,
                              "no placeholder text or lorem ipsum in "
                              "delivered copy",
                              f"delivered copy contains {marker!r}",
                              f"{marker!r} in {rel}"))
                break


def _check_verify_flags(project: Project, content_draft: Any,
                        out: list[Finding]) -> None:
    for rel, text in project.files.items():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "[verify]" in line.lower():
                out.append(_f("R108", "blocker", f"{rel}:{lineno}",
                              "every '[verify]' flag resolved (verified, "
                              "corrected, or removed) before delivery",
                              f"unresolved '[verify]' flag: {line.strip()[:80]!r}",
                              f"[verify] at {rel}:{lineno}"))
    draft = content_draft if isinstance(content_draft, str) else (
        str(content_draft) if isinstance(content_draft, dict) else "")
    if draft:
        for lineno, line in enumerate(draft.splitlines(), start=1):
            if "[verify]" in line.lower():
                out.append(_f("R108", "blocker", f"content draft:{lineno}",
                              "every '[verify]' flag resolved (verified, "
                              "corrected, or removed) before delivery",
                              f"unresolved '[verify]' flag: {line.strip()[:80]!r}",
                              f"[verify] at content draft:{lineno}"))


def _check_links(project: Project, out: list[Finding],
                 measured: dict[str, Any]) -> None:
    # One implementation of the link check lives in checkers.py (the
    # Dead Link Checker registry slot); this judger only converts its
    # violations into findings so the two can never drift apart.
    result = check_links(project)
    for violation in result["violations"]:
        out.append(_f(violation["rule_id"], violation["severity"],
                      violation["location"], violation["expected"],
                      violation["actual"], violation["evidence"]))
    measured.update(result["measured"])


# --- UX / psychology rules (audit section 4: R041-R061, R106, R110) ----

_GENERIC_LABELS = {"submit", "ok", "click here", "read more", "learn more",
                   "go"}
_VAGUE_LINKS = {"click here", "here", "more", "link", "this"}
_JARGON = ("sku", "dept", "backend", "cms", "api key", "webhook",
           "org-chart", "org chart")
_SUCCESS_MARKERS = ("thank", "success", "confirm", "submitted",
                    "order placed", "welcome", "you're in")
_DESTRUCTIVE_RE = re.compile(
    r"\b(delete|remove|unsubscribe|cancel subscription)\b", re.IGNORECASE)
_PRICE_RE = re.compile(r"[$€£]\s?\d")
_LONG_DIGITS_RE = re.compile(r"\d{8,}")
_HEX_CODE_RE = re.compile(r"0x[0-9a-fA-F]{6,}")
_URGENCY_RE = re.compile(
    r"only \d+ left|hurry|ends in|offer ends|limited time|left in stock"
    r"|countdown|act now", re.IGNORECASE)
_TRUST_WORDS = ("return", "secure", "security", "review", "guarantee",
                "trusted", "ssl", "verified")
_PLAN_CLASSES = frozenset({"plan", "plan-card", "pricing-plan", "price-plan",
                           "tier", "pricing-card"})
_FEATURE_CLASSES = frozenset({"feature", "feature-card"})
_RECOMMENDED_RE = re.compile(r"recommended|most popular|best value",
                             re.IGNORECASE)
# A pattern made only of digit classes with fixed counts, e.g. [0-9]{10}
# or \d{10}: it demands one exact format and rejects spaces/dashes.
_DIGITS_ONLY_PATTERN_RE = re.compile(r"^(?:(?:\\d|\[0-9\])(?:\{\d+\})?)+$")
_IDENTITY_HINTS = ("email", "phone", "tel", "date", "birth")
_FREE_TEXT_NAMES = {"name", "message", "comment", "description", "bio",
                    "title"}
_CHOICE_LIMIT = 7


def _texts_by_line(doc: Any) -> dict[int, str]:
    grouped: dict[int, list[str]] = {}
    for text, line in doc.texts:
        grouped.setdefault(line, []).append(text)
    return {line: " ".join(parts) for line, parts in grouped.items()}


def _text_at(texts: dict[int, str], line: int) -> str:
    return texts.get(line, "")


def _range_end(doc: Any, tag: str, start: int) -> int | None:
    for end_tag, line in doc.ends:
        if end_tag == tag and line >= start:
            return line
    return None


def _limit(end: int | None) -> int:
    return end if end is not None else 1 << 30


def _in_range(doc: Any, start: int, end: int | None) -> list[Any]:
    limit = _limit(end)
    return [e for e in doc.elements if start <= e.line <= limit]


def _has_grouping(doc: Any, start: int, end: int | None) -> bool:
    els = _in_range(doc, start, end)
    if any(e.tag in {"optgroup", "fieldset", "section"} for e in els):
        return True
    return sum(1 for e in els if e.tag == "ul") > 1


def _is_home_link(el: Any, label: str) -> bool:
    href = el.attrs.get("href", "")
    base = href.split("#")[0].split("?")[0].rstrip("/").split("/")[-1]
    if base in {"", "index.html", "index.htm"} or href in {"/", "#"}:
        return True
    if label.strip().lower() == "home":
        return True
    classes = el.attrs.get("class", "").lower()
    return "logo" in classes or "brand" in classes


def _is_checkout(rel: str, doc: Any) -> bool:
    low = rel.lower()
    if any(k in low for k in ("checkout", "order", "cart")):
        return True
    if any("checkout" in e.attrs.get("class", "").lower()
           or "order-summary" in e.attrs.get("class", "").lower()
           for e in doc.elements):
        return True
    text = doc.text().lower()
    return "order summary" in text or "proceed to checkout" in text


def _has_success(doc: Any, js_text: str) -> bool:
    hay = doc.text().lower() + "\n" + js_text.lower()
    return any(m in hay for m in _SUCCESS_MARKERS)


def _element_label(el: Any, texts: dict[int, str]) -> str:
    if el.tag == "input":
        return el.attrs.get("value", "").strip()
    return _text_at(texts, el.line).strip()


def _is_exact_digits_pattern(pattern: str) -> bool:
    stripped = pattern.strip().lstrip("^").rstrip("$")
    return bool(_DIGITS_ONLY_PATTERN_RE.fullmatch(stripped))


def _check_hick_miller(project: Project, out: list[Finding]) -> None:
    """R041 (ungrouped choice lists above ~7) and R043 (unchunked groups)."""
    for rel, doc in project.html:
        for nav in doc.find("nav"):
            end = _range_end(doc, "nav", nav.line)
            links = [e for e in doc.find("a")
                     if nav.line <= e.line <= _limit(end)]
            if len(links) > _CHOICE_LIMIT and not _has_grouping(
                    doc, nav.line, end):
                out.append(_f("R041", "change", f"{rel}:{nav.line}",
                              "no more than ~7 ungrouped choices in one list",
                              f"nav lists {len(links)} links with no grouping",
                              f"{len(links)} links in <nav> at {rel}:{nav.line}"))
                out.append(_f("R043", "change", f"{rel}:{nav.line}",
                              "chunk navigation into groups of 4-7",
                              f"nav lists {len(links)} items ungrouped",
                              f"{len(links)} links in <nav> at {rel}:{nav.line}"))
        for sel in doc.find("select"):
            end = _range_end(doc, "select", sel.line)
            options = [e for e in doc.find("option")
                       if sel.line <= e.line <= _limit(end)]
            grouped = any(e.tag == "optgroup"
                          for e in _in_range(doc, sel.line, end))
            if len(options) > _CHOICE_LIMIT and not grouped:
                out.append(_f("R041", "change", f"{rel}:{sel.line}",
                              "no more than ~7 ungrouped choices in one list",
                              f"select has {len(options)} options with no "
                              "grouping",
                              f"{len(options)} <option> at {rel}:{sel.line}"))
        for form in doc.find("form"):
            end = _range_end(doc, "form", form.line)
            fields = [e for e in _form_fields(doc)
                      if form.line <= e.line <= _limit(end)]
            grouped = any(e.tag == "fieldset"
                          for e in _in_range(doc, form.line, end))
            if len(fields) > _CHOICE_LIMIT and not grouped:
                out.append(_f("R041", "change", f"{rel}:{form.line}",
                              "no more than ~7 ungrouped choices in one list",
                              f"form has {len(fields)} fields with no grouping",
                              f"{len(fields)} fields in <form> at "
                              f"{rel}:{form.line}"))
                out.append(_f("R043", "change", f"{rel}:{form.line}",
                              "chunk form sections into groups of 4-7 fields",
                              f"form section has {len(fields)} fields with no "
                              "<fieldset>",
                              f"{len(fields)} fields in <form> at "
                              f"{rel}:{form.line}"))
        for text, line in doc.texts:
            match = _LONG_DIGITS_RE.search(text)
            if match:
                out.append(_f("R043", "change", f"{rel}:{line}",
                              "chunk long digit strings (spaces/dashes every "
                              "3-4 digits)",
                              f"unchunked digit string {match.group(0)!r}",
                              f"{match.group(0)!r} at {rel}:{line}"))
                break


def _check_jakob(project: Project, out: list[Finding]) -> None:
    """R042: brand/home first in the header, nav on multi-page sites,
    search in the header."""
    for rel, doc in project.html:
        texts = _texts_by_line(doc)
        for header in doc.find("header"):
            end = _range_end(doc, "header", header.line)
            links = [e for e in doc.find("a")
                     if header.line <= e.line <= _limit(end)]
            if links and not _is_home_link(
                    links[0], _text_at(texts, links[0].line)):
                out.append(_f("R042", "change", f"{rel}:{links[0].line}",
                              "first link in the header is the brand/home link",
                              f"first header link is "
                              f"{_text_at(texts, links[0].line)!r} "
                              f"({links[0].attrs.get('href', '')!r})",
                              f"<a href={links[0].attrs.get('href', '')!r}> "
                              f"at {rel}:{links[0].line}"))
        ranges = [(h.line, _limit(_range_end(doc, "header", h.line)))
                  for h in doc.find("header")]
        for el in doc.find("input"):
            blob = (el.attrs.get("name", "") + el.attrs.get("id", "")
                    + el.attrs.get("role", "")).lower()
            is_search = el.attrs.get("type") == "search" or "search" in blob
            if (is_search and ranges
                    and not any(s <= el.line <= e for s, e in ranges)):
                out.append(_f("R042", "change", f"{rel}:{el.line}",
                              "search sits in the header, as users expect",
                              "search control outside the header",
                              f"<input type={el.attrs.get('type', '')!r}> at "
                              f"{rel}:{el.line}"))
    if len(project.html) > 1 and not any(doc.find("nav")
                                         for _, doc in project.html):
        out.append(_f("R042", "change", "navigation",
                      "navigation present on multi-page sites",
                      f"{len(project.html)} pages but no <nav>",
                      f"{len(project.html)} HTML pages, no <nav>"))


def _check_tesler_postel(project: Project, out: list[Finding]) -> None:
    """R044 (inputs absorb complexity) and R048 (accept varied formats)."""
    for rel, doc in project.html:
        for field in _form_fields(doc):
            name = (field.attrs.get("name", "")
                    or field.attrs.get("id", "")).lower()
            ftype = field.attrs.get("type", "text").lower()
            is_identity = ftype in {"email", "tel", "date", "number"} or any(
                k in name for k in _IDENTITY_HINTS)
            if is_identity and not (field.attrs.get("autocomplete")
                                    or field.attrs.get("inputmode")
                                    or field.attrs.get("value")):
                out.append(_f("R044", "change", f"{rel}:{field.line}",
                              "identity fields absorb complexity "
                              "(autocomplete/inputmode/default)",
                              f"identity field {name!r} has no autocomplete, "
                              "inputmode or default",
                              f"<{field.tag} name={name!r}> at {rel}:{field.line}"))
            pattern = field.attrs.get("pattern", "")
            if pattern and _is_exact_digits_pattern(pattern):
                out.append(_f("R044", "defect", f"{rel}:{field.line}",
                              "patterns must not demand one exact format",
                              f"pattern {pattern!r} demands exact digits only",
                              f"pattern={pattern!r} at {rel}:{field.line}"))
                if "phone" in name or "tel" in name or ftype == "tel":
                    out.append(_f("R048", "defect", f"{rel}:{field.line}",
                                  "accept varied valid phone formats (spaces, "
                                  "dashes)",
                                  f"phone pattern {pattern!r} forbids spaces "
                                  "and dashes",
                                  f"pattern={pattern!r} at {rel}:{field.line}"))
            maxlength = field.attrs.get("maxlength", "")
            if (maxlength.isdigit() and int(maxlength) <= 12
                    and ftype in {"text", ""} and name in _FREE_TEXT_NAMES):
                out.append(_f("R048", "change", f"{rel}:{field.line}",
                              "maxlength must not cap free text at an exact "
                              "short length",
                              f"free-text field {name!r} has maxlength="
                              f"{maxlength}",
                              f"maxlength={maxlength} at {rel}:{field.line}"))


def _check_flow_endings(project: Project, out: list[Finding]) -> None:
    """R046 (clear success ending), R047 (core task works), R054 (feedback
    on submit)."""
    js = project.js_text
    js_low = js.lower()
    for rel, doc in project.html:
        for form in doc.find("form"):
            if not _has_success(doc, js):
                out.append(_f("R046", "change", f"{rel}:{form.line}",
                              "flows end with a clear success/confirmation "
                              "state",
                              "form has no success, confirmation or "
                              "thank-you state",
                              f"<form> at {rel}:{form.line}, no success text "
                              "in HTML/JS"))
            has_loading = ("loading" in js_low or "disabled" in js_low
                           or any(e.attrs.get("aria-busy")
                                  for e in doc.elements))
            has_live = any(e.attrs.get("aria-live") for e in doc.elements)
            if not has_loading and not has_live and not _has_success(doc, js):
                out.append(_f("R054", "change", f"{rel}:{form.line}",
                              "submitting shows feedback (loading/disabled, "
                              "live region, success handling)",
                              "form submit has no loading/disabled state, "
                              "aria-live region or success handling",
                              f"<form> at {rel}:{form.line}"))
            if not form.attrs.get("action") and "submit" not in js_low:
                end = _range_end(doc, "form", form.line)
                submits = [e for e in doc.find("button")
                           if e.attrs.get("type") == "submit"
                           and form.line <= e.line <= _limit(end)]
                submits += [e for e in doc.find("input")
                            if e.attrs.get("type") == "submit"
                            and form.line <= e.line <= _limit(end)]
                if submits:
                    out.append(_f("R047", "defect", f"{rel}:{form.line}",
                                  "the core task must work: a submit button "
                                  "needs a form action or JS handler",
                                  "submit button with no form action and no "
                                  "JS submit handling",
                                  f"<form> at {rel}:{form.line}"))
        for el in doc.find("a"):
            classes = el.attrs.get("class", "")
            if "btn-primary" in classes or "cta" in classes:
                href = el.attrs.get("href", "")
                if (href in ("#", "") and "click" not in js_low
                        and "addeventlistener" not in js_low):
                    out.append(_f("R047", "defect", f"{rel}:{el.line}",
                                  "the core task must work: a primary CTA "
                                  "must go somewhere or do something",
                                  f"primary CTA links to {href!r} with no JS "
                                  "handler",
                                  f"<a href={href!r}> at {rel}:{el.line}"))


def _doc_has_helper(doc: Any) -> bool:
    return bool(doc.find("small")) or any(
        "hint" in e.attrs.get("class", "").lower()
        or "help" in e.attrs.get("class", "").lower()
        for e in doc.elements)


def _check_generic_labels(rel: str, doc: Any, texts: Any,
                          out: list[Finding]) -> None:
    """R049 (generic labels) and R106 (vague link text)."""
    for el in doc.find("button") + doc.find("a"):
        label = _element_label(el, texts)
        low = label.lower()
        if (low in _GENERIC_LABELS
                and not (el.attrs.get("aria-label")
                         or el.attrs.get("title"))):
            out.append(_f("R049", "change", f"{rel}:{el.line}",
                          "buttons and links say what they do, in context",
                          f"generic label {label!r} with no context",
                          f"<{el.tag}> {label!r} at {rel}:{el.line}"))
        if el.tag == "a" and low in _VAGUE_LINKS:
            out.append(_f("R106", "defect", f"{rel}:{el.line}",
                          "links carry information scent (say where they "
                          "go)",
                          f"vague link text {label!r}",
                          f"<a> {label!r} at {rel}:{el.line}"))


def _check_jargon_copy(rel: str, doc: Any, texts: Any,
                       out: list[Finding]) -> None:
    """R055 (jargon in visible copy)."""
    nav_links: list[Any] = []
    for nav in doc.find("nav"):
        end = _range_end(doc, "nav", nav.line)
        nav_links += [e for e in doc.find("a")
                      if nav.line <= e.line <= _limit(end)]
    for el in nav_links + doc.find("button") + doc.find("label"):
        text = (_text_at(texts, el.line) if el.tag != "input"
                else el.attrs.get("value", ""))
        for word in _JARGON:
            if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
                out.append(_f("R055", "defect", f"{rel}:{el.line}",
                              "visible copy uses the user's words, not "
                              "internal jargon",
                              f"jargon {word!r} in visible text "
                              f"{text.strip()!r}",
                              f"{word!r} at {rel}:{el.line}"))
                break


def _check_recall_demands(rel: str, doc: Any, out: list[Finding]) -> None:
    """R051 (flows that demand recalling data from another page)."""
    for field in _form_fields(doc):
        name = (field.attrs.get("name", "")
                or field.attrs.get("id", "")).lower()
        if any(k in name for k in ("promo", "code", "discount", "voucher",
                                   "coupon")):
            helper = (field.attrs.get("aria-describedby")
                      or field.attrs.get("placeholder")
                      or _doc_has_helper(doc))
            if not helper:
                out.append(_f("R051", "change", f"{rel}:{field.line}",
                              "code/promo fields show where the code "
                              "comes from",
                              f"code field {name!r} has no helper text",
                              f"<{field.tag} name={name!r}> at "
                              f"{rel}:{field.line}"))
    for text, line in doc.texts:
        if (re.search(r"code from your (email|phone|inbox|account)",
                      text, re.IGNORECASE)
                and not _doc_has_helper(doc)):
            out.append(_f("R051", "change", f"{rel}:{line}",
                          "flows never demand recalling data from "
                          "another page",
                          "copy demands a code from another page: "
                          f"{text.strip()[:60]!r}",
                          f"{text.strip()[:80]!r} at {rel}:{line}"))


def _check_error_copy(rel: str, doc: Any, out: list[Finding]) -> None:
    """R057 (error copy) in page text and error elements."""
    for text, line in doc.texts:
        low = text.lower()
        if ("invalid input" in low or "something went wrong" in low
                or low.strip() in {"error", "error!"}):
            out.append(_f("R057", "defect", f"{rel}:{line}",
                          "errors say what is wrong and how to fix it",
                          f"generic error copy: {text.strip()[:60]!r}",
                          f"{text.strip()[:80]!r} at {rel}:{line}"))
        match = _HEX_CODE_RE.search(text)
        if match:
            out.append(_f("R057", "defect", f"{rel}:{line}",
                          "errors say what is wrong and how to fix it, "
                          "never a bare code",
                          f"bare error code {match.group(0)!r} shown to "
                          "the user",
                          f"{match.group(0)!r} at {rel}:{line}"))
    error_els = [e for e in doc.elements
                 if "error" in e.attrs.get("class", "").lower()
                 or e.attrs.get("role") == "alert"]
    if error_els and not any(f.attrs.get("aria-describedby")
                             or f.attrs.get("aria-invalid")
                             for f in _form_fields(doc)):
        el = error_els[0]
        out.append(_f("R057", "change", f"{rel}:{el.line}",
                      "error messages are linked to their field",
                      "error shown with no aria-describedby/aria-invalid "
                      "link to a field",
                      f"<{el.tag}> at {rel}:{el.line}"))


def _check_error_copy_js(project: Project, out: list[Finding]) -> None:
    """R057 (error copy) in scripts."""
    for rel, text in project.files.items():
        if not rel.endswith((".js", ".ts", ".tsx", ".jsx")):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            if "invalid input" in low or "something went wrong" in low:
                out.append(_f("R057", "defect", f"{rel}:{lineno}",
                              "errors say what is wrong and how to fix it",
                              f"generic error copy: {line.strip()[:60]!r}",
                              f"{line.strip()[:80]!r} at {rel}:{lineno}"))
            match = _HEX_CODE_RE.search(line)
            if match:
                out.append(_f("R057", "defect", f"{rel}:{lineno}",
                              "errors say what is wrong and how to fix it, "
                              "never a bare code",
                              f"bare error code {match.group(0)!r} shown to "
                              "the user",
                              f"{match.group(0)!r} at {rel}:{lineno}"))


def _check_labels_copy(project: Project, out: list[Finding]) -> None:
    """R049 (generic labels), R051 (recall demands), R055 (jargon),
    R057 (error copy), R106 (vague link text)."""
    for rel, doc in project.html:
        texts = _texts_by_line(doc)
        _check_generic_labels(rel, doc, texts, out)
        _check_jargon_copy(rel, doc, texts, out)
        _check_recall_demands(rel, doc, out)
        _check_error_copy(rel, doc, out)
    _check_error_copy_js(project, out)


def _check_disclosure_undo(project: Project, out: list[Finding]) -> None:
    """R050 (progressive disclosure), R052 (undo), R056 (confirm before
    destructive actions)."""
    js_low = project.js_text.lower()
    has_undo = "undo" in project.all_text.lower()
    for rel, doc in project.html:
        texts = _texts_by_line(doc)
        has_disclosure = bool(doc.find("details")) or any(
            "accordion" in e.attrs.get("class", "").lower()
            or "step" in e.attrs.get("class", "").lower()
            or "wizard" in e.attrs.get("class", "").lower()
            for e in doc.elements)
        for form in doc.find("form"):
            end = _range_end(doc, "form", form.line)
            fields = [e for e in _form_fields(doc)
                      if form.line <= e.line <= _limit(end)]
            if len(fields) > 8 and not has_disclosure:
                out.append(_f("R050", "change", f"{rel}:{form.line}",
                              "advanced/rare options sit behind disclosure, "
                              "steps or a secondary screen",
                              f"form shows {len(fields)} fields at once with "
                              "no disclosure",
                              f"{len(fields)} fields in <form> at "
                              f"{rel}:{form.line}"))
        for field in _form_fields(doc):
            name = (field.attrs.get("name", "") or field.attrs.get("id", "")
                    or field.attrs.get("class", "")).lower()
            if (("api" in name and "key" in name) or "apikey" in name
                    or "advanced" in name) and not has_disclosure:
                out.append(_f("R050", "change", f"{rel}:{field.line}",
                              "advanced settings sit behind disclosure",
                              f"advanced field {name!r} shown with no "
                              "disclosure",
                              f"<{field.tag} name={name!r}> at "
                              f"{rel}:{field.line}"))
        for el in doc.find("button") + doc.find("a"):
            label = _element_label(el, texts)
            if not _DESTRUCTIVE_RE.search(label):
                continue
            if not has_undo:
                out.append(_f("R052", "change", f"{rel}:{el.line}",
                              "destructive actions offer an undo afterwards",
                              f"destructive action {label!r} with no undo "
                              "affordance",
                              f"<{el.tag}> {label!r} at {rel}:{el.line}"))
            guarded = (el.attrs.get("data-confirm")
                       or "confirm(" in js_low
                       or bool(doc.find("dialog"))
                       or any("confirm" in e.attrs.get("class", "").lower()
                              for e in doc.elements))
            if not guarded:
                out.append(_f("R056", "defect", f"{rel}:{el.line}",
                              "destructive actions ask for confirmation "
                              "before acting",
                              f"destructive action {label!r} with no confirm "
                              "step",
                              f"<{el.tag}> {label!r} at {rel}:{el.line}"))


def _check_consistency(project: Project, out: list[Finding]) -> None:
    """R053: same action, same label and same primary style, every page."""
    submit_labels: dict[str, set[str]] = {}
    primary_classes: dict[str, set[str]] = {}
    for rel, doc in project.html:
        texts = _texts_by_line(doc)
        for el in doc.find("button"):
            if el.attrs.get("type") == "submit":
                label = _element_label(el, texts)
                if label:
                    submit_labels.setdefault(rel, set()).add(label)
        for el in doc.find("input"):
            if el.attrs.get("type") == "submit":
                value = el.attrs.get("value", "").strip()
                if value:
                    submit_labels.setdefault(rel, set()).add(value)
        for el in doc.find("a") + doc.find("button"):
            for token in el.attrs.get("class", "").split():
                if "primary" in token:
                    primary_classes.setdefault(rel, set()).add(token)
    all_labels = set().union(*submit_labels.values()) if submit_labels else set()
    if len(all_labels) > 1:
        out.append(_f("R053", "defect", "consistency",
                      "the same action keeps the same label on every page",
                      f"submit actions labelled differently: "
                      f"{sorted(all_labels)}",
                      f"submit labels {sorted(all_labels)} across pages"))
    all_primary = (set().union(*primary_classes.values())
                   if primary_classes else set())
    if len(all_primary) > 1:
        out.append(_f("R053", "defect", "consistency",
                      "primary buttons use one consistent style class",
                      f"primary buttons use different classes: "
                      f"{sorted(all_primary)}",
                      f"primary classes {sorted(all_primary)} across pages"))


def _plan_items(doc: Any) -> list[Any]:
    return [e for e in doc.elements
            if set(e.attrs.get("class", "").split()) & _PLAN_CLASSES]


def _check_choice_load(project: Project, out: list[Finding]) -> None:
    """R058 (decoration vs the goal), R059 (choice overload), R060 (no
    visible total), R061 (recommended item buried mid-list)."""
    for rel, doc in project.html:
        is_landing = rel == "index.html" or any(
            "hero" in e.attrs.get("class", "").lower()
            for e in doc.elements)
        checkout = _is_checkout(rel, doc)
        if is_landing or checkout:
            deco = [e for e in doc.elements
                    if any(k in token
                           for token in e.attrs.get("class", "").lower().split()
                           for k in ("banner", "popup", "promo", "modal"))]
            has_cta = any("btn-primary" in e.attrs.get("class", "")
                          or "cta" in e.attrs.get("class", "")
                          for e in doc.elements)
            if len(deco) > 2 and has_cta:
                out.append(_f("R058", "change", rel,
                              "landing/checkout pages keep decoration to at "
                              "most 2 competing elements beside the CTA",
                              f"{len(deco)} banner/popup/promo/modal elements "
                              "beside the primary CTA",
                              f"{len(deco)} competing elements in {rel}"))
        for sel in doc.find("select"):
            end = _range_end(doc, "select", sel.line)
            options = [e for e in doc.find("option")
                       if sel.line <= e.line <= _limit(end)]
            grouped = any(e.tag == "optgroup"
                          for e in _in_range(doc, sel.line, end))
            if len(options) > _CHOICE_LIMIT and not grouped:
                out.append(_f("R059", "change", f"{rel}:{sel.line}",
                              "curate choices: more than ~7 options need "
                              "grouping or a recommendation",
                              f"select has {len(options)} options with no "
                              "<optgroup>",
                              f"{len(options)} <option> at {rel}:{sel.line}"))
        plans = _plan_items(doc)
        if len(plans) > _CHOICE_LIMIT and not _RECOMMENDED_RE.search(doc.text()):
            out.append(_f("R059", "change", rel,
                          "curate choices: more than ~7 plans need a "
                          "recommended one",
                          f"{len(plans)} plans with no recommended option",
                          f"{len(plans)} plan items in {rel}"))
        if checkout:
            prices = [t for t, _ in doc.texts if _PRICE_RE.search(t)]
            low = doc.text().lower()
            if len(prices) >= 2 and "total" not in low and "subtotal" not in low:
                out.append(_f("R060", "defect", rel,
                              "checkout shows a visible total/subtotal so "
                              "users never compute it",
                              f"{len(prices)} prices shown with no total or "
                              "subtotal",
                              f"{len(prices)} price texts in {rel}, no total"))
        items = [e for e in doc.elements
                 if set(e.attrs.get("class", "").split())
                 & (_PLAN_CLASSES | _FEATURE_CLASSES)]
        if len(items) >= 3:
            for text, line in doc.texts:
                match = _RECOMMENDED_RE.search(text)
                if not match:
                    continue
                before = [i for i, e in enumerate(items) if e.line <= line]
                if not before:
                    break
                idx = before[-1]
                if idx not in (0, len(items) - 1):
                    out.append(_f("R061", "change", f"{rel}:{line}",
                                  "the recommended item sits first or last in "
                                  "the list",
                                  f"{match.group(0)!r} item is {idx + 1} of "
                                  f"{len(items)}",
                                  f"{match.group(0)!r} at {rel}:{line}, item "
                                  f"{idx + 1} of {len(items)}"))
                break


def _check_trust(project: Project, out: list[Finding]) -> None:
    """R110: hidden costs, forced accounts, fabricated urgency, missing
    trust signals — checkout/order pages only."""
    for rel, doc in project.html:
        if not _is_checkout(rel, doc):
            continue
        low = doc.text().lower()
        prices = [t for t, _ in doc.texts if _PRICE_RE.search(t)]
        if prices and "total" not in low and "subtotal" not in low:
            out.append(_f("R110", "defect", rel,
                          "the full total is visible before checkout",
                          f"{len(prices)} prices shown with no total (hidden "
                          "costs)",
                          f"{len(prices)} price texts in {rel}, no total"))
        if "create an account" in low and "guest" not in low:
            out.append(_f("R110", "defect", rel,
                          "guest checkout is offered; accounts are never "
                          "forced",
                          "checkout requires creating an account with no "
                          "guest option",
                          "'create an account' in {rel}, no 'guest' option"))
        for text, line in doc.texts:
            match = _URGENCY_RE.search(text)
            if match:
                out.append(_f("R110", "defect", f"{rel}:{line}",
                              "no fabricated scarcity or urgency",
                              f"urgency copy: {match.group(0)!r}",
                              f"{match.group(0)!r} at {rel}:{line}"))
                break
        has_cta = any("btn-primary" in e.attrs.get("class", "")
                      or "cta" in e.attrs.get("class", "")
                      for e in doc.elements)
        if has_cta and not any(w in low for w in _TRUST_WORDS):
            out.append(_f("R110", "change", rel,
                          "trust signals (returns, security, reviews) sit "
                          "next to the CTA",
                          "checkout CTA with no returns/security/reviews text",
                          f"primary CTA in {rel}, no trust words nearby"))


def judge(project_dir: str | Path, *, tokens: Any = None,
          screenshots: Any = None, live_metrics: Any = None,
          brief: Any = None, content_draft: Any = None,
          layout: Any = None, axe_results: Any = None) -> Verdict:
    """Judge one built site's content and copy. See module docstring.

    ``layout`` and ``axe_results`` are accepted (and ignored) so the
    coordinator can pass the same optional inputs to every judger
    through :func:`judge_all` without a per-judger filter.
    """
    project = load_project(project_dir)
    if project is None:
        return unjudged_verdict(JUDGER)
    findings: list[Finding] = []
    measured: dict[str, Any] = {}
    _check_forms(project, findings)
    _check_navigation(project, findings)
    _check_copy(project, content_draft, findings)
    _check_verify_flags(project, content_draft, findings)
    _check_links(project, findings, measured)
    _check_hick_miller(project, findings)
    _check_jakob(project, findings)
    _check_tesler_postel(project, findings)
    _check_flow_endings(project, findings)
    _check_labels_copy(project, findings)
    _check_disclosure_undo(project, findings)
    _check_consistency(project, findings)
    _check_choice_load(project, findings)
    _check_trust(project, findings)
    checked = ["R041", "R042", "R043", "R044", "R046", "R047", "R048",
               "R049", "R050", "R051", "R052", "R053", "R054", "R055",
               "R056", "R057", "R058", "R059", "R060", "R061", "R100",
               "R101", "R102", "R103", "R104", "R105", "R106", "R107",
               "R108", "R109", "R110"]
    return make_verdict(
        JUDGER, findings, unchecked_for(JUDGER), measured=measured,
        checked=checked,
    )
