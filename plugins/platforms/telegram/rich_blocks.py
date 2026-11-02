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
MAX_NATIVE_BLOCKS = 500
MAX_NESTING_DEPTH = 16
MAX_TABLE_COLUMNS = 20
MAX_CODE_COPY_CHARS = 100


def _types():
    from telethon import types
    return types


def _validate_native_blocks(blocks: list[Any]) -> None:
    """Reject explicit block payloads outside Telegram's documented native limits."""
    types = _types()
    stack = list(blocks)
    count = 0
    while stack:
        node = stack.pop()
        if isinstance(node, (list, tuple)):
            stack.extend(node)
            continue
        if not isinstance(node, types.TLObject):
            continue
        name = type(node).__name__
        if (name.startswith(("PageBlock", "PageListItem", "PageListOrderedItem"))
                or name == "PageTableRow"):
            count += 1
            if count > MAX_NATIVE_BLOCKS:
                raise ValueError(f"Telegram RichMessages cannot exceed {MAX_NATIVE_BLOCKS} blocks")
        for value in vars(node).values():
            if isinstance(value, (list, tuple)):
                stack.extend(value)
            elif isinstance(value, types.TLObject):
                stack.append(value)


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
        return types.TextUrl(text=inner, url=str(getattr(entity, "url", "")), webpage_id=0)
    if name == "MessageEntityUrl":
        return types.TextUrl(text=inner, url=_rich_text_plain(inner), webpage_id=0)
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
    types = _types()
    if not markdown.strip():
        return []
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
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
        fence = _FENCE_RE.match(line)
        if fence:
            language = fence.group(1).strip()
            code = []
            index += 1
            while index < len(lines) and not re.match(r"^[ \t]*```", lines[index]):
                code.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            code_text = "\n".join(code)
            blocks.append(types.PageBlockPreformatted(
                text=types.TextPlain(text=code_text), language=language))
            if len(code_text) <= MAX_CODE_COPY_CHARS:
                blocks.append(types.PageBlockButtonRow(buttons=[types.PageButton(
                    text=types.TextPlain("Copy"),
                    type=types.InlineButtonTypeCopy(copy_text=code_text),
                    style=types.RichButtonStyle(link=True),
                )]))
            continue
        if line.lstrip().startswith(">"):
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote.append(re.sub(r"^[ \t]*>[ \t]?", "", lines[index]))
                index += 1
            blocks.append(types.PageBlockBlockquote(
                text=_rich_text_from_markdown(" ".join(quote)), caption=types.TextEmpty()))
            continue
        if (index + 1 < len(lines) and "|" in line and "|" in lines[index + 1]
                and _TABLE_DELIMITER_RE.match(lines[index + 1])):
            header = _split_table_row(line)
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
            blocks.append(types.PageBlockTable(
                title=types.TextEmpty(), rows=rows, bordered=True, striped=True))
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


def native_math_blocks(markdown: str) -> Any | None:
    """Return InputRichMessage with PageBlockMath/TextMath when math is present.

    Returns ``None`` for ordinary prose so the caller can keep the native
    InputRichMessageMarkdown path untouched.
    """
    text = str(markdown or "")
    matches = [match for match in _DISPLAY_MATH_RE.finditer(text) if not match.group("code")]
    if not matches:
        # Inline math is represented by Telegram's native TextMath RichText
        # constructor; use a paragraph block only when such an expression exists.
        if not _INLINE_MATH_RE.search(_mask_code_for_detection(text)):
            return None
        blocks = _markdown_segment_to_blocks(text)
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
    if not blocks:
        return None
    _validate_native_blocks(blocks)
    return types.InputRichMessage(blocks=blocks)
