"""Shared rich-card builders for converted Telegram slash commands (Phase 5).

Pure functions only: Markdown-ish handler lines become Telegram rich HTML.
Values the user may want to reuse (session id, home path, user id) travel as
in-body copy buttons; links travel as in-body URL buttons, never as bare
hyperlinks. There are no card buttons that re-dispatch commands: the old
``hm:run:/...`` nav buttons were removed — a card that used to navigate now
says so in an italic hint instead.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`\n]+)`")
# Italic spans: the opening ``_`` must not be glued to a word character and
# neither must the closing one, so identifiers (``session_id``, ``t_abc123``)
# stay literal while ``_italic_`` renders as <i>.
_ITALIC_RE = re.compile(r"(?<![\w])_([^_\n]+?)_(?![\w])")
# Pictographic emoji (hourglass, warning sign, faces, transport, …) are
# stripped at render time: converted cards carry 0 emojis, even when the
# text comes from a shared engine (goal status lines carry a ⏳). Plain text
# status marks (✓/✗/♥) are not emoji presentation and are kept.
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2300-\u23FF\u2B00-\u2BFF\u26A0\u274C\u2705\uFE0F]")


SESSIONS_PAGE_SIZE = 10

# Session preview length: long enough to read as a message trail.
SESSIONS_PREVIEW_CHARS = 120

# Memories page size (same as sessions) and summary-preview length. The body
# always carries the FULL entry — the preview only shortens the summary line.
MEMORIES_PAGE_SIZE = 10
MEMORIES_PREVIEW_CHARS = 120


def sessions_listing_html(rows: Iterable[dict], *, page: int = 1, page_token: str | None = None,
                          current_session_id: str | None = None, title: str = "Sessions",
                          notice: str | None = None, numbered: bool = False) -> str:
    """Rich sessions listing: one ``<details>`` collapsible per session, 10 per page.

    The summary is the session title (numbered when *numbered*, so ``/resume <number>``
    keeps its mapping), with the current session's "Current" pill inline in the
    header. The body carries the session id as a copy pill on its own bullet
    line, the preview in real ``<i>`` italics — never the markdown ``_..._``
    markers, which the card path used to print literally — and Resume / Rename
    callback buttons. A divider plus the italic page indexation appear only
    when there is more than one page; a single page gets neither. Prev/Next
    page buttons carry a short-lived *page_token* the runner issued for this
    listing.
    """
    from .mtproto_rich import rich_button, rich_button_row, rich_page_footer

    visible = [row for row in rows if str(row.get("id") or "").strip()]
    total = len(visible)
    pages = max(1, (total + SESSIONS_PAGE_SIZE - 1) // SESSIONS_PAGE_SIZE)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * SESSIONS_PAGE_SIZE
    page_rows = visible[start:start + SESSIONS_PAGE_SIZE]

    parts = [f"<h1>{html.escape(title, quote=False)}</h1>"]
    if notice:
        parts.append(f"<p><i>{html.escape(notice, quote=False)}</i></p>")
    for offset, row in enumerate(page_rows):
        session_id = str(row.get("id") or "").strip()
        label = " ".join(str(row.get("title") or session_id).split()) or session_id
        summary = f"{start + offset + 1}. {label}" if numbered else label
        summary_html = html.escape(summary, quote=False)
        if current_session_id and session_id == str(current_session_id):
            summary_html += " " + rich_button("Current", kind="disabled")
        preview = str(row.get("preview") or "")[:SESSIONS_PREVIEW_CHARS]
        body = [f"<p>• session id: {rich_button(session_id, kind='copy', value=session_id)}</p>"]
        if preview:
            body.append(f"<p>• message: <i>{html.escape(preview, quote=False)}</i></p>")
        body.append(rich_button_row([
            rich_button("Resume", kind="callback", value=f"hm:resume:{session_id}"),
            rich_button("Rename", kind="callback", value=f"hm:rename:{session_id}"),
        ]))
        parts.append(
            f"<details><summary>{summary_html}</summary>"
            + "\n".join(body) + "</details>"
        )
    if pages > 1:
        parts.append("<hr>")
        parts.append(rich_page_footer(page, pages, total))
        pager: list[str] = []
        if page > 1 and page_token:
            pager.append(rich_button("‹ Previous", kind="callback",
                                     value=f"hm:sessions:page:{page_token}:{page - 1}",
                                     style="primary"))
        if page < pages and page_token:
            pager.append(rich_button("Next ›", kind="callback",
                                     value=f"hm:sessions:page:{page_token}:{page + 1}",
                                     style="primary"))
        if pager:
            parts.append(rich_button_row(pager))
    parts.append("<p><i>Resume with /resume &lt;session id&gt; or /resume &lt;number&gt;.</i></p>")
    parts.append("<p><i>More: /sessions all, /sessions full, /sessions search &lt;query&gt;.</i></p>")
    return "\n".join(parts)


def memories_listing_html(rows: Iterable[dict], *, page: int = 1, page_token: str | None = None,
                          title: str = "Memories") -> str:
    """Rich memories listing: one ``<details>`` collapsible per entry, 10 per page.

    No tables — entries can be long, and a table column would cut them to a
    fixed width. The summary is the entry number plus its first line (capped
    at MEMORIES_PREVIEW_CHARS); the body carries the FULL entry text with line
    breaks kept, plus its source section. A divider plus the italic page
    indexation appear only when there is more than one page; a single page
    gets neither. Prev/Next page buttons carry a short-lived *page_token* the
    runner issued for this listing.
    """
    from .mtproto_rich import rich_button, rich_button_row, rich_page_footer

    visible = [row for row in rows if str(row.get("text") or "").strip()]
    total = len(visible)
    pages = max(1, (total + MEMORIES_PAGE_SIZE - 1) // MEMORIES_PAGE_SIZE)
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * MEMORIES_PAGE_SIZE
    page_rows = visible[start:start + MEMORIES_PAGE_SIZE]

    parts = [f"<h1>{html.escape(title, quote=False)}</h1>"]
    section = None
    for offset, row in enumerate(page_rows):
        target = str(row.get("target") or "memory")
        if target != section:
            section = target
            section_label = "User profile" if target == "user" else "Memory"
            parts.append(f"<h2>{html.escape(section_label, quote=False)}</h2>")
        text = str(row.get("text") or "")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        preview = " ".join(first_line.split())[:MEMORIES_PREVIEW_CHARS]
        number = start + offset + 1
        summary = f"{number}. {preview}" if preview else f"{number}."
        body = html.escape(text, quote=False).replace("\n", "<br>")
        parts.append(
            f"<details><summary>{html.escape(summary, quote=False)}</summary>"
            f"<p>{body}</p>"
            f"<p>• source: {html.escape(section_label, quote=False)}</p>"
            "</details>"
        )
    if pages > 1:
        parts.append("<hr>")
        parts.append(rich_page_footer(page, pages, total))
        pager: list[str] = []
        if page > 1 and page_token:
            pager.append(rich_button("‹ Previous", kind="callback",
                                     value=f"hm:memories:page:{page_token}:{page - 1}",
                                     style="primary"))
        if page < pages and page_token:
            pager.append(rich_button("Next ›", kind="callback",
                                     value=f"hm:memories:page:{page_token}:{page + 1}",
                                     style="primary"))
        if pager:
            parts.append(rich_button_row(pager))
    parts.append("<p><i>Review staged writes with /memory pending.</i></p>")
    return "\n".join(parts)


def command_card_markup(*, buttons: Iterable[tuple[str, str]] = ()) -> Any | None:
    """One inline keyboard from explicit one-per-row *buttons* (or None when empty)."""
    from .mtproto_rich import inline_markup

    rows: list[list[tuple[str, str, None]]] = [
        [(label, payload, None)] for label, payload in buttons
    ]
    return inline_markup(rows) if rows else None


def _inline_markup(line: str) -> str:
    """Escape one line, then render ``**bold**``, ``_italic_`` and ```code``` spans as HTML.

    Code spans are stashed first so underscores/asterisks inside them stay
    literal (`` `session_id` `` never becomes italic).
    """
    safe = html.escape(line, quote=False)
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    safe = _CODE_RE.sub(_stash, safe)
    safe = _ITALIC_RE.sub(r"<i>\1</i>", safe)
    safe = _BOLD_RE.sub(r"<strong>\1</strong>", safe)
    for i, code in enumerate(codes):
        safe = safe.replace(f"\x00{i}\x00", f"<code>{code}</code>")
    return safe


def lines_to_rich_html(title: str, lines: Iterable[str]) -> str:
    """Render handler lines as one Telegram rich-HTML card.

    ``# ``/``## `` become headings, ``- `` lines group into a list, blank lines
    break paragraphs, and ```` ``` ```` fences become ``<pre>`` blocks.
    Anything else is a paragraph. Pictographic emoji are stripped (converted
    cards carry 0 emojis); no emoji is added here.
    """
    parts: list[str] = []
    if title:
        parts.append(f"<h1>{html.escape(title, quote=False)}</h1>")
    paragraph: list[str] = []
    list_items: list[str] = []
    fence_lines: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            parts.append("<p>" + "<br>".join(_inline_markup(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    def flush_fence() -> None:
        if fence_lines:
            parts.append("<pre>" + "\n".join(html.escape(line, quote=False) for line in fence_lines) + "</pre>")
            fence_lines.clear()

    for raw in lines:
        line = _EMOJI_RE.sub("", str(raw or "")).rstrip()
        if line.strip().startswith("```"):
            if in_fence:
                flush_fence()
                in_fence = False
            else:
                flush_paragraph()
                flush_list()
                in_fence = True
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if line.startswith("# "):
            flush_paragraph()
            flush_list()
            parts.append(f"<h1>{_inline_markup(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            flush_paragraph()
            flush_list()
            parts.append(f"<h2>{_inline_markup(line[3:].strip())}</h2>")
        elif line.startswith("- "):
            flush_paragraph()
            list_items.append(_inline_markup(line[2:].strip()))
        else:
            flush_list()
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    flush_fence()
    return "\n".join(parts)


def with_action_buttons(html_text: str, *, links: Iterable[tuple[str, str]] = (),
                        copies: Iterable[tuple[str, str]] = ()) -> str:
    """Append in-body URL / copy button rows to *html_text* (no buttons, no change)."""
    from .mtproto_rich import rich_button, rich_button_row

    rows: list[str] = []
    link_buttons = [rich_button(label, kind="url", value=url) for label, url in links]
    if link_buttons:
        rows.append(rich_button_row(link_buttons))
    copy_buttons = [rich_button(label, kind="copy", value=text) for label, text in copies]
    if copy_buttons:
        rows.append(rich_button_row(copy_buttons))
    if not rows:
        return html_text
    return html_text.rstrip() + "\n" + "\n".join(rows)
