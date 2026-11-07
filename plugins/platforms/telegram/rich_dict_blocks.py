"""Literal dict -> Telethon InputRichMessage blocks.

Port of ``tg-rich-messages`` ``rich_ui.py`` (serejaris/telegram-skills,
Bot API 10.3) to Telethon layer 233. The source helper built validated
literal block payloads for ``sendRichMessage``; this module builds the same
payloads as Telethon TL objects (``InputRichMessage(blocks=...)``) so they
can be sent with the existing ``send_request`` in ``mtproto_rich.py``.

Validation is ported verbatim from ``rich_ui.build_blocks_message``:
32,768 literal characters, 500 blocks (table rows count), 16 nesting depth,
50 media, 20 table columns, heading size 1..6, true-only flags, no unknown
fields. Validation is local only, never an API call.

Bot API -> Telethon mapping notes (verified against the installed layer 233):

* There is no ``InputRichMessageMedia`` type. Media blocks reference a
  string id that must be present in the ``media`` map (``ResolvedRichMedia``
  from ``rich_media``); the builder turns it into the server-side integer
  id on the block and collects the ``InputPhoto`` / ``InputDocument`` into
  ``InputRichMessage(photos=..., documents=...)``.
* ``InputRichBlockAnimation`` / ``InputRichBlockVoiceNote`` do not exist:
  animation folds into ``PageBlockVideo``, voice notes into ``PageBlockAudio``.
* ``InputRichBlockMap.zoom`` is optional 0..24 in the Bot API; the TL
  ``PageBlockMap`` requires ``zoom``/``w``/``h`` — defaults zoom 15,
  600x300.
* ``date_time_format`` (a free-form format string) has no TL equivalent:
  ``TextDate`` takes six boolean flags. The format string is mapped by an
  explicit token table (see ``_date_time_flags``); an unrecognised format
  raises instead of silently rendering the wrong date.
* ``reference`` / ``reference_link`` / ``anchor_link`` have no TL
  equivalent (``TextReference`` does not exist in layer 233) and raise,
  naming the node. ``anchor`` maps to ``TextAnchor``.
* ``thinking`` is draft-only: it raises unless ``draft=True``.
* Table ``caption`` maps to the TL ``title``; ``align`` / ``valign``
  strings map to the TL boolean flags (top = neither valign flag).
* ``open_app`` buttons have no TL equivalent in layer 233 (there is no
  ``InlineButtonTypeOpenApp``) and raise.
"""
from __future__ import annotations

import datetime
from typing import Any

from .rich_media import ResolvedRichMedia

MAX_TEXT = 32768
MAX_BLOCKS = 500
MAX_DEPTH = 16
MAX_MEDIA = 50
MAX_TABLE_COLUMNS = 20
MAX_BUTTONS_PER_ROW = 8
BUTTON_STYLES = frozenset(("danger", "success", "primary", "link"))

_MEDIA_BLOCK_TYPES = ("photo", "video", "animation", "audio", "voice_note", "document")

# PHP-style date() tokens (the format vocabulary TDLib documents for
# RichTextDateTime.date_time_format) mapped onto TextDate's six flags.
_DATE_TOKENS = frozenset("djDlFMmnYy")
_TIME_TOKENS = frozenset("HGhgisaA")
_LONG_DATE_TOKENS = frozenset("Fl")
_DAY_OF_WEEK_TOKENS = frozenset("Dl")


def _types():
    from telethon import types
    return types


def _text_chars(value: Any) -> int:
    """Length of a literal string, or the plain-text length of a RichText dict."""
    if isinstance(value, str):
        value.encode("utf-8")  # lone surrogates raise ValueError, as in rich_ui
        return len(value)
    if isinstance(value, dict):
        node_type = value.get("type")
        if node_type == "concat":
            texts = value.get("texts")
            if not isinstance(texts, list):
                raise ValueError("concat RichText requires a texts list")
            return sum(_text_chars(item) for item in texts)
        if node_type in ("custom_emoji", "date_time"):
            return _text_chars(value.get("text", "") or value.get("alternative_text", ""))
        if "text" in value:
            return _text_chars(value["text"])
        raise ValueError(f"RichText node {node_type!r} has no text to count")
    raise ValueError("text must be a literal string or a RichText dict")


def _fields(value: Any, required: set[str], optional: set[str] = ()) -> None:
    if (not isinstance(value, dict) or not required <= value.keys()
            or value.keys() - required - set(optional)):
        raise ValueError("unsupported or missing fields")


def _true_fields(value: dict, names: tuple[str, ...]) -> None:
    for name in names:
        if name in value and value[name] is not True:
            raise ValueError("optional flag must be true or omitted")


def _date_time_flags(date_time_format: Any) -> dict[str, bool]:
    """Map a TDLib date_time_format string onto TextDate boolean flags.

    ``relative`` renders a relative date. Otherwise the token classes decide:
    date tokens -> a date flag (long when a full month/weekday name is used),
    time tokens -> a time flag (long when seconds are shown), weekday tokens
    -> ``day_of_week``. A format with neither date nor time tokens is not a
    date format at all and raises.
    """
    fmt = str(date_time_format or "")
    if fmt == "relative":
        return {"relative": True}
    stripped = "".join(
        char for i, char in enumerate(fmt)
        if not (char == "\\" and i + 1 < len(fmt)) and (i == 0 or fmt[i - 1] != "\\"))
    has_date = any(token in stripped for token in _DATE_TOKENS)
    has_time = any(token in stripped for token in _TIME_TOKENS)
    if not has_date and not has_time:
        raise ValueError(f"unknown date_time_format: {date_time_format!r}")
    flags: dict[str, bool] = {}
    if has_date:
        flags["long_date" if any(t in stripped for t in _LONG_DATE_TOKENS) else "short_date"] = True
    if has_time:
        flags["long_time" if "s" in stripped else "short_time"] = True
    if any(t in stripped for t in _DAY_OF_WEEK_TOKENS):
        flags["day_of_week"] = True
    return flags


class _Counters:
    """Shared preflight counters: blocks, literal chars, media occurrences."""

    def __init__(self) -> None:
        self.count = 0
        self.chars = 0
        self.media = 0

    def check_limits(self) -> None:
        if self.count > MAX_BLOCKS or self.chars > MAX_TEXT or self.media > MAX_MEDIA:
            raise ValueError("rich message exceeds documented limits")


def _validate_items(items: Any, depth: int, counters: _Counters) -> None:
    if not isinstance(items, list) or not items or depth > MAX_DEPTH:
        raise ValueError("blocks must be nonempty and within nesting limit")
    for block in items:
        _validate_block(block, depth, counters)


def _validate_block(block: Any, depth: int, counters: _Counters) -> None:
    if not isinstance(block, dict):
        raise ValueError("block must be an object")
    kind = block.get("type")
    if not isinstance(kind, str):
        raise ValueError("block type must be a string")
    counters.count += 1
    handler = _BLOCK_VALIDATORS.get(kind)
    if handler is None:
        raise ValueError("unsupported block type")
    handler(block, depth, counters)
    counters.check_limits()


def _validate_text_block(block: dict, depth: int, counters: _Counters) -> None:
    kind = block["type"]
    required = {"type", "text"} | ({"size"} if kind == "heading" else set())
    _fields(block, required, {"language"} if kind == "pre" else set())
    counters.chars += _text_chars(block["text"])
    if kind == "heading" and (type(block["size"]) is not int or not 1 <= block["size"] <= 6):
        raise ValueError("heading size must be 1..6")
    if "language" in block:
        _text_chars(block["language"])


def _validate_media_block(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "media"},
            {"caption", "has_spoiler", "autoplay", "loop"})
    _true_fields(block, ("has_spoiler", "autoplay", "loop"))
    if not isinstance(block["media"], str) or not block["media"]:
        raise ValueError("media block requires a media id string")
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    counters.media += 1


def _validate_details(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "summary", "blocks"}, {"is_open"})
    counters.chars += _text_chars(block["summary"])
    _true_fields(block, ("is_open",))
    _validate_items(block["blocks"], depth + 1, counters)


def _validate_list(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "items"}, {"ordered", "start", "reversed"})
    _true_fields(block, ("ordered", "reversed"))
    if not isinstance(block["items"], list) or not block["items"]:
        raise ValueError("list must contain items")
    for item in block["items"]:
        counters.count += 1
        if isinstance(item, dict) and "blocks" in item:
            _validate_items(item["blocks"], depth + 1, counters)
        elif isinstance(item, dict):
            counters.chars += _text_chars(item.get("text", ""))
        else:
            counters.chars += _text_chars(item)


def _validate_quote(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "text"}, {"caption", "is_collapsed"})
    counters.chars += _text_chars(block["text"])
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    _true_fields(block, ("is_collapsed",))


def _validate_table(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "cells"},
            {"caption", "is_compact", "is_bordered", "is_striped"})
    _true_fields(block, ("is_compact", "is_bordered", "is_striped"))
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    rows = block["cells"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("table must contain rows")
    for row in rows:
        counters.count += 1
        _validate_table_row(row, counters)


def _validate_table_row(row: Any, counters: _Counters) -> None:
    if not isinstance(row, list) or not row or len(row) > MAX_TABLE_COLUMNS:
        raise ValueError("table row must have 1..20 cells")
    width = 0
    for cell in row:
        _fields(cell, {"align", "valign"},
                {"text", "is_header", "colspan", "rowspan"})
        if cell["align"] not in ("left", "center", "right") or cell["valign"] not in ("top", "middle", "bottom"):
            raise ValueError("invalid table alignment")
        _true_fields(cell, ("is_header",))
        if "text" in cell:
            counters.chars += _text_chars(cell["text"])
        for span in ("colspan", "rowspan"):
            if span in cell and (type(cell[span]) is not int or cell[span] < 1):
                raise ValueError("span must be a positive integer")
        width += cell.get("colspan", 1)
    if width > MAX_TABLE_COLUMNS:
        raise ValueError("table row spans exceed 20 columns")


def _validate_map(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "lat", "long"},
            {"zoom", "width", "height", "caption"})
    if not isinstance(block["lat"], (int, float)) or not isinstance(block["long"], (int, float)):
        raise ValueError("map requires numeric lat/long")
    if "zoom" in block and (type(block["zoom"]) is not int or not 0 <= block["zoom"] <= 24):
        raise ValueError("map zoom must be 0..24")
    for dim in ("width", "height"):
        if dim in block and (type(block[dim]) is not int or not 0 <= block[dim] <= 10000):
            raise ValueError(f"map {dim} must be 0..10000")
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])


def _validate_gallery(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "blocks"}, {"caption"})
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    _validate_items(block["blocks"], depth + 1, counters)


def _validate_button_row(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "buttons"}, {"align"})
    if block.get("align", "left") not in ("left", "center", "right"):
        raise ValueError("button row align must be left, center or right")
    buttons = block["buttons"]
    if not isinstance(buttons, list) or not buttons or len(buttons) > MAX_BUTTONS_PER_ROW:
        raise ValueError("button row must have 1..8 buttons")
    for button in buttons:
        _fields(button, {"text"},
                {"callback_data", "url", "copy_text", "switch_inline_query",
                 "same_peer", "style", "open_app"})
        button_chars = _text_chars(button["text"])
        if button_chars == 0:
            raise ValueError("button text must not be empty")
        counters.chars += button_chars
        actions = [key for key in ("callback_data", "url", "copy_text", "switch_inline_query",
                               "open_app")
                   if key in button]
        if len(actions) != 1:
            raise ValueError("button requires exactly one action")
        _true_fields(button, ("same_peer",))
        if "style" in button and button["style"] not in BUTTON_STYLES:
            raise ValueError("unsupported inline button style")


def _validate_embed(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type"}, {"url", "html", "caption", "poster",
                              "width", "height", "full_width", "allow_scrolling"})
    _true_fields(block, ("full_width", "allow_scrolling"))
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    if "poster" in block:
        counters.media += 1


def _validate_embed_post(block: dict, depth: int, counters: _Counters) -> None:
    _fields(block, {"type", "url", "webpage_id", "author_photo_id",
                    "author", "date", "blocks"}, {"caption"})
    if "caption" in block:
        counters.chars += _text_chars(block["caption"])
    _validate_items(block["blocks"], depth + 1, counters)


def _validate_simple(block: dict, depth: int, counters: _Counters) -> None:
    kind = block["type"]
    if kind == "divider":
        _fields(block, {"type"})
    elif kind == "anchor":
        _fields(block, {"type", "name"})
        _text_chars(block["name"])
    elif kind == "mathematical_expression":
        _fields(block, {"type", "expression"})
        counters.chars += _text_chars(block["expression"])
    elif kind == "thinking":
        _fields(block, {"type", "text"})
        counters.chars += _text_chars(block["text"])


_BLOCK_VALIDATORS = {
    "paragraph": _validate_text_block,
    "heading": _validate_text_block,
    "pre": _validate_text_block,
    "footer": _validate_text_block,
    "divider": _validate_simple,
    "anchor": _validate_simple,
    "mathematical_expression": _validate_simple,
    "thinking": _validate_simple,
    "photo": _validate_media_block,
    "video": _validate_media_block,
    "animation": _validate_media_block,
    "audio": _validate_media_block,
    "voice_note": _validate_media_block,
    "document": _validate_media_block,
    "details": _validate_details,
    "list": _validate_list,
    "blockquote": _validate_quote,
    "pullquote": _validate_quote,
    "table": _validate_table,
    "map": _validate_map,
    "collage": _validate_gallery,
    "slideshow": _validate_gallery,
    "button_row": _validate_button_row,
    "embed": _validate_embed,
    "embed_post": _validate_embed_post,
}


def validate_dict_blocks(blocks: Any) -> None:
    """Preflight a literal block list exactly like rich_ui.build_blocks_message.

    Raises ``ValueError`` on any malformed block or limit breach. Counts
    literal Unicode code points (not bytes); table rows count toward the
    500-block limit; media blocks count toward the 50-media limit.
    """
    try:
        _validate_items(blocks, 1, _Counters())
    except RecursionError:
        raise ValueError("cyclic or excessive nesting") from None


def _plain_text(node: Any) -> str:
    from .rich_blocks import _rich_text_plain
    return _rich_text_plain(rich_text_from_dict(node))


def rich_text_from_dict(node: Any) -> Any:
    """Build a Telethon RichText node from a literal dict (or a plain string)."""
    types = _types()
    if isinstance(node, str):
        node.encode("utf-8")
        return types.TextPlain(text=node)
    if not isinstance(node, dict):
        raise ValueError("RichText must be a literal string or an object")
    node_type = node.get("type")
    if not isinstance(node_type, str):
        raise ValueError("RichText type must be a string")
    if node_type in ("plain", "text"):
        _fields(node, {"type", "text"})
        return types.TextPlain(text=node["text"])
    if node_type == "concat":
        _fields(node, {"type", "texts"})
        texts = [rich_text_from_dict(item) for item in node["texts"]]
        return types.TextConcat(texts=texts) if len(texts) != 1 else texts[0]
    wrappers = {
        "bold": types.TextBold,
        "italic": types.TextItalic,
        "underline": types.TextUnderline,
        "strikethrough": types.TextStrike,
        "spoiler": types.TextSpoiler,
        "marked": types.TextMarked,
        "code": types.TextFixed,
        "pre": types.TextFixed,
        "subscript": types.TextSubscript,
        "superscript": types.TextSuperscript,
        "bank_card": types.TextBankCard,
        "mention": types.TextMention,
        "hashtag": types.TextHashtag,
        "cashtag": types.TextCashtag,
        "bot_command": types.TextBotCommand,
    }
    if node_type in wrappers:
        _fields(node, {"type", "text"})
        return wrappers[node_type](text=rich_text_from_dict(node["text"]))
    handler = _RICHTEXT_BUILDERS.get(node_type)
    if handler is None:
        if node_type in ("reference", "reference_link", "anchor_link"):
            raise ValueError(
                f"RichText {node_type!r} has no Telethon layer 233 equivalent "
                "(TextReference does not exist); footnotes are not supported as "
                "native rich text")
        raise ValueError(f"unsupported RichText type: {node_type!r}")
    return handler(node)


def _rich_url(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text", "url"})
    return types.TextUrl(text=rich_text_from_dict(node["text"]),
                         url=str(node["url"]), webpage_id=0)


def _rich_email(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text"}, {"email"})
    email = node.get("email") or _plain_text(node["text"])
    return types.TextEmail(text=rich_text_from_dict(node["text"]), email=str(email))


def _rich_phone(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text"}, {"phone"})
    phone = node.get("phone") or _plain_text(node["text"])
    return types.TextPhone(text=rich_text_from_dict(node["text"]), phone=str(phone))


def _rich_text_mention(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text"}, {"user", "user_id"})
    user_id = node.get("user_id")
    if user_id is None:
        user = node.get("user")
        if not isinstance(user, dict) or "id" not in user:
            raise ValueError("text_mention requires a user id")
        user_id = user["id"]
    return types.TextMentionName(text=rich_text_from_dict(node["text"]),
                                 user_id=int(user_id))


def _rich_custom_emoji(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "custom_emoji_id", "alternative_text"})
    return types.TextCustomEmoji(document_id=int(node["custom_emoji_id"]),
                                 alt=str(node["alternative_text"]))


def _rich_math(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "expression"})
    return types.TextMath(source=str(node["expression"]))


def _rich_date_time(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text", "unix_time", "date_time_format"})
    date = datetime.datetime.fromtimestamp(int(node["unix_time"]), tz=datetime.UTC)
    return types.TextDate(text=rich_text_from_dict(node["text"]), date=date,
                          **_date_time_flags(node["date_time_format"]))


def _rich_anchor(node: dict) -> Any:
    types = _types()
    _fields(node, {"type", "text", "name"})
    return types.TextAnchor(text=rich_text_from_dict(node["text"]),
                            name=str(node["name"]))


_RICHTEXT_BUILDERS = {
    "url": _rich_url,
    "email": _rich_email,
    "phone": _rich_phone,
    "text_mention": _rich_text_mention,
    "custom_emoji": _rich_custom_emoji,
    "mathematical_expression": _rich_math,
    "date_time": _rich_date_time,
    "anchor": _rich_anchor,
}


def _caption(node: Any) -> Any:
    """Build a PageCaption from a string, a RichText dict, or a caption dict."""
    types = _types()
    if node is None:
        return types.PageCaption(text=types.TextEmpty(), credit=types.TextEmpty())
    if isinstance(node, dict) and "text" in node and "credit" in node:
        return types.PageCaption(text=rich_text_from_dict(node["text"]),
                                 credit=rich_text_from_dict(node["credit"]))
    return types.PageCaption(text=rich_text_from_dict(node),
                             credit=types.TextEmpty())


def _media_entry(value: Any) -> tuple[str, int, Any]:
    """Normalise a media-map entry to (kind, server id, InputPhoto/InputDocument)."""
    types = _types()
    if isinstance(value, ResolvedRichMedia):
        obj = value.photo if value.photo is not None else value.document
        return value.kind, value.numeric_id, obj
    if isinstance(value, types.InputPhoto):
        return "photo", int(value.id), value
    if isinstance(value, types.InputDocument):
        return "document", int(value.id), value
    raise ValueError("media map entries must be ResolvedRichMedia, InputPhoto or InputDocument")


class _Builder:
    """Stateful dict -> PageBlock builder collecting photos/documents lists."""

    def __init__(self, media: dict[str, Any] | None, draft: bool):
        self.media = media or {}
        self.draft = draft
        self.photos: dict[int, Any] = {}
        self.documents: dict[int, Any] = {}

    def media_id(self, key: Any, *, want: str) -> int:
        if not isinstance(key, str) or key not in self.media:
            raise ValueError(f"media id {key!r} has no binding in the media map")
        kind, numeric_id, obj = _media_entry(self.media[key])
        if want == "photo" and kind != "photo":
            raise ValueError(f"media id {key!r} is a document, not a photo")
        if want == "document" and kind != "document":
            raise ValueError(f"media id {key!r} is a photo, not a document")
        if kind == "photo":
            self.photos.setdefault(numeric_id, obj)
        else:
            self.documents.setdefault(numeric_id, obj)
        return numeric_id

    def blocks(self, items: list[dict], depth: int) -> list[Any]:
        if depth > MAX_DEPTH:
            raise ValueError(f"Telegram RichMessages cannot exceed {MAX_DEPTH} nested levels")
        return [self.block(item, depth) for item in items]

    def block(self, item: dict, depth: int) -> Any:
        handler = _BLOCK_BUILDERS.get(item["type"])
        if handler is None:
            raise ValueError("unsupported block type")
        return handler(self, item, depth)

    def _simple(self, item: dict, depth: int) -> Any:
        types = _types()
        kind = item["type"]
        if kind == "paragraph":
            return types.PageBlockParagraph(text=rich_text_from_dict(item["text"]))
        if kind == "heading":
            return getattr(types, f"PageBlockHeading{item['size']}")(
                text=rich_text_from_dict(item["text"]))
        if kind == "divider":
            return types.PageBlockDivider()
        if kind == "pre":
            return types.PageBlockPreformatted(
                text=rich_text_from_dict(item["text"]),
                language=str(item.get("language", "")))
        if kind == "footer":
            return types.PageBlockFooter(text=rich_text_from_dict(item["text"]))
        if kind == "anchor":
            return types.PageBlockAnchor(name=str(item["name"]))
        if kind == "mathematical_expression":
            return types.PageBlockMath(source=str(item["expression"]))
        if kind == "thinking":
            if not self.draft:
                raise ValueError("thinking blocks are valid only in drafts, not in sent messages")
            return types.PageBlockThinking(text=rich_text_from_dict(item["text"]))
        raise ValueError("unsupported block type")

    def _media(self, item: dict, depth: int) -> Any:
        types = _types()
        kind = item["type"]
        caption = _caption(item.get("caption"))
        spoiler = bool(item.get("has_spoiler", False)) or None
        if kind == "photo":
            return types.PageBlockPhoto(
                photo_id=self.media_id(item["media"], want="photo"),
                caption=caption, spoiler=spoiler)
        if kind in ("video", "animation"):
            # No PageBlockAnimation in layer 233: animations are videos.
            return types.PageBlockVideo(
                video_id=self.media_id(item["media"], want="document"),
                caption=caption,
                autoplay=bool(item.get("autoplay", False)) or None,
                loop=bool(item.get("loop", False)) or None,
                spoiler=spoiler)
        if kind == "audio":
            return types.PageBlockAudio(
                audio_id=self.media_id(item["media"], want="document"),
                caption=caption)
        # document / voice_note: both carry a document id.
        # (No PageBlockVoiceNote in layer 233: voice notes are audio.)
        return types.PageBlockDocument(
            document_id=self.media_id(item["media"], want="document"),
            caption=caption)

    def _details(self, item: dict, depth: int) -> Any:
        types = _types()
        return types.PageBlockDetails(
            blocks=self.blocks(item["blocks"], depth + 1),
            title=rich_text_from_dict(item["summary"]),
            open=bool(item.get("is_open", False)) or None)

    def _list(self, item: dict, depth: int) -> Any:
        types = _types()
        ordered = bool(item.get("ordered", False))
        items = []
        for raw in item["items"]:
            items.append(self._list_item(raw, ordered, depth))
        if ordered:
            return types.PageBlockOrderedList(
                items=items, start=item.get("start"),
                reversed=bool(item.get("reversed", False)) or None)
        return types.PageBlockList(items=items)

    def _list_item(self, raw: Any, ordered: bool, depth: int) -> Any:
        types = _types()
        if isinstance(raw, dict) and "blocks" in raw:
            blocks = [types.PageBlockParagraph(
                text=rich_text_from_dict(raw["text"]))] if "text" in raw else []
            blocks.extend(self.blocks(raw["blocks"], depth + 1))
            common = {"checkbox": bool(raw.get("checkbox", False)) or None,
                      "checked": bool(raw.get("checked", False)) or None}
            if ordered:
                return types.PageListOrderedItemBlocks(
                    blocks=blocks, num=raw.get("num"), **common)
            return types.PageListItemBlocks(blocks=blocks, **common)
        text = raw["text"] if isinstance(raw, dict) else raw
        checkbox = raw.get("checkbox") if isinstance(raw, dict) else None
        checked = raw.get("checked") if isinstance(raw, dict) else None
        num = raw.get("num") if isinstance(raw, dict) else None
        common = {"checkbox": bool(checkbox) or None,
                  "checked": bool(checked) or None}
        if ordered:
            return types.PageListOrderedItemText(
                text=rich_text_from_dict(text), num=num, **common)
        return types.PageListItemText(text=rich_text_from_dict(text), **common)

    def _quote(self, item: dict, depth: int) -> Any:
        types = _types()
        caption = (rich_text_from_dict(item["caption"])
                   if "caption" in item else types.TextEmpty())
        if item["type"] == "blockquote":
            return types.PageBlockBlockquote(
                text=rich_text_from_dict(item["text"]), caption=caption,
                collapsed=bool(item.get("is_collapsed", False)) or None)
        return types.PageBlockPullquote(
            text=rich_text_from_dict(item["text"]), caption=caption)

    def _table(self, item: dict, depth: int) -> Any:
        types = _types()
        rows = [types.PageTableRow(cells=[self._table_cell(cell) for cell in row])
                for row in item["cells"]]
        return types.PageBlockTable(
            title=(rich_text_from_dict(item["caption"])
                   if "caption" in item else types.TextEmpty()),
            rows=rows,
            bordered=bool(item.get("is_bordered", False)) or None,
            striped=bool(item.get("is_striped", False)) or None,
            compact=bool(item.get("is_compact", False)) or None)

    def _table_cell(self, cell: dict) -> Any:
        types = _types()
        return types.PageTableCell(
            header=bool(cell.get("is_header", False)) or None,
            align_center=(cell["align"] == "center") or None,
            align_right=(cell["align"] == "right") or None,
            valign_middle=(cell["valign"] == "middle") or None,
            valign_bottom=(cell["valign"] == "bottom") or None,
            text=(rich_text_from_dict(cell["text"]) if "text" in cell else None),
            colspan=cell.get("colspan"),
            rowspan=cell.get("rowspan"))

    def _map(self, item: dict, depth: int) -> Any:
        types = _types()
        geo = types.GeoPoint(long=float(item["long"]), lat=float(item["lat"]),
                             access_hash=0)
        return types.PageBlockMap(
            geo=geo, zoom=int(item.get("zoom", 15)),
            w=int(item.get("width", 600)), h=int(item.get("height", 300)),
            caption=_caption(item.get("caption")))

    def _gallery(self, item: dict, depth: int) -> Any:
        types = _types()
        items = self.blocks(item["blocks"], depth + 1)
        caption = _caption(item.get("caption"))
        if item["type"] == "collage":
            return types.PageBlockCollage(items=items, caption=caption)
        return types.PageBlockSlideshow(items=items, caption=caption)

    def _button_row(self, item: dict, depth: int) -> Any:
        types = _types()
        buttons = [self._button(raw) for raw in item["buttons"]]
        align = item.get("align", "left")
        return types.PageBlockButtonRow(
            buttons=buttons,
            align_left=(align == "left") or None,
            align_center=(align == "center") or None,
            align_right=(align == "right") or None)

    def _button(self, raw: dict) -> Any:
        types = _types()
        text = rich_text_from_dict(raw["text"])
        if "callback_data" in raw:
            data = str(raw["callback_data"]).encode("utf-8")
            if not 1 <= len(data) <= 64:
                raise ValueError("callback_data must be 1..64 UTF-8 bytes")
            button_type = types.InlineButtonTypeCallback(data=data)
        elif "url" in raw:
            url = str(raw["url"])
            if any(c.isspace() or ord(c) < 32 for c in url):
                raise ValueError("invalid button URL")
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https", "tg") or not parsed.netloc:
                raise ValueError("button URL must use HTTP, HTTPS or tg")
            button_type = types.InlineButtonTypeUrl(url=url)
        elif "copy_text" in raw:
            copy_text = str(raw["copy_text"])
            if not 1 <= len(copy_text) <= 256:
                raise ValueError("copy_text must be 1..256 characters")
            button_type = types.InlineButtonTypeCopy(copy_text=copy_text)
        elif "switch_inline_query" in raw:
            button_type = types.InlineButtonTypeSwitchInline(
                query=str(raw["switch_inline_query"]),
                same_peer=bool(raw.get("same_peer", False)) or None)
        elif "open_app" in raw:
            raise ValueError(
                "open_app buttons have no Telethon layer 233 equivalent "
                "(no InlineButtonTypeOpenApp)")
        else:
            raise ValueError("button requires exactly one action")
        style = _button_style(raw.get("style"))
        return types.PageButton(text=text, type=button_type, style=style)

    def _embed(self, item: dict, depth: int) -> Any:
        types = _types()
        poster_id = None
        if "poster" in item:
            poster_id = self.media_id(item["poster"], want="photo")
        return types.PageBlockEmbed(
            caption=_caption(item.get("caption")),
            full_width=bool(item.get("full_width", False)) or None,
            allow_scrolling=bool(item.get("allow_scrolling", False)) or None,
            url=item.get("url"), html=item.get("html"),
            poster_photo_id=poster_id,
            w=item.get("width"), h=item.get("height"))

    def _embed_post(self, item: dict, depth: int) -> Any:
        types = _types()
        date = datetime.datetime.fromtimestamp(int(item["date"]), tz=datetime.UTC)
        return types.PageBlockEmbedPost(
            url=str(item["url"]), webpage_id=int(item["webpage_id"]),
            author_photo_id=int(item["author_photo_id"]),
            author=str(item["author"]), date=date,
            blocks=self.blocks(item["blocks"], depth + 1),
            caption=_caption(item.get("caption")))


def _button_style(style_name: Any) -> Any:
    types = _types()
    if style_name is None:
        return None
    if style_name == "primary":
        return types.RichButtonStyle(bg_primary=True)
    if style_name == "danger":
        return types.RichButtonStyle(bg_danger=True)
    if style_name == "success":
        return types.RichButtonStyle(bg_success=True)
    if style_name == "link":
        return types.RichButtonStyle(link=True)
    raise ValueError("unsupported inline button style")


_BLOCK_BUILDERS = {
    "paragraph": _Builder._simple,
    "heading": _Builder._simple,
    "divider": _Builder._simple,
    "pre": _Builder._simple,
    "footer": _Builder._simple,
    "anchor": _Builder._simple,
    "mathematical_expression": _Builder._simple,
    "thinking": _Builder._simple,
    "photo": _Builder._media,
    "video": _Builder._media,
    "animation": _Builder._media,
    "audio": _Builder._media,
    "voice_note": _Builder._media,
    "document": _Builder._media,
    "details": _Builder._details,
    "list": _Builder._list,
    "blockquote": _Builder._quote,
    "pullquote": _Builder._quote,
    "table": _Builder._table,
    "map": _Builder._map,
    "collage": _Builder._gallery,
    "slideshow": _Builder._gallery,
    "button_row": _Builder._button_row,
    "embed": _Builder._embed,
    "embed_post": _Builder._embed_post,
}


def blocks_from_dict(blocks: Any, *, media: dict[str, Any] | None = None,
                     draft: bool = False) -> Any:
    """Build ``InputRichMessage(blocks=...)`` from a literal block list.

    ``media`` maps string media ids to ``ResolvedRichMedia`` (or raw
    ``InputPhoto`` / ``InputDocument``); media blocks reference those ids
    and the resolved objects land in ``photos`` / ``documents``.
    ``draft=True`` permits ``thinking`` blocks; sent messages reject them.
    """
    types = _types()
    validate_dict_blocks(blocks)
    builder = _Builder(media, draft)
    built = builder.blocks(blocks, 1)
    return types.InputRichMessage(
        blocks=built, noautolink=True,
        photos=list(builder.photos.values()) or None,
        documents=list(builder.documents.values()) or None)
