"""Native Telegram Rich Message helpers for the Telethon/MTProto adapter.

All text delivery stays on MTProto: rich content is carried on Telethon's
``messages.SendMessageRequest`` / ``messages.EditMessageRequest`` constructors,
and colored callback buttons are ordinary MTProto ``KeyboardInlineButton``
constructors with ``KeyboardButtonStyle`` metadata.
"""
from __future__ import annotations

import re
from html import escape
from typing import Any, Iterable, Optional


# Markdown treats a lone newline as a soft break in many clients. Preserve
# visible line breaks without altering structured Markdown/HTML blocks.
_RICH_PROTECTED_REGION_RE = re.compile(
    r'(?:```[^\n]*\n[\s\S]*?```)'                       # fenced code block
    r'|(?:^[ \t]*\$\$[ \t]*\n[\s\S]*?^[ \t]*\$\$[ \t]*(?:\n|$))'  # display math
    r'|(?:^[ \t]*\\\[[ \t]*\n?[\s\S]*?^[ \t]*\\\][ \t]*(?:\n|$))'  # bracket math
    r'|(?:^[^\n]*\|[^\n]*\n'                            # table header row
    r'[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*'  # delimiter
    r'(?:\n[^\n]*\|[^\n]*)*)'                           # data rows
    r'|(?:^[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+[^\n]*'    # GFM task-list block
    r'(?:\n[ \t]*[-*+][ \t]+\[[ xX]\][ \t]+[^\n]*)*)'
    r'|(?:^[ \t]*(?:[-*+]|\d+[.)])[ \t]+[^\n]*'         # ordered/unordered lists
    r'(?:\n(?![ \t]*\n)(?:[ \t]*(?:[-*+]|\d+[.)])[ \t]+[^\n]*|[ \t]+[^\n]*))*)'
    r'|(?:<(details|table|ul|ol|pre|blockquote|p|tg-math-block|tg-collage|tg-slideshow)\b[^>]*>'
    r'[\s\S]*?</\1[ \t]*>)', re.IGNORECASE | re.MULTILINE)


def normalize_rich_markdown(markdown: str) -> str:
    """Turn lone Markdown newlines into hard breaks, leaving structured regions intact."""
    text = str(markdown)
    if not text or "\n" not in text:
        return text
    out: list[str] = []
    pos = 0
    for match in _RICH_PROTECTED_REGION_RE.finditer(text):
        out.append(re.sub(r'(?<!\n)\n(?!\n)', '  \n', text[pos:match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(re.sub(r'(?<!\n)\n(?!\n)', '  \n', text[pos:]))
    return "".join(out)


MAX_RICH_MESSAGE_CHARS = 32_768

_HTML_PARAGRAPH_RE = re.compile(r"<p\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</p\s*>", re.IGNORECASE)
_PAGE_INDEX_RE = re.compile(r"^Page\s+(?P<page>\d+)\s+of\s+(?P<pages>\d+)(?P<tail>.*?)\.?$", re.IGNORECASE)


def _normalized_page_indexation(value: str) -> str | None:
    """Return the canonical page-index text when a standalone footer is present."""
    from html import unescape

    plain = unescape(re.sub(r"<[^>]*>", "", str(value or "")))
    plain = plain.strip().strip("*_ ")
    match = _PAGE_INDEX_RE.fullmatch(plain)
    if not match:
        return None
    page = int(match.group("page"))
    pages = int(match.group("pages"))
    tail = match.group("tail").strip()
    total_match = re.fullmatch(
        r"(?:[•·]\s*)(?:(?:total)\s*)?(\d+)(?:\s+[A-Za-z][\w() -]*)?",
        tail,
        flags=re.IGNORECASE,
    ) if tail else None
    if total_match:
        total = int(total_match.group(1))
    elif pages == 1:
        total = 1
    else:
        return None
    return f"Page {page} of {pages} • total {total}"


def normalize_page_indexation(content: str, *, rich_format: str) -> str:
    """Normalize standalone Telegram page markers to one italic, canonical footer."""
    text = str(content or "")
    mode = str(rich_format or "markdown").strip().lower()
    if mode == "html":
        footers: list[str] = []

        def replace_paragraph(match: re.Match) -> str:
            footer = _normalized_page_indexation(match.group("body"))
            if footer is None:
                return match.group(0)
            footers.append(f"<p><i>{footer}</i></p>")
            return ""

        body = _HTML_PARAGRAPH_RE.sub(replace_paragraph, text).rstrip()
        return f"{body}\n{footers[-1]}" if footers else body

    lines = text.splitlines()
    footers = []
    body_lines = []
    for index, line in enumerate(lines):
        footer = _normalized_page_indexation(line)
        if footer is not None:
            footers.append(f"__{footer}__")
        else:
            body_lines.append(line)
    body = "\n".join(body_lines).rstrip()
    return f"{body}\n\n{footers[-1]}" if footers else body


def split_rich_message(content: str, max_length: int = MAX_RICH_MESSAGE_CHARS) -> list[str]:
    """Split long text on natural boundaries without cutting structured RichMessage blocks."""
    text = str(content or "")
    limit = int(max_length)
    if limit < 64:
        raise ValueError("RichMessage chunk limit must be at least 64 characters")
    if len(text) <= limit:
        return [text]

    # Reserve room for a small per-part indicator. The shared list/table/HTML/math
    # protection also covers complete fenced code blocks and nested list structures.
    reserve = 16
    budget = limit - reserve
    inline_math = re.compile(
        r"(?<!\\)(?<!\$)\$(?!\$)[^$\n]+?(?<!\\)\$(?!\$)"
        r"|\\\([^\n]+?\\\)|\\\[[\s\S]+?\\\]"
    )
    protected = [(match.start(), match.end()) for match in _RICH_PROTECTED_REGION_RE.finditer(text)]
    protected.extend((match.start(), match.end()) for match in inline_math.finditer(text))
    protected.sort()

    chunks: list[str] = []
    cursor = 0
    while len(text) - cursor > limit:
        window = text[cursor:cursor + budget]
        double_newline = window.rfind("\n\n")
        newline = window.rfind("\n")
        space = window.rfind(" ")
        if double_newline >= budget // 3:
            cut = double_newline
        elif newline >= budget // 2:
            cut = newline
        elif space >= budget // 2:
            cut = space
        else:
            cut = budget

        for _ in range(8):
            absolute_cut = cursor + cut
            overlaps = [(start, end) for start, end in protected if start < absolute_cut < end]
            if not overlaps:
                break
            start = min(item[0] for item in overlaps)
            end = max(item[1] for item in overlaps)
            if start > cursor:
                cut = start - cursor
            elif end - cursor <= budget:
                cut = end - cursor
            else:
                raise ValueError("A single structured Telegram RichMessage block exceeds the message limit")
        else:
            raise ValueError("Could not find a safe Telegram RichMessage split boundary")

        if cut <= 0:
            cut = budget
        chunk = text[cursor:cursor + cut].rstrip("\r\n")
        if not chunk:
            raise ValueError("Could not find a non-empty Telegram RichMessage chunk")
        chunks.append(chunk)
        cursor += cut
        # Drop only the line-break separator; preserve indentation at the start of a
        # nested list or continuation line so the following part remains well-formed.
        while cursor < len(text) and text[cursor] in "\r\n":
            cursor += 1
        if cursor < len(text) and text[cursor] == " " and cut == space:
            cursor += 1

    tail = text[cursor:]
    if tail:
        chunks.append(tail)
    count = len(chunks)
    parts = [f"{chunk}\n\n({index}/{count})" for index, chunk in enumerate(chunks, 1)]
    if any(len(part) > limit for part in parts):
        raise ValueError("Telegram RichMessage chunk exceeded the configured character limit")
    return parts


def input_rich_markdown(markdown: str, *, files: Any = None) -> Any:
    """Build the installed Telethon layer's native Markdown rich payload.

    ``files`` is an optional list of ``InputRichFilePhoto`` /
    ``InputRichFileDocument`` (see ``rich_media.bind_rich_media``) binding
    ``tg://...?id=`` references in the markup to resolved server media.
    """
    from telethon import types
    from .telegram_markdown import escape_rich_currency_dollars

    normalized = normalize_page_indexation(markdown, rich_format="markdown")
    # Telegram's rich Markdown pairs any two bare `$` as inline LaTeX, so a
    # currency amount would render as a garbled math span; escape currency
    # `$` before send (real math and code are kept verbatim by the escaper).
    return types.InputRichMessageMarkdown(
        normalize_rich_markdown(escape_rich_currency_dollars(normalized)),
        files=files)


_HTML_RICH_TAG_NAME = (
    r"(?:html|body|p|h[1-6]|ul|ol|table|details|blockquote|pre|tg-math|tg-math-block|"
    r"tg-collage|tg-slideshow|tg-button-row|tg-button|tg-thinking|"
    r"figure|aside|hr|b|strong|i|em|u|ins|s|strike|del|code|mark|"
    r"sub|sup|tg-spoiler|a)"
)
_HTML_RICH_START_RE = re.compile(rf"^\s*<{_HTML_RICH_TAG_NAME}\b", re.IGNORECASE)
_HTML_RICH_PAIRED_TAG_RE = re.compile(
    rf"<(?P<tag>{_HTML_RICH_TAG_NAME})\b[^>]*>[\s\S]*?</(?P=tag)\s*>", re.IGNORECASE,
)
_MARKDOWN_CODE_REGION_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]*`")


def _auto_detects_rich_html(text: str) -> bool:
    """Recognize supported HTML after prose, but not HTML-looking code examples."""
    if _HTML_RICH_START_RE.match(text):
        return True
    without_code = _MARKDOWN_CODE_REGION_RE.sub("", str(text))
    return _HTML_RICH_PAIRED_TAG_RE.search(without_code) is not None


def input_rich_html(html: str, *, files: Any = None) -> Any:
    """Build Telegram's native HTML RichMessage without parsing or text fallback.

    ``files`` is an optional list of ``InputRichFilePhoto`` /
    ``InputRichFileDocument`` (see ``rich_media.bind_rich_media``) binding
    ``tg://...?id=`` references in the markup to resolved server media.
    """
    from telethon import types

    return types.InputRichMessageHTML(normalize_page_indexation(str(html), rich_format="html"),
                                      files=files)


# --- In-body Rich HTML buttons (Bot API 10.3 RichMessageButton) -----------------
#
# Buttons inside the message body are written in Rich HTML as <tg-button>
# elements, grouped in <tg-button-row> rows. They render as the pill buttons
# the showcase cards use; they are NOT PageBlock TextButtons, which the
# server rejects in hand-built block payloads. The HTML string is sent
# verbatim inside InputRichMessageHTML, so the server parses it itself.
# Supported kinds here are the ones Hermes uses: url, callback, copy,
# profile (a url button to tg://user?id=...), and disabled. WebView buttons
# are deliberately absent: the server rejects them live with
# ButtonTypeInvalidError. Login/switch-inline variants are not built here.

_RICH_BUTTON_STYLES = frozenset({"primary", "success", "danger", "link"})
_RICH_BUTTON_KINDS = frozenset({"url", "callback", "copy", "profile", "disabled"})
_MAX_CALLBACK_DATA_BYTES = 64
_MAX_COPY_TEXT_CHARS = 256
_MAX_BUTTON_URL_CHARS = 2048


def rich_button(label: str, *, kind: str, value: str | None = None,
                style: str | None = None) -> str:
    """Render one in-body Rich HTML button, validating the server's limits.

    ``kind`` is ``url`` (http/https or tg:// value), ``callback`` (1–64 byte
    data value; the only kind that may use the ``link`` style), ``copy``
    (1–256 character text value), ``profile`` (numeric user-id value, rendered
    as a ``tg://user?id=`` url button), or ``disabled`` (no value; renders
    but does nothing). The label is plain text and is HTML-escaped.
    """
    from html import escape

    text = " ".join(str(label or "").split())
    if not text:
        raise ValueError("Rich button label must not be empty")
    kind = str(kind or "").strip().lower()
    if kind not in _RICH_BUTTON_KINDS:
        raise ValueError("Rich button kind must be url, callback, copy, profile, or disabled")
    if style is not None:
        style = str(style).strip().lower()
        if style not in _RICH_BUTTON_STYLES:
            raise ValueError("Rich button style must be primary, success, danger, or link")
        if style == "link" and kind != "callback":
            raise ValueError("The link button style is allowed only for callback buttons")
    style_attr = f' style="{style}"' if style else ""
    if kind == "disabled":
        if value not in (None, ""):
            raise ValueError("Disabled rich buttons take no value")
        return f'<tg-button type="disabled"{style_attr}>{escape(text)}</tg-button>'
    if kind == "callback":
        data = str(value or "")
        if not data or len(data.encode("utf-8")) > _MAX_CALLBACK_DATA_BYTES:
            raise ValueError("Callback rich buttons require data of 1–64 bytes")
        return (f'<tg-button type="callback_data"{style_attr} '
                f'data="{escape(data, quote=True)}">{escape(text)}</tg-button>')
    if kind == "copy":
        copy_text = str(value or "")
        if not copy_text or len(copy_text) > _MAX_COPY_TEXT_CHARS or "\x00" in copy_text:
            raise ValueError("Copy rich buttons require text of 1–256 characters")
        return (f'<tg-button type="copy_text"{style_attr} '
                f'text="{escape(copy_text, quote=True)}">{escape(text)}</tg-button>')
    if kind == "profile":
        user_id = str(value or "").strip()
        if not user_id.isdecimal():
            raise ValueError("Profile rich buttons require a numeric Telegram user ID")
        return (f'<tg-button type="url"{style_attr} '
                f'url="tg://user?id={user_id}">{escape(text)}</tg-button>')
    url = str(value or "").strip()
    if (not url or len(url) > _MAX_BUTTON_URL_CHARS
            or not (url.startswith(("http://", "https://", "tg://")))
            or any(ord(char) < 32 for char in url)):
        raise ValueError("URL rich buttons require an http://, https://, or tg:// URL")
    return (f'<tg-button type="url"{style_attr} '
            f'url="{escape(url, quote=True)}">{escape(text)}</tg-button>')


def rich_button_row(buttons: list[str], *, align: str | None = None) -> str:
    """Group 1–8 rendered rich buttons into one <tg-button-row> block."""
    if not isinstance(buttons, list) or not 1 <= len(buttons) <= 8:
        raise ValueError("A rich button row needs 1–8 buttons")
    if align is not None and str(align).strip().lower() not in {"left", "center", "right"}:
        raise ValueError("Rich button row align must be left, center, or right")
    align_attr = f' align="{str(align).strip().lower()}"' if align else ""
    return f"<tg-button-row{align_attr}>" + "".join(buttons) + "</tg-button-row>"


def rich_page_footer(page: int, pages: int, total: int) -> str:
    """Render the bottom page indexation as one italic HTML paragraph."""
    if (isinstance(page, bool) or isinstance(pages, bool) or isinstance(total, bool)
            or not isinstance(page, int) or not isinstance(pages, int) or not isinstance(total, int)
            or page < 1 or pages < page or total < 0):
        raise ValueError("Page indexation needs 1-based page/pages counts and a non-negative total")
    return f"<p><i>Page {page} of {pages} • total {total}</i></p>"


def autonomous_tag_block_row() -> Any:
    """The green ``autonomous`` tag as one native disabled-success button row.

    Native ``PageBlockButtonRow``, not Rich HTML: this is the tag row for
    blocks finals (explicit ``rich_format="blocks"``). An HTML final rides
    the tag as an HTML row instead (see ``autonomous_tag_html_row``). The
    row serializes inside ``InputRichMessage(blocks=...)`` (verified
    against the installed Telethon).
    """
    from telethon import types

    return types.PageBlockButtonRow([
        types.PageButton(
            text=types.TextPlain("autonomous"),
            type=types.InlineButtonTypeDisabled(),
            style=types.RichButtonStyle(bg_success=True),
        ),
    ])


def _rich_text_to_plain(node: Any) -> str:
    """Flatten a RichText node to its literal text (no tags)."""
    if node is None:
        return ""
    name = type(node).__name__
    if name == "TextEmpty":
        return ""
    if name == "TextPlain":
        return str(getattr(node, "text", "") or "")
    if name == "TextConcat":
        return "".join(_rich_text_to_plain(child)
                       for child in (getattr(node, "texts", None) or []))
    if name == "TextMath":
        return str(getattr(node, "source", "") or "")
    inner = getattr(node, "text", None)
    if isinstance(inner, str):
        return inner
    if inner is not None:
        return _rich_text_to_plain(inner)
    return ""


def _html_text_math(node: Any) -> str | None:
    return f"<tg-math>{escape(str(getattr(node, 'source', '') or ''))}</tg-math>"


def _html_text_url(node: Any) -> str | None:
    inner = rich_text_to_html(getattr(node, "text", None))
    if inner is None:
        return None
    url = str(getattr(node, "url", "") or "").strip()
    if not url:
        return inner
    return f'<a href="{escape(url, quote=True)}">{inner}</a>'


def _html_text_email(node: Any) -> str | None:
    inner = rich_text_to_html(getattr(node, "text", None))
    if inner is None:
        return None
    email = str(getattr(node, "email", "") or "").strip()
    if not email:
        return inner
    return f'<a href="mailto:{escape(email, quote=True)}">{inner}</a>'


def _html_text_phone(node: Any) -> str | None:
    inner = rich_text_to_html(getattr(node, "text", None))
    if inner is None:
        return None
    phone = str(getattr(node, "phone", "") or "").strip()
    if not phone:
        return inner
    return f'<a href="tel:{escape(phone, quote=True)}">{inner}</a>'


def _html_text_mention(node: Any) -> str | None:
    plain = _rich_text_to_plain(getattr(node, "text", None))
    if not plain:
        return ""
    username = plain[1:] if plain.startswith("@") else ""
    if username and re.fullmatch(r"[A-Za-z0-9_]+", username):
        return (f'<a href="https://t.me/{escape(username, quote=True)}">'
                f"{escape(plain)}</a>")
    return escape(plain)


def _html_text_mention_name(node: Any) -> str | None:
    inner = rich_text_to_html(getattr(node, "text", None))
    if inner is None:
        return None
    try:
        user_id = int(getattr(node, "user_id", 0))
    except (TypeError, ValueError):
        return inner
    if user_id <= 0:
        return inner
    return f'<a href="tg://user?id={user_id}">{inner}</a>'


def _html_text_plain_tag(node: Any) -> str | None:
    # Hashtags, cashtags and bot commands: no HTML tag carries them, so the
    # text rides plain (readable, not clickable).
    return escape(_rich_text_to_plain(getattr(node, "text", None)))


def _html_text_button(node: Any) -> str | None:
    # Only a copy button survives as HTML: the Markdown path makes one for
    # a short inline code span. Any other button type falls back to blocks
    # rather than silently dropping the button.
    button_type = getattr(node, "type", None)
    if type(button_type).__name__ != "InlineButtonTypeCopy":
        return None
    copy_text = str(getattr(button_type, "copy_text", "") or "")
    label = _rich_text_to_plain(getattr(node, "text", None)).strip()
    if not copy_text or not label:
        return None
    try:
        return rich_button(label, kind="copy", value=copy_text)
    except ValueError:
        return None


_RICH_TEXT_INLINE_TAGS: dict[str, str] = {
    "TextBold": "b",
    "TextItalic": "i",
    "TextUnderline": "u",
    "TextStrike": "s",
    "TextFixed": "code",
    "TextSubscript": "sub",
    "TextSuperscript": "sup",
    "TextMarked": "mark",
    "TextSpoiler": "tg-spoiler",
}

_BLOCK_TEXT_TAGS: dict[str, str] = {
    "PageBlockParagraph": "p",
    "PageBlockFooter": "p",
    "PageBlockHeading1": "h1",
    "PageBlockHeading2": "h2",
    "PageBlockHeading3": "h3",
    "PageBlockHeading4": "h4",
    "PageBlockHeading5": "h5",
    "PageBlockHeading6": "h6",
    "PageBlockBlockquote": "blockquote",
    "PageBlockPullquote": "blockquote",
}


_RICH_TEXT_HTML_HANDLERS: dict[str, Any] = {
    "TextMath": _html_text_math,
    "TextUrl": _html_text_url,
    "TextEmail": _html_text_email,
    "TextPhone": _html_text_phone,
    "TextMention": _html_text_mention,
    "TextMentionName": _html_text_mention_name,
    "TextHashtag": _html_text_plain_tag,
    "TextCashtag": _html_text_plain_tag,
    "TextBotCommand": _html_text_plain_tag,
    "TextButton": _html_text_button,
}


def rich_text_to_html(text: Any) -> str | None:
    """Render one RichText node to Rich HTML, or None when it cannot be expressed.

    Unknown nodes return None so the caller falls back to the blocks route
    rather than dropping content.
    """
    if text is None:
        return ""
    name = type(text).__name__
    if name == "TextEmpty":
        return ""
    if name == "TextPlain":
        return escape(str(getattr(text, "text", "") or ""))
    if name == "TextConcat":
        parts = [rich_text_to_html(child) for child in (getattr(text, "texts", None) or [])]
        if any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
    tag = _RICH_TEXT_INLINE_TAGS.get(name)
    if tag is not None:
        inner = rich_text_to_html(getattr(text, "text", None))
        if inner is None:
            return None
        return f"<{tag}>{inner}</{tag}>"
    handler = _RICH_TEXT_HTML_HANDLERS.get(name)
    if handler is not None:
        return handler(text)
    return None


def _preformatted_to_html(block: Any) -> str | None:
    text = rich_text_to_html(getattr(block, "text", None))
    if text is None:
        return None
    language = str(getattr(block, "language", "") or "").strip()
    lang_attr = f' language="{escape(language, quote=True)}"' if language else ""
    return f"<pre{lang_attr}>{text}</pre>"


def _math_block_to_html(block: Any) -> str | None:
    return f"<tg-math-block>{escape(str(getattr(block, 'source', '') or ''))}</tg-math-block>"


def _table_to_html(block: Any) -> str | None:
    out = ["<table>"]
    for row in getattr(block, "rows", None) or []:
        out.append("<tr>")
        for cell in getattr(row, "cells", None) or []:
            inner = rich_text_to_html(getattr(cell, "text", None))
            if inner is None:
                return None
            tag = "th" if getattr(cell, "header", False) else "td"
            attrs = ""
            if getattr(cell, "align_center", False):
                attrs += ' align="center"'
            elif getattr(cell, "align_right", False):
                attrs += ' align="right"'
            colspan = int(getattr(cell, "colspan", 0) or 0)
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            rowspan = int(getattr(cell, "rowspan", 0) or 0)
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            out.append(f"<{tag}{attrs}>{inner}</{tag}>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _list_to_html(block: Any) -> str | None:
    ordered = type(block).__name__ == "PageBlockOrderedList"
    rendered = []
    for item in getattr(block, "items", None) or []:
        # A checklist item has no HTML form: fall back to the blocks route.
        if getattr(item, "checkbox", None) is not None:
            return None
        item_name = type(item).__name__
        if item_name in ("PageListItemText", "PageListOrderedItemText"):
            inner = rich_text_to_html(getattr(item, "text", None))
            if inner is None:
                return None
            rendered.append(f"<li>{inner}</li>")
        elif item_name in ("PageListItemBlocks", "PageListOrderedItemBlocks"):
            item_blocks = getattr(item, "blocks", None) or []
            parts = []
            for index, sub in enumerate(item_blocks):
                if index == 0 and type(sub).__name__ in ("PageBlockParagraph", "PageBlockFooter"):
                    # A list item's leading text renders inline (no <p>
                    # wrapper): the nested list or other block follows after
                    # it, and a <p> inside the <li> would render as a gap.
                    inner = rich_text_to_html(getattr(sub, "text", None))
                    if inner is None:
                        return None
                    parts.append(inner)
                    continue
                html = _block_to_html(sub)
                if html is None:
                    return None
                parts.append(html)
            rendered.append(f"<li>{''.join(parts)}</li>")
        else:
            return None
    if ordered:
        start = int(getattr(block, "start", 1) or 1)
        start_attr = f' start="{start}"' if start != 1 else ""
        return f"<ol{start_attr}>" + "".join(rendered) + "</ol>"
    return "<ul>" + "".join(rendered) + "</ul>"


def _details_to_html(block: Any) -> str | None:
    title = rich_text_to_html(getattr(block, "title", None))
    if title is None:
        return None
    body = blocks_to_rich_html(getattr(block, "blocks", None) or [])
    if body is None:
        return None
    open_attr = " open" if getattr(block, "open", None) else ""
    return f"<details{open_attr}><summary>{title}</summary>{body}</details>"


_BLOCK_HTML_HANDLERS: dict[str, Any] = {
    "PageBlockPreformatted": _preformatted_to_html,
    "PageBlockTable": _table_to_html,
    "PageBlockList": _list_to_html,
    "PageBlockOrderedList": _list_to_html,
    "PageBlockDetails": _details_to_html,
    "PageBlockMath": _math_block_to_html,
}


def _block_to_html(block: Any) -> str | None:
    """Render one PageBlock to Rich HTML, or None when it has no HTML form."""
    if block is None:
        return ""
    name = type(block).__name__
    tag = _BLOCK_TEXT_TAGS.get(name)
    if tag is not None:
        inner = rich_text_to_html(getattr(block, "text", None))
        return None if inner is None else f"<{tag}>{inner}</{tag}>"
    if name == "PageBlockDivider":
        return "<hr>"
    handler = _BLOCK_HTML_HANDLERS.get(name)
    if handler is not None:
        return handler(block)
    return None


def blocks_to_rich_html(blocks: list[Any]) -> str | None:
    """Render built PageBlock* nodes to Rich HTML, or None when any block
    cannot be expressed in HTML (media, maps, galleries, checklist items,
    unknown blocks)."""
    parts = []
    for block in blocks or []:
        html = _block_to_html(block)
        if html is None:
            return None
        parts.append(html)
    return "".join(parts)


def autonomous_tag_html_row() -> str:
    """The green ``autonomous`` tag as an in-body Rich HTML button row.

    For HTML finals only: the row rides as text atop the HTML payload,
    the same shape the enable card already sends.
    """
    return rich_button_row([rich_button("autonomous", kind="disabled", style="success")])


def _input_rich_blocks(markdown: str, *, autonomous_tag: bool = False) -> Any:
    from telethon import types
    from .rich_blocks import (
        _markdown_segment_to_blocks,
        _validate_native_blocks,
        footnote_blocks,
        split_footnotes,
    )

    normalized = normalize_page_indexation(markdown, rich_format="markdown")
    normalized, numbered = split_footnotes(normalized)
    blocks = _markdown_segment_to_blocks(normalized)
    if autonomous_tag:
        blocks.insert(0, autonomous_tag_block_row())
    blocks.extend(footnote_blocks(numbered))
    _validate_native_blocks(blocks)
    return types.InputRichMessage(blocks=blocks)


def input_rich_message(content: str, *, rich_format: str = "auto",
                       files: Any = None, autonomous_tag: bool = False) -> Any:
    """Select native MTProto RichMessage HTML, Markdown, or explicit math blocks.

    ``files`` binds ``tg://...?id=`` references in HTML/Markdown bodies to
    resolved server media (see ``rich_media.bind_rich_media``); it is
    ignored for the blocks route, which carries media as ``photos`` /
    ``documents`` lists instead.
    Formula-bearing Markdown becomes ``InputRichMessage(blocks=...)`` with
    ``PageBlockMath``/``TextMath``. Plain Markdown and complete rich-HTML messages
    retain their dedicated Telethon constructors. An explicit
    ``rich_format="markdown"`` is honored verbatim: the caller asked for the
    Markdown payload, so the copyable-code auto-route to blocks applies only
    when ``rich_format`` is ``"auto"``. RPC failures are not retried as
    ordinary text.

    ``autonomous_tag`` puts the green ``autonomous`` tag atop the message:
    an HTML row prepended for HTML finals, a native button row at index 0
    for blocks finals. An explicit-Markdown final cannot carry a native
    row, so it stays untagged rather than flipping format — the tag never
    changes what the caller asked the payload to be.

    Footer badges are not a parameter here: the footer itself carries
    them as literal ``<tg-button-row>`` text (see
    ``gateway.runtime_footer.build_footer_html``), so a badged final is
    already complete Rich HTML by the time it reaches this function, and
    the HTML branch below sends it verbatim. Badges are never a keyboard.
    """
    from telethon import types

    text = str(content or "")
    if len(text) > MAX_RICH_MESSAGE_CHARS:
        raise ValueError(f"Telegram RichMessages cannot exceed {MAX_RICH_MESSAGE_CHARS} characters")
    mode = str(rich_format or "auto").strip().lower()
    if mode not in {"auto", "markdown", "html", "blocks"}:
        raise ValueError("rich_format must be auto, markdown, html, or blocks")
    if mode == "html" or (mode == "auto" and _auto_detects_rich_html(text)):
        if autonomous_tag:
            tag_row = autonomous_tag_html_row()
            if len(text) + len(tag_row) + 1 <= MAX_RICH_MESSAGE_CHARS:
                text = tag_row + "\n" + text
        return input_rich_html(text, files=files)
    if mode == "blocks":
        return _input_rich_blocks(text, autonomous_tag=autonomous_tag)
    if mode == "markdown":
        # Explicit Markdown is honored verbatim. The copyable-code auto-route
        # to blocks is an "auto" behavior only: a caller that names Markdown
        # (e.g. the search-progress card, whose inline `/stop` / model code
        # spans must not flip it to blocks) gets the Markdown payload.
        return input_rich_markdown(text, files=files)
    from .rich_blocks import markdown_has_copyable_code, native_math_blocks

    rich_blocks = native_math_blocks(text)
    if rich_blocks is not None:
        if autonomous_tag:
            rich_blocks.blocks.insert(0, autonomous_tag_block_row())
        return rich_blocks
    if markdown_has_copyable_code(text):
        return _input_rich_blocks(text, autonomous_tag=autonomous_tag)
    if autonomous_tag:
        # Plain auto Markdown has no native row carrier as a Markdown
        # payload, so it rides the blocks route (the same conversion the
        # copyable-code auto-route already applies) with the tag row at 0.
        return _input_rich_blocks(text, autonomous_tag=autonomous_tag)
    return input_rich_markdown(text)


def _validate_entity_spans(text: str, entities: Iterable[Any]) -> None:
    """Reject entity offsets outside text or inside a UTF-16 surrogate pair."""
    encoded = text.encode("utf-16-le")
    utf16_length = len(encoded) // 2
    for entity in entities:
        offset, length = int(entity.offset), int(entity.length)
        if offset < 0 or length <= 0 or offset + length > utf16_length:
            raise ValueError("Telegram entity span is outside the message text")
        for boundary in (offset, offset + length):
            try:
                encoded[:boundary * 2].decode("utf-16-le")
            except UnicodeDecodeError as exc:
                raise ValueError("Telegram entity span splits a UTF-16 surrogate pair") from exc


def markdown_entities(markdown: str) -> tuple[str, list[Any]]:
    """Parse Telegram Markdown to plain text and validated UTF-16 entities."""
    from telethon.extensions import markdown as telethon_markdown

    text, entities = telethon_markdown.parse(str(markdown))
    _validate_entity_spans(text, entities)
    return text, entities


def send_markdown_entities_request(peer: Any, markdown: str, *,
                                   reply_to: Optional[int] = None,
                                   top_msg_id: Optional[int] = None,
                                   no_webpage: Optional[bool] = None,
                                   reply_markup: Any = None) -> Any:
    """Create a standard MTProto sendMessage with explicit, parser-built entities."""
    from telethon import functions, types

    text, entities = markdown_entities(markdown)
    from .topic_delivery import topic_reply_header
    reply_header = topic_reply_header(reply_to, top_msg_id)
    return functions.messages.SendMessageRequest(
        peer=peer, message=text, entities=entities or None, no_webpage=no_webpage,
        reply_to=reply_header, reply_markup=reply_markup,
    )


def edit_markdown_entities_request(peer: Any, message_id: int, markdown: str, *,
                                  no_webpage: Optional[bool] = None,
                                  reply_markup: Any = None) -> Any:
    """Create a standard MTProto editMessage with explicit Telegram entities."""
    from telethon import functions

    text, entities = markdown_entities(markdown)
    return functions.messages.EditMessageRequest(
        peer=peer, id=int(message_id), message=text, entities=entities or None,
        no_webpage=no_webpage, reply_markup=reply_markup,
    )


def inline_markup(rows: Optional[Iterable[Iterable[tuple[str, str, Optional[str]]]]]) -> Any:
    """Build an MTProto inline keyboard.

    Each button tuple is ``(label, callback_payload, style)``; style is one of
    Telethon's ``primary`` (blue), ``success`` (green), ``danger`` (red), or
    ``None``. Telethon validates callback payload length and style values.
    """
    materialized = [list(row) for row in (rows or []) if row]
    if not materialized:
        return None

    from telethon import Button, types

    return types.ReplyInlineMarkup(rows=[
        types.KeyboardButtonRow(buttons=[
            Button.inline(label, data=payload, style=style)
            for label, payload, style in row
        ])
        for row in materialized
    ])


def _rich_request_payload(content: Any, rich_format: str,
                          autonomous_tag: bool = False) -> tuple[str, Any]:
    """Return the RichMessage and the companion text required by MTProto requests."""
    from telethon import types

    if isinstance(content, types.InputRichMessage):
        rich_message = content
        if autonomous_tag:
            rich_message.blocks.insert(0, autonomous_tag_block_row())
        from .start_ui import _rich_text
        rich_source = "\n".join(
            _rich_text(block.text, []) for block in rich_message.blocks
            if isinstance(block, types.PageBlockParagraph)
        )
        return rich_source or " ", rich_message

    rich_message = input_rich_message(content, rich_format=rich_format,
                                      autonomous_tag=autonomous_tag)
    rich_source = (getattr(rich_message, "markdown", None)
                   or getattr(rich_message, "html", None) or str(content))
    return rich_source, rich_message


def send_request(peer: Any, markdown: Any, *, reply_to: Optional[int] = None,
                 top_msg_id: Optional[int] = None,
                 no_webpage: Optional[bool] = None, reply_markup: Any = None,
                 silent: Optional[bool] = None,
                 rich_format: str = "auto", autonomous_tag: bool = False) -> Any:
    """Create a ``messages.sendMessage`` request with native Rich Message text."""
    from telethon import functions

    rich_source, rich_message = _rich_request_payload(
        markdown, rich_format, autonomous_tag)
    from .topic_delivery import topic_reply_header
    reply_header = topic_reply_header(reply_to, top_msg_id)
    return functions.messages.SendMessageRequest(
        peer=peer,
        # The TL schema still requires the ordinary message field alongside the
        # native rich payload; it is not a second transport or retry fallback.
        message=rich_source,
        no_webpage=no_webpage,
        silent=silent,
        reply_to=reply_header,
        reply_markup=reply_markup,
        rich_message=rich_message,
    )


def edit_request(peer: Any, message_id: int, markdown: Any, *,
                 no_webpage: Optional[bool] = None, reply_markup: Any = None,
                 rich_format: str = "auto") -> Any:
    """Create a ``messages.editMessage`` request with native Rich Message text."""
    from telethon import functions

    rich_source, rich_message = _rich_request_payload(markdown, rich_format)
    return functions.messages.EditMessageRequest(
        peer=peer,
        id=int(message_id),
        message=rich_source,
        no_webpage=no_webpage,
        reply_markup=reply_markup,
        rich_message=rich_message,
    )


def guest_result(query_id: int, markdown: str, *, reply_markup: Any = None) -> Any:
    """Create a native MTProto Guest Mode inline result containing a Rich Message."""
    from telethon import functions, types

    result = types.InputBotInlineResult(
        id=f"hermes-guest-{int(query_id)}",
        type="article",
        title="Hermes",
        description="Guest chat response",
        send_message=types.InputBotInlineMessageRichMessage(
            rich_message=input_rich_message(markdown),
            reply_markup=reply_markup,
        ),
    )
    return functions.messages.SetBotGuestChatResultRequest(query_id=int(query_id), result=result)


def edit_guest_result(inline_message_id: Any, markdown: str) -> Any:
    """Edit a delivered guest-result inline message with a new Rich Message.

    ``SetBotGuestChatResultRequest`` returns an ``InputBotInlineMessageID``
    for the message it delivered; that id is what this edit takes. The
    guest result is the Thinking message, so the answer replaces it in
    place instead of arriving as a second message.
    """
    from telethon import functions

    return functions.messages.EditInlineBotMessageRequest(
        id=inline_message_id,
        rich_message=input_rich_message(markdown),
    )


# Compatibility entry points for callers that need display text and entity
# metadata rather than a native RichMessage TL constructor.
def detect_rich_format(content: str) -> str:
    """Classify content as HTML, Markdown, or plain text."""
    text = str(content or "")
    if _HTML_RICH_START_RE.match(text):
        return "html"
    if re.search(r"(?m)^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s)|\*\*|(?<!\\)[*_`]", text):
        return "markdown"
    return "plain"


def parse_mode_for_text(text: str) -> str:
    """Use HTML only when Telethon recognizes actual Telegram HTML entities.

    Command placeholders such as ``<name>`` and ``<json|md|html>`` are ordinary
    text, not HTML tags. Mask Markdown code spans/fences before probing so a
    literal HTML example inside code does not switch the whole message to HTML.
    """
    probe = re.sub(r"```.*?```", "", str(text or ""), flags=re.DOTALL)
    probe = re.sub(r"(?<!`)`+[^`\n]*`+(?!`)", "", probe)
    try:
        from telethon.extensions import html as telethon_html
        _, entities = telethon_html.parse(probe)
    except Exception:
        return "markdown"
    return "html" if entities else "markdown"


def _html_to_telegram_markup(source: str) -> str:
    """Convert structural HTML to plain layout and safe Telegram inline markup."""
    from html import escape
    from html.parser import HTMLParser

    allowed = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "blockquote"}
    structural = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "div", "li",
                  "tr", "table", "thead", "tbody", "ul", "ol", "section"}

    class Converter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self.open_tags: list[str] = []
            self.cells = 0

        def handle_starttag(self, tag, attrs):
            tag = tag.lower()
            if tag in allowed:
                self.parts.append(f"<{tag}>")
                self.open_tags.append(tag)
            elif tag == "a":
                href = dict(attrs).get("href", "")
                if href.lower().startswith(("http://", "https://", "mailto:")):
                    self.parts.append(f'<a href="{escape(href, quote=True)}">')
                    self.open_tags.append(tag)
            elif tag in {"td", "th"}:
                if self.cells:
                    self.parts.append(" | ")
                self.cells += 1
            elif tag == "br":
                self.parts.append("\n")

        def handle_endtag(self, tag):
            tag = tag.lower()
            if tag in allowed or tag == "a":
                if tag in self.open_tags:
                    index = len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)
                    close = self.open_tags[index:]
                    del self.open_tags[index:]
                    self.parts.extend(f"</{name}>" for name in reversed(close))
            elif tag in {"td", "th"}:
                self.cells = max(0, self.cells - 1)
            elif tag == "tr" or tag in structural:
                self.parts.append("\n")

        def handle_data(self, data):
            self.parts.append(escape(data, quote=False))

    parser = Converter()
    parser.feed(str(source))
    parser.close()
    for tag in reversed(parser.open_tags):
        parser.parts.append(f"</{tag}>")
    return re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(parser.parts)).strip()


def text_and_entities(content: str, rich_format: str = "auto") -> tuple[str, list[Any]]:
    """Return safe visible text and canonical Telethon entities for rich content."""
    from html import unescape

    mode = str(rich_format or "auto").strip().lower()
    if mode == "auto":
        mode = detect_rich_format(content)
    if mode == "html":
        from telethon.extensions import html as telethon_html
        text, entities = telethon_html.parse(_html_to_telegram_markup(content))
        _validate_entity_spans(text, entities)
        return text, entities
    if mode == "markdown":
        return markdown_entities(content)
    if mode != "plain":
        raise ValueError("rich_format must be auto, markdown, html, or plain")
    return unescape(str(content or "")), []
