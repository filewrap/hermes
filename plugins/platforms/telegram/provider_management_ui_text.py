"""Pure text helpers for the Telegram provider-management UI.

Moved verbatim from ``provider_management_ui.py`` so that file stays within its
line budget: page footers, hints, notice splitting, credential status labels.
No Telegram client state lives here — only strings and rich-text nodes.
"""
from __future__ import annotations

import re
from typing import Any

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")

def _page_footer(page: int, pages: int, total: int) -> str:
    """Consistent page/total footer for Telegram catalogue cards."""
    return f"Page {int(page) + 1} of {int(pages)} • total {int(total)}"

_PROVIDER_HINTS = (
    "Use /model to configure model settings.",
    "Use /help to see all commands.",
)


def _provider_hints_block() -> str:
    """Hints footer for the providers card: both hints, professionally aligned."""
    return "<p><i>Hints: " + " · ".join(_PROVIDER_HINTS) + "</i></p>"

def _plain_rich_text(value: Any) -> str:
    """Flatten a native RichText node for locating a provider-status list item."""
    if isinstance(value, str):
        return value
    texts = getattr(value, "texts", None)
    if texts is not None:
        return "".join(_plain_rich_text(item) for item in texts)
    if hasattr(value, "text"):
        return _plain_rich_text(value.text)
    return ""

def _notice_segments(notice: str, limit: int = 240) -> list[tuple[str, bool]]:
    """Split a notice on `code` spans, truncated to ``limit`` plain chars.

    Backtick spans render monospace in both notice paths (TextFixed in
    blocks, <code> in HTML). A lone backtick — or one cut by truncation —
    stays literal text, and a span cut by the budget stays closed.
    """
    raw = str(notice or "").replace("\n", " ")
    segments: list[tuple[str, bool]] = []
    cursor = 0
    for match in _CODE_SPAN_RE.finditer(raw):
        if match.start() > cursor:
            segments.append((raw[cursor:match.start()], False))
        segments.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(raw):
        segments.append((raw[cursor:], False))
    if not segments:
        return [("", False)]
    kept: list[tuple[str, bool]] = []
    remaining = limit
    for segment, is_code in segments:
        if remaining <= 0:
            break
        if len(segment) <= remaining:
            kept.append((segment, is_code))
            remaining -= len(segment)
        else:
            kept.append((segment[:remaining], is_code))
            remaining = 0
    if sum(len(segment) for segment, _ in segments) > limit and kept:
        text, is_code = kept[-1]
        kept[-1] = (text + "…", is_code)
    return kept

def _notice_insert_at(blocks: list[Any]) -> int:
    """Where the notice block goes: above the divider that precedes the
    page footer — never between the divider and the page indexation."""
    from telethon import types

    insert_at = len(blocks)
    if blocks and _plain_rich_text(blocks[-1]).startswith("Page "):
        insert_at -= 1
        if insert_at > 0 and isinstance(blocks[insert_at - 1], types.PageBlockDivider):
            insert_at -= 1
    return insert_at

def _credential_status(provider: dict[str, Any]) -> str:
    if provider.get("credential_load_error") and not provider.get("ready"):
        return "Status unavailable"
    return "Added" if provider.get("ready") else "Missing"

def _credential_type_label(value: Any) -> str:
    raw = str(value or "credential").strip().casefold().replace("-", "_")
    return {
        "oauth": "OAuth",
        "api_key": "API key",
        "apikey": "API key",
        "device_code": "Device sign-in",
        "external_process": "Host sign-in",
    }.get(raw, raw.replace("_", " ").title()[:24] or "Credential")

def _is_free_model(model_id: Any) -> bool:
    """A model id is free when it carries a free marker: :free, -free or _free."""
    lowered = str(model_id or "").casefold()
    return any(marker in lowered for marker in (":free", "-free", "_free"))
