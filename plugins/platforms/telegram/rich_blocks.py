"""Native MTProto rich-block conversion for Telegram math and code messages.

Telegram's Rich Message TL schema accepts ``PageBlockMath(source=...)`` for
LaTeX blocks, and RichText nodes such as ``TextMath`` for inline expressions.
It also supports native copy-action buttons for code. Ordinary messages continue
through Telegram's native Markdown/HTML RichMessage constructors.
"""
from __future__ import annotations

import re
from typing import Any


_DISPLAY_MATH_RE = re.compile(
    r"(?P<fenced>^```(?:math|latex|tex)[ \t]*\r?\n(?P<fenced_source>[\s\S]*?)^```[ \t]*(?:\r?\n|$))"
    r"|(?P<code>^```[^\n]*\r?\n[\s\S]*?^```[ \t]*(?:\r?\n|$))"
    r"|(?P<dollar>\$\$(?P<dollar_source>[\s\S]*?)\$\$)"
    r"|(?P<bracket>\\\[(?P<bracket_source>[\s\S]*?)\\\])",
    re.IGNORECASE | re.MULTILINE,
)
_INLINE_MATH_RE = re.compile(
    r"(?<!\\)(?<!\$)\$(?!\$)(?P<dollar>[^$\n]+?)(?<!\\)\$(?!\$)"
    r"|\\\((?P<paren>.+?)\\\)",
)
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d+[.)])[ \t]+(?P<body>.*)$")
_TABLE_DELIMITER_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_FENCE_RE = re.compile(r"^[ \t]*```([^\n]*)$")
_DIVIDER_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
# Inline constructs Telethon's Markdown parser silently drops, handled by the
# pre-pass in _rich_text_from_markdown: ==marked==, ||spoiler||, *italic*
# (single star — Telethon reads * as bold), <sub>/<sup>.
_INLINE_EXTRA_RE = re.compile(
    r"==(?P<marked>.+?)=="
    r"|\|\|(?P<spoiler>.+?)\|\|"
    r"|(?<!\*)\*(?!\*)(?P<italic>.+?)(?<!\*)\*(?!\*)"
    r"|<sub>(?P<sub>.+?)</sub>"
    r"|<sup>(?P<sup>.+?)</sup>",
    re.DOTALL,
)
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([A-Za-z0-9_-]+)\]:[ \t]*(.*)$")
_FOOTNOTE_REF_RE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
_SUPERSCRIPT_DIGITS = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
# Block-level HTML constructs from the rich-messages spec that have no
# Markdown form: <details>, <aside> (pullquote), <tg-map>, <tg-collage>,
# <tg-slideshow>, <img> / ![alt](tg://...?id=N) with numeric ids.
_DETAILS_OPEN_RE = re.compile(r"^[ \t]*<details(\s+open)?[ \t]*>[ \t]*$")
_SUMMARY_RE = re.compile(r"^[ \t]*<summary>(.*?)</summary>[ \t]*$")
_ASIDE_OPEN_RE = re.compile(r"^[ \t]*<aside[ \t]*>[ \t]*$")
_SINGLE_ASIDE_RE = re.compile(r"^[ \t]*<aside[ \t]*>(.*?)</aside>[ \t]*$")
_TG_MAP_RE = re.compile(
    r"^[ \t]*<tg-map\s+lat=\"(?P<lat>-?\d+(?:\.\d+)?)\"\s+long=\"(?P<long>-?\d+(?:\.\d+)?)\""
    r"(?:\s+zoom=\"(?P<zoom>\d+)\")?(?:\s+width=\"(?P<width>\d+)\")?"
    r"(?:\s+height=\"(?P<height>\d+)\")?[ \t]*/?>[ \t]*$")
_GALLERY_OPEN_RE = re.compile(r"^[ \t]*<tg-(collage|slideshow)[ \t]*>[ \t]*$")
_IMG_RE = re.compile(
    r"^[ \t]*<img\s+src=\"tg://(?P<scheme>photo|video|audio|document)\?id=(?P<id>\d+)\""
    r"(?:\s+alt=\"(?P<alt>[^\"]*)\")?[ \t]*/?>[ \t]*$")
_MD_IMG_RE = re.compile(
    r"^[ \t]*!\[(?P<alt>[^\]]*)\]\(tg://(?P<scheme>photo|video|audio|document)\?id=(?P<id>\d+)\)[ \t]*$")
MAX_NATIVE_BLOCKS = 500
MAX_NESTING_DEPTH = 16
MAX_TABLE_COLUMNS = 20
MAX_CODE_COPY_CHARS = 100
MAX_MEDIA = 50
MAX_RICH_TEXT_CHARS = 32768


def _types():
    from telethon import types
    return types


def _validate_native_blocks(blocks: list[Any]) -> None:
    """Reject explicit block payloads outside Telegram's documented native limits.

    Counts blocks (500), literal characters across all text leaves (32,768),
    media blocks (50), and nesting depth (16) for containers
    (details / list items with blocks / collage / slideshow / embed posts).
    """
    types = _types()
    stack: list[tuple[Any, int]] = [(node, 1) for node in blocks]
    count = chars = media = 0
    while stack:
        node, depth = stack.pop()
        if isinstance(node, (list, tuple)):
            stack.extend((item, depth) for item in node)
            continue
        if not isinstance(node, types.TLObject):
            continue
        name = type(node).__name__
        if (name.startswith(("PageBlock", "PageListItem", "PageListOrderedItem"))
                or name == "PageTableRow"):
            count += 1
            if count > MAX_NATIVE_BLOCKS:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_NATIVE_BLOCKS} blocks")
        if name in ("PageBlockPhoto", "PageBlockVideo", "PageBlockAudio", "PageBlockDocument"):
            media += 1
            if media > MAX_MEDIA:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_MEDIA} media blocks")
        if name == "PageBlockEmbed" and getattr(node, "poster_photo_id", None):
            media += 1
            if media > MAX_MEDIA:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_MEDIA} media blocks")
        if name == "TextPlain":
            chars += len(str(getattr(node, "text", "")))
            if chars > MAX_RICH_TEXT_CHARS:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_RICH_TEXT_CHARS} characters")
        if name == "PageBlockMath":
            chars += len(str(getattr(node, "source", "")))
            if chars > MAX_RICH_TEXT_CHARS:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_RICH_TEXT_CHARS} characters")
        if depth > MAX_NESTING_DEPTH:
            raise ValueError(f"Telegram RichMessages cannot exceed {MAX_NESTING_DEPTH} nested levels")
        child_depth = depth + 1 if name in (
            "PageBlockDetails", "PageListItemBlocks", "PageListOrderedItemBlocks",
            "PageBlockCollage", "PageBlockSlideshow", "PageBlockEmbedPost") else depth
        for value in vars(node).values():
            if isinstance(value, (list, tuple)):
                stack.extend((item, child_depth) for item in value)
            elif isinstance(value, types.TLObject):
                stack.append((value, child_depth))


def _utf16_offsets(text: str) -> dict[int, int]:
    """Map valid UTF-16 entity offsets back to Python code-point indexes."""
    offsets = {0: 0}
    units = 0
    for index, char in enumerate(text, 1):
        units += len(char.encode("utf-16-le")) // 2
        offsets[units] = index
    return offsets


def _wrap_rich_text(entity: Any, inner: Any) -> Any:
    types = _types()
    name = type(entity).__name__
    if name == "MessageEntityCode":
        copy_text = _rich_text_plain(inner)
        if len(copy_text) > 100:
            return types.TextFixed(text=inner)
        return types.TextButton(
            text=types.TextFixed(text=inner),
            type=types.InlineButtonTypeCopy(copy_text=copy_text),
            style=types.RichButtonStyle(link=True),
        )
    wrappers = {
        "MessageEntityBold": types.TextBold,
        "MessageEntityItalic": types.TextItalic,
        "MessageEntityUnderline": types.TextUnderline,
        "MessageEntityStrike": types.TextStrike,
        "MessageEntityPre": types.TextFixed,
        "MessageEntitySpoiler": types.TextSpoiler,
        "MessageEntityMention": types.TextMention,
        "MessageEntityHashtag": types.TextHashtag,
        "MessageEntityCashtag": types.TextCashtag,
        "MessageEntityBotCommand": types.TextBotCommand,
    }
    wrapper = wrappers.get(name)
    if wrapper is not None:
        return wrapper(text=inner)
    if name == "MessageEntityTextUrl":
        url = str(getattr(entity, "url", ""))
        # Telethon's Markdown parser folds mailto:/tel: links into TextUrl;
        # keep the Email/Phone distinction the HTML form carries.
        if url.startswith("mailto:"):
            return types.TextEmail(text=inner, email=url[len("mailto:"):])
        if url.startswith("tel:"):
            return types.TextPhone(text=inner, phone=url[len("tel:"):])
        return types.TextUrl(text=inner, url=url, webpage_id=0)
    if name == "MessageEntityUrl":
        plain = _rich_text_plain(inner)
        if plain.startswith("mailto:"):
            return types.TextEmail(text=inner, email=plain[len("mailto:"):])
        if plain.startswith("tel:"):
            return types.TextPhone(text=inner, phone=plain[len("tel:"):])
        return types.TextUrl(text=inner, url=plain, webpage_id=0)
    if name == "MessageEntityEmail":
        return types.TextEmail(text=inner, email=_rich_text_plain(inner))
    if name == "MessageEntityPhone":
        return types.TextPhone(text=inner, phone=_rich_text_plain(inner))
    if name == "MessageEntityMentionName":
        return types.TextMentionName(text=inner, user_id=int(entity.user_id))
    return inner


def _rich_text_plain(text: Any) -> str:
    """Extract best-effort plain text from a Text* node for URL-like entities."""
    if isinstance(text, str):
        return text
    name = type(text).__name__
    if name == "TextPlain":
        return str(text.text)
    if name == "TextConcat":
        return "".join(_rich_text_plain(item) for item in text.texts)
    if hasattr(text, "text"):
        return _rich_text_plain(text.text)
    return ""


def _inline_math_nodes(text: str) -> Any:
    """Build RichText nodes, using Telegram's native TextMath for inline TeX."""
    types = _types()
    pieces = []
    cursor = 0
    for match in _INLINE_MATH_RE.finditer(text):
        if match.start() > cursor:
            pieces.append(types.TextPlain(text=text[cursor:match.start()]))
        source = (match.group("dollar") or match.group("paren") or "").strip()
        if source:
            pieces.append(types.TextMath(source=source))
        else:
            pieces.append(types.TextPlain(text=match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        pieces.append(types.TextPlain(text=text[cursor:]))
    if not pieces:
        return types.TextPlain(text=text)
    if len(pieces) == 1:
        return pieces[0]
    return types.TextConcat(texts=pieces)


def _mask_code_for_detection(text: str) -> str:
    """Keep code verbatim for rendering, but ignore its dollar signs in detection."""
    def spaces(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    masked = re.sub(r"(?ms)^```[^\n]*\r?\n[\s\S]*?^```[ \t]*(?:\r?\n|$)", spaces, text)
    return re.sub(r"(?<!`)`{1,2}[^`\n]*`{1,2}(?!`)", spaces, masked)


def markdown_has_copyable_code(markdown: str) -> bool:
    """Detect fenced or inline Markdown code that can receive a native copy action."""
    text = str(markdown or "")
    fence = re.compile(r"(?ms)^[ \t]*```[^\n]*\r?\n[\s\S]*?^[ \t]*```[ \t]*(?:\r?\n|$)")
    if fence.search(text):
        return True
    without_fences = fence.sub("", text)
    return bool(re.search(r"(?<!`)`{1,2}[^`\n]+?`{1,2}(?!`)", without_fences))


def _rich_text_from_markdown(text: str) -> Any:
    """Convert inline Markdown, including constructs Telethon's parser drops.

    Telethon's Markdown parser silently drops ``==marked==``, ``||spoiler||``,
    single-star ``*italic*`` (it reads ``*`` as bold), ``<sub>`` and
    ``<sup>``. Split those out first — positions are found on the
    code-masked copy so matches inside code spans stay literal — recurse
    each plain segment through Telethon, and wrap the inner text in the
    native node. The inner text may itself carry Telethon formatting.
    """
    types = _types()
    masked = _mask_code_for_detection(str(text))
    pieces = []
    cursor = 0
    for match in _INLINE_EXTRA_RE.finditer(masked):
        start, end = match.start(), match.end()
        if start > cursor:
            pieces.append(_rich_text_from_telethon_markdown(text[cursor:start]))
        kind = match.lastgroup
        inner_start, inner_end = match.span(kind)
        inner = _rich_text_from_telethon_markdown(text[inner_start:inner_end])
        if kind == "marked":
            pieces.append(types.TextMarked(text=inner))
        elif kind == "spoiler":
            pieces.append(types.TextSpoiler(text=inner))
        elif kind == "italic":
            pieces.append(types.TextItalic(text=inner))
        elif kind == "sub":
            pieces.append(types.TextSubscript(text=inner))
        else:
            pieces.append(types.TextSuperscript(text=inner))
        cursor = end
    if cursor < len(text):
        pieces.append(_rich_text_from_telethon_markdown(text[cursor:]))
    if not pieces:
        return types.TextPlain(text=text)
    if len(pieces) == 1:
        return pieces[0]
    return types.TextConcat(texts=pieces)


def _rich_text_from_telethon_markdown(text: str) -> Any:
    """Convert inline Telegram Markdown formatting into the MTProto RichText tree."""
    types = _types()
    from telethon.extensions import markdown as telethon_markdown

    plain, entities = telethon_markdown.parse(str(text))
    offsets = _utf16_offsets(plain)
    spans = []
    for entity in entities or ():
        start_u16, end_u16 = int(entity.offset), int(entity.offset + entity.length)
        if start_u16 not in offsets or end_u16 not in offsets:
            raise ValueError("Telegram Markdown entity splits a UTF-16 surrogate pair")
        start, end = offsets[start_u16], offsets[end_u16]
        if start < 0 or end <= start or end > len(plain):
            raise ValueError("Telegram Markdown entity is outside its rich-text span")
        spans.append((start, end, entity))
    spans.sort(key=lambda item: (item[0], -item[1], type(item[2]).__name__))

    def render(start: int, end: int, contained: list[tuple[int, int, Any]]) -> Any:
        nodes = []
        cursor = start
        remaining = list(contained)
        while cursor < end:
            eligible = [item for item in remaining if item[0] >= cursor and item[1] <= end]
            if not eligible:
                nodes.append(_inline_math_nodes(plain[cursor:end]))
                break
            next_start = min(item[0] for item in eligible)
            if next_start > cursor:
                nodes.append(_inline_math_nodes(plain[cursor:next_start]))
                cursor = next_start
            same_start = [item for item in eligible if item[0] == cursor]
            if not same_start:
                nodes.append(_inline_math_nodes(plain[cursor:end]))
                break
            outer_end = max(item[1] for item in same_start)
            same_bounds = [item for item in same_start if item[1] == outer_end]
            nested = [item for item in eligible if item not in same_bounds
                      and item[0] >= cursor and item[1] <= outer_end]
            code_entity = any(type(item[2]).__name__ in {"MessageEntityCode", "MessageEntityPre"}
                              for item in same_bounds)
            if code_entity:
                inner = types.TextPlain(text=plain[cursor:outer_end])
            else:
                inner = render(cursor, outer_end, nested) if nested else _inline_math_nodes(plain[cursor:outer_end])
            for _, _, entity in reversed(same_bounds):
                inner = _wrap_rich_text(entity, inner)
            nodes.append(inner)
            consumed = set(id(item) for item in same_bounds + nested)
            remaining = [item for item in remaining if id(item) not in consumed]
            cursor = outer_end
        if not nodes:
            return types.TextEmpty()
        if len(nodes) == 1:
            return nodes[0]
        return types.TextConcat(texts=nodes)

    return render(0, len(plain), spans)


def _split_table_row(line: str) -> list[str]:
    cells = []
    current = []
    escaped = False
    content = line.strip()
    content = content.removeprefix("|")
    if content.endswith("|") and not content.endswith("\\|"):
        content = content[:-1]
    for char in content:
        if char == "|" and not escaped:
            cells.append("".join(current).strip().replace("\\|", "|"))
            current = []
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    cells.append("".join(current).strip().replace("\\|", "|"))
    return cells


def _task_item(body: str) -> tuple[str, bool | None, bool | None]:
    match = re.match(r"^\[([ xX])\][ \t]*(.*)$", body)
    if not match:
        return body, None, None
    return match.group(2), True, match.group(1).lower() == "x"


def _parse_list(lines: list[str], start: int, *, base_indent: int | None = None,
                ordered: bool | None = None, depth: int = 1) -> tuple[Any, int]:
    if depth > MAX_NESTING_DEPTH:
        raise ValueError(f"Telegram RichMessages cannot exceed {MAX_NESTING_DEPTH} nested levels")
    types = _types()
    first = _LIST_ITEM_RE.match(lines[start])
    if first is None:
        return None, start
    if base_indent is None:
        base_indent = len(first.group("indent").expandtabs(4))
    if ordered is None:
        ordered = first.group("marker")[0].isdigit()
    rows: list[dict[str, Any]] = []
    current = None
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        match = _LIST_ITEM_RE.match(line)
        if match:
            indent = len(match.group("indent").expandtabs(4))
            is_ordered = match.group("marker")[0].isdigit()
            if indent < base_indent:
                break
            if indent == base_indent:
                if is_ordered != ordered:
                    break
                body, checkbox, checked = _task_item(match.group("body"))
                current = {"body": [body], "checkbox": checkbox, "checked": checked,
                           "marker": match.group("marker"), "children": []}
                rows.append(current)
                index += 1
                continue
            if current is None:
                break
            child_block, next_index = _parse_list(
                lines, index, base_indent=indent, ordered=is_ordered, depth=depth + 1,
            )
            if child_block is None or next_index <= index:
                break
            current["children"].append(child_block)
            index = next_index
            continue
        if current is not None and line[:1].isspace():
            current["body"].append(line.strip())
            index += 1
            continue
        break

    items = []
    for row in rows:
        rich = _rich_text_from_markdown(" ".join(row["body"]))
        common = {"checkbox": row["checkbox"], "checked": row["checked"]}
        if row["children"]:
            blocks = [types.PageBlockParagraph(text=rich), *row["children"]]
            item_type = types.PageListOrderedItemBlocks if ordered else types.PageListItemBlocks
            kwargs = {"blocks": blocks, **common}
            if ordered:
                kwargs["num"] = row["marker"]
            items.append(item_type(**kwargs))
        else:
            item_type = types.PageListOrderedItemText if ordered else types.PageListItemText
            kwargs = {"text": rich, **common}
            if ordered:
                kwargs["num"] = row["marker"]
            items.append(item_type(**kwargs))
    if ordered:
        first_number = re.match(r"\d+", rows[0]["marker"] or "")
        block = types.PageBlockOrderedList(items=items, start=int(first_number.group()) if first_number else 1)
    else:
        block = types.PageBlockList(items=items)
    return block, index


def _markdown_segment_to_blocks(markdown: str) -> list[Any]:
    """Convert a Markdown segment into PageBlock* objects.

    Block-level HTML constructs from the rich-messages spec
    (``<details>``, ``<aside>``, ``<tg-map>``, ``<tg-collage>`` /
    ``<tg-slideshow>``, ``<img>`` / ``![alt](tg://...?id=N)`` with numeric
    server ids) are partitioned out first; the remaining lines go through
    the plain-Markdown segmenter. String media ids cannot be resolved on
    this route (no client): use ``rich_media`` + ``blocks_from_dict``.
    """
    if not markdown.strip():
        return []
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    blocks: list[Any] = []
    for kind, payload in _iter_segments(lines):
        if kind == "md":
            blocks.extend(_segment_plain_lines(payload))
        else:
            blocks.append(payload)
    return blocks


def _iter_segments(lines: list[str]):
    """Split lines into ("md", lines) runs and ("block", PageBlock) items."""
    index = 0
    current: list[str] = []
    while index < len(lines):
        parsed, next_index = _parse_custom_block(lines, index)
        if parsed is None:
            current.append(lines[index])
            index += 1
            continue
        if current:
            yield "md", current
            current = []
        yield "block", parsed
        index = next_index
    if current:
        yield "md", current


def _parse_custom_block(lines: list[str], index: int) -> tuple[Any | None, int]:
    """Parse one block-level HTML construct starting at ``lines[index]``.

    Returns ``(PageBlock, next_index)`` or ``(None, index)`` when the line
    starts no custom construct.
    """
    line = lines[index]
    if _DETAILS_OPEN_RE.match(line):
        return _parse_details(lines, index)
    if _ASIDE_OPEN_RE.match(line) or _SINGLE_ASIDE_RE.match(line):
        return _parse_aside(lines, index)
    tg_map = _TG_MAP_RE.match(line)
    if tg_map:
        return _parse_tg_map(tg_map), index + 1
    gallery = _GALLERY_OPEN_RE.match(line)
    if gallery:
        return _parse_gallery(lines, index, gallery.group(1))
    media = _IMG_RE.match(line) or _MD_IMG_RE.match(line)
    if media:
        return _media_block(media), index + 1
    return None, index


def _collect_until(lines: list[str], index: int, close: str) -> tuple[list[str], int]:
    body: list[str] = []
    index += 1
    while index < len(lines) and lines[index].strip() != close:
        body.append(lines[index])
        index += 1
    if index < len(lines):
        index += 1
    return body, index


def _parse_details(lines: list[str], index: int) -> tuple[Any, int]:
    types = _types()
    open_block = _DETAILS_OPEN_RE.match(lines[index]).group(1) is not None
    body, next_index = _collect_until(lines, index, "</details>")
    summary = types.TextPlain(text="Details")
    if body:
        match = _SUMMARY_RE.match(body[0])
        if match:
            summary = _rich_text_from_markdown(match.group(1).strip())
            body = body[1:]
    return types.PageBlockDetails(
        blocks=_markdown_segment_to_blocks("\n".join(body)),
        title=summary, open=open_block or None), next_index


def _parse_aside(lines: list[str], index: int) -> tuple[Any, int]:
    types = _types()
    single = _SINGLE_ASIDE_RE.match(lines[index])
    if single:
        return types.PageBlockPullquote(
            text=_rich_text_from_markdown(single.group(1).strip()),
            caption=types.TextEmpty()), index + 1
    body, next_index = _collect_until(lines, index, "</aside>")
    return types.PageBlockPullquote(
        text=_rich_text_from_markdown(" ".join(line.strip() for line in body if line.strip())),
        caption=types.TextEmpty()), next_index


def _parse_tg_map(match: Any) -> Any:
    types = _types()
    zoom = int(match.group("zoom")) if match.group("zoom") else 15
    if not 0 <= zoom <= 24:
        raise ValueError("map zoom must be 0..24")
    width = int(match.group("width")) if match.group("width") else 600
    height = int(match.group("height")) if match.group("height") else 300
    if not 0 <= width <= 10000 or not 0 <= height <= 10000:
        raise ValueError("map width/height must be 0..10000")
    geo = types.GeoPoint(long=float(match.group("long")), lat=float(match.group("lat")),
                         access_hash=0)
    return types.PageBlockMap(
        geo=geo, zoom=zoom, w=width, h=height,
        caption=types.PageCaption(text=types.TextEmpty(), credit=types.TextEmpty()))


def _parse_gallery(lines: list[str], index: int, kind: str) -> tuple[Any, int]:
    types = _types()
    close = f"</tg-{kind}>"
    body, next_index = _collect_until(lines, index, close)
    items = []
    for line in body:
        media = _IMG_RE.match(line) or _MD_IMG_RE.match(line)
        if media is not None:
            items.append(_media_block(media))
    caption = types.PageCaption(text=types.TextEmpty(), credit=types.TextEmpty())
    if kind == "collage":
        return types.PageBlockCollage(items=items, caption=caption), next_index
    return types.PageBlockSlideshow(items=items, caption=caption), next_index


def _media_block(match: Any) -> Any:
    """A media block from a numeric tg:// id (already-resolved server id)."""
    types = _types()
    scheme = match.group("scheme")
    numeric_id = int(match.group("id"))
    alt = match.group("alt") or ""
    caption = types.PageCaption(
        text=types.TextPlain(text=alt) if alt else types.TextEmpty(),
        credit=types.TextEmpty())
    if scheme == "photo":
        return types.PageBlockPhoto(photo_id=numeric_id, caption=caption)
    if scheme == "video":
        return types.PageBlockVideo(video_id=numeric_id, caption=caption)
    if scheme == "audio":
        return types.PageBlockAudio(audio_id=numeric_id, caption=caption)
    return types.PageBlockDocument(document_id=numeric_id, caption=caption)


def _parse_fence(lines: list[str], index: int) -> tuple[list[Any], int]:
    types = _types()
    language = _FENCE_RE.match(lines[index]).group(1).strip()
    code = []
    index += 1
    while index < len(lines) and not re.match(r"^[ \t]*```", lines[index]):
        code.append(lines[index])
        index += 1
    if index < len(lines):
        index += 1
    code_text = "\n".join(code)
    blocks = [types.PageBlockPreformatted(
        text=types.TextPlain(text=code_text), language=language)]
    if len(code_text) <= MAX_CODE_COPY_CHARS:
        blocks.append(types.PageBlockButtonRow(buttons=[types.PageButton(
            text=types.TextPlain("Copy"),
            type=types.InlineButtonTypeCopy(copy_text=code_text),
            style=types.RichButtonStyle(link=True),
        )]))
    return blocks, index


def _blocks_plain_text(blocks: list[Any]) -> str:
    """Flatten built blocks back to plain text (nested-quote fallback)."""
    parts = []
    for block in blocks:
        name = type(block).__name__
        if name in ("PageBlockParagraph", "PageBlockHeading1", "PageBlockHeading2",
                    "PageBlockHeading3", "PageBlockHeading4", "PageBlockHeading5",
                    "PageBlockHeading6", "PageBlockPreformatted", "PageBlockFooter",
                    "PageBlockBlockquote"):
            parts.append(_rich_text_plain(block.text))
        elif name in ("PageBlockList", "PageBlockOrderedList"):
            for item in block.items:
                parts.append(_rich_text_plain(getattr(item, "text", None)
                                              or item.blocks[0].text
                                              if getattr(item, "blocks", None) else ""))
    return "\n".join(part for part in parts if part)


def _parse_blockquote(lines: list[str], index: int) -> tuple[Any, int]:
    types = _types()
    quote = []
    while index < len(lines) and lines[index].lstrip().startswith(">"):
        quote.append(re.sub(r"^[ \t]*>[ \t]?", "", lines[index]))
        index += 1
    if any(line.lstrip().startswith(">") for line in quote):
        # Nested quotes have no native nesting in TL PageBlockBlockquote:
        # re-segment the stripped body and flatten it to text.
        nested = _markdown_segment_to_blocks("\n".join(quote))
        text = types.TextPlain(text=_blocks_plain_text(nested))
    else:
        text = _rich_text_from_markdown(" ".join(quote))
    return types.PageBlockBlockquote(text=text, caption=types.TextEmpty()), index


def _parse_table(lines: list[str], index: int) -> tuple[Any, int]:
    types = _types()
    header = _split_table_row(lines[index])
    delimiter = _split_table_row(lines[index + 1])
    if max(len(header), len(delimiter)) > MAX_TABLE_COLUMNS:
        raise ValueError(f"Telegram RichMessage tables cannot exceed {MAX_TABLE_COLUMNS} columns")
    rows = [types.PageTableRow(cells=[
        types.PageTableCell(
            header=True,
            align_center=(i < len(delimiter) and delimiter[i].startswith(":")
                          and delimiter[i].endswith(":")),
            align_right=(i < len(delimiter) and delimiter[i].endswith(":")
                         and not delimiter[i].startswith(":")),
            text=_rich_text_from_markdown(cell))
        for i, cell in enumerate(header)])]
    index += 2
    while index < len(lines) and "|" in lines[index] and lines[index].strip():
        cells = _split_table_row(lines[index])
        if len(cells) > MAX_TABLE_COLUMNS:
            raise ValueError(f"Telegram RichMessage tables cannot exceed {MAX_TABLE_COLUMNS} columns")
        # A row with exactly one non-empty cell is a section head:
        # one header cell spanning the whole table, so a single
        # table can carry a different head per section.
        non_empty = [cell for cell in cells if cell.strip()]
        if len(header) > 1 and len(non_empty) == 1:
            rows.append(types.PageTableRow(cells=[
                types.PageTableCell(
                    header=True, colspan=len(header),
                    text=_rich_text_from_markdown(non_empty[0]))]))
        else:
            rows.append(types.PageTableRow(cells=[
                types.PageTableCell(text=_rich_text_from_markdown(cell)) for cell in cells]))
        index += 1
    return types.PageBlockTable(
        title=types.TextEmpty(), rows=rows, bordered=True, striped=True), index


def _segment_plain_lines(lines: list[str]) -> list[Any]:
    types = _types()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            blocks.append(getattr(types, f"PageBlockHeading{level}")(
                text=_rich_text_from_markdown(heading.group(2).strip())))
            index += 1
            continue
        if _DIVIDER_RE.match(line):
            blocks.append(types.PageBlockDivider())
            index += 1
            continue
        if _FENCE_RE.match(line):
            fence_blocks, index = _parse_fence(lines, index)
            blocks.extend(fence_blocks)
            continue
        if line.lstrip().startswith(">"):
            quote_block, index = _parse_blockquote(lines, index)
            blocks.append(quote_block)
            continue
        if (index + 1 < len(lines) and "|" in line and "|" in lines[index + 1]
                and _TABLE_DELIMITER_RE.match(lines[index + 1])):
            table_block, index = _parse_table(lines, index)
            blocks.append(table_block)
            continue
        list_match = _LIST_ITEM_RE.match(line)
        if list_match:
            list_block, next_index = _parse_list(lines, index)
            if list_block is not None:
                blocks.append(list_block)
                index = next_index
                continue
        paragraph = [line.strip()]
        index += 1
        while index < len(lines) and lines[index].strip():
            if (_HEADING_RE.match(lines[index]) or _DIVIDER_RE.match(lines[index])
                    or _FENCE_RE.match(lines[index]) or lines[index].lstrip().startswith(">")
                    or _LIST_ITEM_RE.match(lines[index])):
                break
            if (index + 1 < len(lines) and "|" in lines[index]
                    and _TABLE_DELIMITER_RE.match(lines[index + 1])):
                break
            paragraph.append(lines[index].strip())
            index += 1
        blocks.append(types.PageBlockParagraph(
            text=_rich_text_from_markdown(" ".join(paragraph))))
    return blocks


def split_footnotes(markdown: str) -> tuple[str, list[tuple[int, str]]]:
    """Extract ``[^label]: definition`` lines; replace refs with superscripts.

    There is no footnote type in the layer 233 TL schema (``TextReference``
    does not exist), so references become superscript numerals and the
    definitions are returned for a trailing paragraph (see
    ``footnote_blocks``). Refs inside fenced code and refs with no
    definition stay literal. Returns ``(cleaned_markdown, numbered_defs)``.
    """
    lines = str(markdown or "").splitlines()
    defs: dict[str, str] = {}
    kept: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        if not in_fence:
            match = _FOOTNOTE_DEF_RE.match(line)
            if match:
                defs[match.group(1)] = match.group(2).strip()
                continue
        kept.append(line)
    order: list[str] = []
    in_fence = False

    def replace(match: Any) -> str:
        label = match.group(1)
        if label not in defs:
            return match.group(0)
        if label not in order:
            order.append(label)
        return str(order.index(label) + 1).translate(_SUPERSCRIPT_DIGITS)

    cleaned: list[str] = []
    for line in kept:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        cleaned.append(line if in_fence else _FOOTNOTE_REF_RE.sub(replace, line))
    numbered = [(i + 1, defs[label]) for i, label in enumerate(order)]
    return "\n".join(cleaned), numbered


def footnote_blocks(numbered: list[tuple[int, str]]) -> list[Any]:
    """Trailing definitions paragraph for footnotes split out by split_footnotes."""
    if not numbered:
        return []
    types = _types()
    text = "\n".join(f"{num}. {definition}" for num, definition in numbered)
    return [types.PageBlockParagraph(text=types.TextPlain(text=text))]


def native_math_blocks(markdown: str) -> Any | None:
    """Return InputRichMessage with PageBlockMath/TextMath when math is present.

    Returns ``None`` for ordinary prose so the caller can keep the native
    InputRichMessageMarkdown path untouched.
    """
    text = str(markdown or "")
    text, numbered = split_footnotes(text)
    matches = [match for match in _DISPLAY_MATH_RE.finditer(text) if not match.group("code")]
    if not matches:
        # Inline math is represented by Telegram's native TextMath RichText
        # constructor; use a paragraph block only when such an expression exists.
        if not _INLINE_MATH_RE.search(_mask_code_for_detection(text)):
            return None
        blocks = _markdown_segment_to_blocks(text)
        blocks.extend(footnote_blocks(numbered))
        _validate_native_blocks(blocks)
        return _types().InputRichMessage(blocks=blocks)

    types = _types()
    blocks: list[Any] = []
    cursor = 0
    for match in matches:
        blocks.extend(_markdown_segment_to_blocks(text[cursor:match.start()]))
        source = (match.group("fenced_source") or match.group("dollar_source")
                  or match.group("bracket_source") or "").strip()
        if source:
            blocks.append(types.PageBlockMath(source=source))
        cursor = match.end()
    blocks.extend(_markdown_segment_to_blocks(text[cursor:]))
    blocks.extend(footnote_blocks(numbered))
    if not blocks:
        return None
    _validate_native_blocks(blocks)
    return types.InputRichMessage(blocks=blocks)
