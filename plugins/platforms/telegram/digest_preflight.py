"""Preflight gate for a Telegram rich digest — mostly local, one networked check.

Port of the 8 preflight checks from serejaris/telegram-skills ``tg-rich-digest``.
A digest is published on a schedule and cannot be edited after it is live, so
every check in the gate has a failure mode that only shows up after the post
is out. Run :func:`preflight_digest` before every edition; fix every blocking
failure, fix or explicitly accept every warning.

Statuses: ``pass`` (ok), ``fail`` (blocking — do not send), ``warn`` (fixable
— send only after an explicit accept), ``unchecked`` (could not verify).
Only the outbound-link check needs the network, and only when the caller
supplies a ``resolve_link`` callable; without one it reports ``unchecked``.
The duplicate check reads the local sent index
(``gateway.rich_sent_store``) for the chat when ``chat_id`` is given.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .rich_media import validate_rich_media_bindings

PREVIEW_BUDGET_CHARS = 500
PARAGRAPH_BUDGET_CHARS = 220
TELEGRAM_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")
_BARE_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_.@])@([A-Za-z0-9_]{1,64})")
_ATTACH_RE = re.compile(r"attach://[^\s\"'\)\]]+")
_HREF_RE = re.compile(r"""href\s*=\s*["'](https?://[^"'\s]+)["']""")
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'=\(\[])https?://[^\s\"'\)\]]+")
_DETAILS_OPEN_RE = re.compile(r"<details\b[^>]*>", re.IGNORECASE)
_DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class PreflightCheck:
    """One gate result: ``name``, ``status``, and a human-readable ``detail``."""

    name: str
    status: str  # "pass" | "fail" | "warn" | "unchecked"
    detail: str


def ok_to_send(checks: Iterable[PreflightCheck]) -> bool:
    """True when no check failed. Warnings still need an explicit accept."""
    return not any(check.status == "fail" for check in checks)


def blocking_failures(checks: Iterable[PreflightCheck]) -> list[PreflightCheck]:
    """The checks that block the send."""
    return [check for check in checks if check.status == "fail"]


def _visible_text(markup: str) -> str:
    """Strip tags and collapse whitespace; the text a reader actually sees."""
    text = _TAG_RE.sub("", markup or "")
    return re.sub(r"\s+", " ", text).strip()


def _preview_region(content: str) -> str:
    """Everything before the first ``<details>`` — the feed preview."""
    match = _DETAILS_OPEN_RE.search(content or "")
    return content if match is None else content[: match.start()]


def _check_preview_budget(content: str) -> PreflightCheck:
    if _DETAILS_OPEN_RE.search(content or "") is None:
        return PreflightCheck(
            "preview_budget", "pass", "Flat layout: no <details> preview cut."
        )
    preview = _visible_text(_preview_region(content))
    overlong = [
        paragraph
        for paragraph in re.split(r"\n\s*\n", _preview_region(content))
        if len(_visible_text(paragraph)) > PARAGRAPH_BUDGET_CHARS
    ]
    if len(preview) > PREVIEW_BUDGET_CHARS or overlong:
        return PreflightCheck(
            "preview_budget",
            "warn",
            f"Preview is {len(preview)} visible chars "
            f"(budget {PREVIEW_BUDGET_CHARS})"
            + (
                f"; {len(overlong)} paragraph(s) over "
                f"{PARAGRAPH_BUDGET_CHARS} chars"
                if overlong
                else ""
            )
            + ". The feed truncates the preview itself past the budget.",
        )
    return PreflightCheck(
        "preview_budget", "pass", f"Preview is {len(preview)} visible chars."
    )


def _check_details_shape(content: str) -> PreflightCheck:
    opens = len(_DETAILS_OPEN_RE.findall(content or ""))
    if opens == 0:
        return PreflightCheck(
            "details_shape", "pass", "Flat layout: no <details> block."
        )
    if opens > 1:
        return PreflightCheck(
            "details_shape",
            "warn",
            f"{opens} <details> blocks; the preview layout expects exactly one.",
        )
    tail = _DETAILS_CLOSE_RE.split(content or "", maxsplit=1)[-1]
    if _visible_text(tail):
        return PreflightCheck(
            "details_shape",
            "fail",
            "Text after the closing </details> reads as a detached fragment "
            "in the feed; keep the sign-off inside <details>.",
        )
    return PreflightCheck(
        "details_shape", "pass", "Exactly one <details>; sign-off inside it."
    )


def _check_media_bindings(content: str, media_bindings) -> PreflightCheck:
    try:
        validate_rich_media_bindings(content, media_bindings or [])
    except ValueError as exc:
        return PreflightCheck("media_bindings", "fail", str(exc))
    return PreflightCheck("media_bindings", "pass", "Every tg:// ref bound.")


def _check_attach_manifest(content: str) -> PreflightCheck:
    names = sorted(set(_ATTACH_RE.findall(content or "")))
    if names:
        return PreflightCheck(
            "attach_manifest",
            "fail",
            "attach:// is Bot API-only and is not supported in this port: "
            + ", ".join(names)
            + ". Rebind the media via file_id, URL, or upload instead.",
        )
    return PreflightCheck("attach_manifest", "pass", "No attach:// references.")


def _check_heading_rule_break(content: str, kind: str) -> PreflightCheck:
    if kind != "markdown":
        return PreflightCheck(
            "heading_rule_break", "pass", "Not applicable to HTML content."
        )
    lines = (content or "").splitlines()
    bad = [
        i + 1
        for i, line in enumerate(lines)
        if line.strip() == "---" and i > 0 and lines[i - 1].strip()
    ]
    if bad:
        return PreflightCheck(
            "heading_rule_break",
            "warn",
            "Blank line missing before '---' on line(s) "
            + ", ".join(map(str, bad))
            + "; Markdown reads the rule as a heading underline and turns "
            "the paragraph above it into an <h2>.",
        )
    return PreflightCheck("heading_rule_break", "pass", "Rules have a blank line.")


def _check_bare_handles(content: str) -> PreflightCheck:
    bad = [
        handle
        for handle in _BARE_HANDLE_RE.findall(_visible_text(content))
        if not TELEGRAM_USERNAME_RE.fullmatch(handle)
    ]
    if bad:
        return PreflightCheck(
            "bare_handles",
            "warn",
            "Bare @handle(s) that are not valid Telegram usernames: "
            + ", ".join(f"@{handle}" for handle in sorted(set(bad)))
            + ". Use a full URL for external handles.",
        )
    return PreflightCheck(
        "bare_handles", "pass", "Bare @handles are Telegram usernames only."
    )


def _extract_links(content: str) -> list[str]:
    found: list[str] = []
    for pattern in (_HREF_RE, _MD_LINK_RE, _BARE_URL_RE):
        found.extend(pattern.findall(content or ""))
    seen: set[str] = set()
    return [url for url in found if not (url in seen or seen.add(url))]


def _check_outbound_links(content: str, resolve_link) -> PreflightCheck:
    urls = _extract_links(content)
    if not urls:
        return PreflightCheck("outbound_links", "pass", "No outbound links.")
    if resolve_link is None:
        return PreflightCheck(
            "outbound_links",
            "unchecked",
            f"{len(urls)} outbound link(s); no resolver supplied.",
        )
    dead: list[str] = []
    for url in urls:
        try:
            alive = bool(resolve_link(url))
        except Exception:
            alive = False
        if not alive:
            dead.append(url)
    if dead:
        return PreflightCheck(
            "outbound_links",
            "fail",
            "Dead outbound link(s): " + ", ".join(dead) + ".",
        )
    return PreflightCheck(
        "outbound_links", "pass", f"All {len(urls)} outbound link(s) resolve."
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sent_texts_for_chat(chat_id) -> list[str]:
    """Previously sent texts for a chat, from the local sent index."""
    from gateway import rich_sent_store

    return rich_sent_store.texts_for_chat(chat_id)


def _check_duplicate(content: str, chat_id, sent_texts) -> PreflightCheck:
    if sent_texts is None:
        if chat_id is None:
            return PreflightCheck(
                "duplicate",
                "unchecked",
                "No sent index available: pass chat_id or sent_texts.",
            )
        sent_texts = _sent_texts_for_chat(chat_id)
    needle = _normalize(content)
    for sent in sent_texts or []:
        # The store keeps the first 2,000 chars of each sent text; compare
        # on that prefix so a truncated record still matches.
        if needle[:2000] and needle[:2000] == _normalize(sent)[:2000]:
            return PreflightCheck(
                "duplicate",
                "fail",
                "Identical to a previously sent edition.",
            )
    return PreflightCheck("duplicate", "pass", "Not a duplicate edition.")


def preflight_digest(
    content: str,
    *,
    kind: str = "markdown",
    media_bindings: Optional[Iterable[dict]] = None,
    chat_id=None,
    sent_texts: Optional[Iterable[str]] = None,
    resolve_link: Optional[Callable[[str], bool]] = None,
) -> list[PreflightCheck]:
    """Run the 8 digest preflight checks, in the source skill's order.

    ``content`` is the digest body as HTML or Markdown (``kind``). Blocks-dict
    digests go through their Markdown source for preflight. ``media_bindings``
    is the list of ``{"id", "media"}`` bindings for the tg:// references.
    ``sent_texts`` overrides the sent-index lookup for ``chat_id``.
    """
    return [
        _check_preview_budget(content),
        _check_details_shape(content),
        _check_media_bindings(content, media_bindings),
        _check_attach_manifest(content),
        _check_heading_rule_break(content, kind),
        _check_bare_handles(content),
        _check_outbound_links(content, resolve_link),
        _check_duplicate(content, chat_id, sent_texts),
    ]
