"""Native Telegram widget helpers for the Telethon/MTProto adapter.

Widgets are layer-233 HTML messages: one self-contained HTML document with
inline CSS and JavaScript, delivered as the media of a message
(``messageMediaWidget``). The HTML is fixed at send time; only the two
state strings (``chat_data`` for this message, ``public_data`` for the
shared object) can change afterwards, and only the bot can change them.

This module is pure request construction + send-boundary validation, in the
same shape as ``mtproto_rich.py``: no transport, no rendering (rendering is
the recipient's layer-233 beta client), no wallet (framework-only, no
server API). The adapter owns the client calls; the tap loop lives in
``widget_queries.py``.

Validation is LOCAL ONLY — no validation API is ever called. The limits
below are the server/profile numbers from the widget guides; a widget
that breaks them is refused here with a clear error instead of being
sent as garbage the client silently drops. The widget-sandbox local
harness (``sandbox-node.ts``) is deliberately NOT wired in: it is a
dev-time pre-flight (needs pnpm/tsx/headless Chromium), not something a
gateway process should spawn per send — the Python size checks are the
send boundary.
"""
from __future__ import annotations

import zlib
from typing import Any, Optional

# Server limits (widget guides §16 / companion §1, §15).
MAX_WIDGET_HTML_RAW_BYTES = 32_768
MAX_WIDGET_HTML_ZLIB_BYTES = 4_096
MAX_WIDGET_STATE_BYTES = 1_024
MAX_WIDGET_REQUEST_BYTES = 64
MAX_WIDGET_RESULT_BYTES = 1_024
# Bubble anchors: 0 means auto on that axis (guide §5.1).
MIN_WIDGET_WIDTH = 200
MAX_WIDGET_WIDTH = 600
MIN_WIDGET_HEIGHT = 50
MAX_WIDGET_HEIGHT = 600


def _utf8_bytes(value: str) -> bytes:
    return str(value).encode("utf-8")


def validate_widget_html(html: Any) -> str:
    """Return the raw HTML string, refusing anything over the size limits.

    ``html`` is the RAW string — never zlib it first; the server compresses
    it and refuses anything over 4,096 bytes compressed / 32,768 raw.
    """
    if not isinstance(html, str):
        raise TypeError("Widget HTML must be a string")
    raw = html.encode("utf-8")
    if len(raw) > MAX_WIDGET_HTML_RAW_BYTES:
        raise ValueError(
            f"Widget HTML is {len(raw):,} bytes raw, over the "
            f"{MAX_WIDGET_HTML_RAW_BYTES:,} byte limit"
        )
    compressed = len(zlib.compress(raw, 9))
    if compressed > MAX_WIDGET_HTML_ZLIB_BYTES:
        raise ValueError(
            f"Widget HTML compresses to {compressed:,} bytes (zlib level 9), "
            f"over the {MAX_WIDGET_HTML_ZLIB_BYTES:,} byte limit"
        )
    return html


def validate_widget_state(data: Any, *, field: str) -> Optional[str]:
    """Return a state string, refusing anything over 1,024 bytes UTF-8.

    ``None`` passes through (no state for that field). State carries no NUL
    bytes. ``field`` is ``"public_data"`` or ``"chat_data"`` and names the
    refusal.
    """
    if data is None:
        return None
    if not isinstance(data, str):
        raise TypeError(f"Widget {field} must be a string")
    if "\x00" in data:
        raise ValueError(f"Widget {field} must not contain NUL bytes")
    size = len(_utf8_bytes(data))
    if size > MAX_WIDGET_STATE_BYTES:
        raise ValueError(
            f"Widget {field} is {size:,} bytes UTF-8, over the "
            f"{MAX_WIDGET_STATE_BYTES:,} byte limit"
        )
    return data


def validate_widget_result(result: Any) -> Optional[str]:
    """Return an answer result, refusing anything over 1,024 bytes.

    ``None`` passes through: it is the empty answer every unservable tap
    gets. The answer carries no state — a tap that should change what the
    widget shows needs a state edit, not a bigger answer.
    """
    if result is None:
        return None
    if not isinstance(result, str):
        raise TypeError("Widget query answer result must be a string")
    if "\x00" in result:
        raise ValueError("Widget query answer result must not contain NUL bytes")
    size = len(_utf8_bytes(result))
    if size > MAX_WIDGET_RESULT_BYTES:
        raise ValueError(
            f"Widget query answer is {size:,} bytes UTF-8, over the "
            f"{MAX_WIDGET_RESULT_BYTES:,} byte limit"
        )
    return result


def validate_widget_dimensions(width: int, height: int) -> tuple[int, int]:
    """Return a (width, height) pair, refusing out-of-range fixed anchors.

    ``0`` means auto on that axis; a fixed width must be 200–600 px and a
    fixed height 50–600 px (guide §5.1).
    """
    width = int(width)
    height = int(height)
    if width != 0 and not MIN_WIDGET_WIDTH <= width <= MAX_WIDGET_WIDTH:
        raise ValueError(
            f"Widget width must be 0 (auto) or {MIN_WIDGET_WIDTH}–{MAX_WIDGET_WIDTH}, got {width}"
        )
    if height != 0 and not MIN_WIDGET_HEIGHT <= height <= MAX_WIDGET_HEIGHT:
        raise ValueError(
            f"Widget height must be 0 (auto) or {MIN_WIDGET_HEIGHT}–{MAX_WIDGET_HEIGHT}, got {height}"
        )
    return width, height


def is_broadcast_channel(entity: Any) -> bool:
    """Return True when a resolved chat entity is a broadcast channel.

    Supergroups are channels in MTProto too, but they are not broadcast
    channels: a ``Channel`` with ``megagroup`` set is a group and may receive
    widgets. Only a broadcast channel (``Channel`` without ``megagroup``)
    refuses widget originals — channels get forwards only.
    """
    if type(entity).__name__ != "Channel":
        return False
    return not bool(getattr(entity, "megagroup", False))


def build_widget_media(html: str, *, width: int = 0, height: int = 0,
                       allow_queries: Optional[bool] = None,
                       shared_widget: Optional[bool] = None,
                       public_data: Optional[str] = None,
                       chat_data: Optional[str] = None) -> Any:
    """Build the widget media for a send.

    ``allow_queries=True`` makes the widget interactive; ``shared_widget=True``
    together with it is what lets forwards ask the bot (there is no separate
    ``fwd_allow_queries`` parameter). ``html`` is validated raw — never zlib.
    """
    from telethon import types

    width, height = validate_widget_dimensions(width, height)
    return types.InputMediaWidget(
        width=width,
        height=height,
        html=validate_widget_html(html),
        allow_queries=allow_queries,
        shared_widget=shared_widget,
        public_data=validate_widget_state(public_data, field="public_data"),
        chat_data=validate_widget_state(chat_data, field="chat_data"),
    )


def build_widget_media_by_id(widget_id: Any, *,
                             chat_data: Optional[str] = None) -> Any:
    """Build the media to send an existing widget again by its id.

    A new message carrying the same shared object: edit its ``public_data``
    once and every copy follows.
    """
    from telethon import types

    widget_id = int(widget_id)
    if widget_id <= 0:
        raise ValueError("Widget id must be a positive integer")
    return types.InputMediaWidgetId(
        widget_id=widget_id,
        chat_data=validate_widget_state(chat_data, field="chat_data"),
    )


def send_widget_request(peer: Any, media: Any, *, message: str = "",
                        entities: Optional[Any] = None,
                        reply_to: Optional[int] = None,
                        top_msg_id: Optional[int] = None,
                        reply_markup: Optional[Any] = None,
                        silent: Optional[bool] = None,
                        noforwards: Optional[bool] = None) -> Any:
    """Create a ``messages.sendMedia`` request carrying a widget.

    The caption is plain formatted text (``message`` + ``entities``) — a
    widget message has no rich caption, and ``SendMediaRequest`` takes no
    ``rich_message``. ``reply_markup`` is a classic inline keyboard below
    the widget; its taps arrive as ordinary callback queries, not widget
    queries.
    """
    from telethon import functions

    from .mtproto_rich import _validate_entity_spans
    from .topic_delivery import topic_reply_header

    text = str(message or "")
    if entities:
        _validate_entity_spans(text, entities)
    reply_header = topic_reply_header(reply_to, top_msg_id)
    return functions.messages.SendMediaRequest(
        peer=peer,
        media=media,
        message=text,
        entities=list(entities) if entities else None,
        reply_to=reply_header,
        reply_markup=reply_markup,
        silent=silent,
        noforwards=noforwards,
    )


def upload_widget_request(peer: Any, media: Any) -> Any:
    """Create a ``messages.uploadMedia`` request for a widget.

    Uploads without sending; the returned ``MessageMediaWidget`` carries the
    new ``widget_id``, for callers that need the id before the first send.
    """
    from telethon import functions

    return functions.messages.UploadMediaRequest(peer=peer, media=media)


def build_inline_widget_message(html: str, *, message: str, width: int = 0,
                                height: int = 0,
                                allow_queries: Optional[bool] = None,
                                shared_widget: Optional[bool] = None,
                                public_data: Optional[str] = None,
                                chat_data: Optional[str] = None,
                                reply_markup: Optional[Any] = None,
                                entities: Optional[Any] = None) -> Any:
    """Build a widget as an inline (via-bot) result message."""
    from telethon import types

    width, height = validate_widget_dimensions(width, height)
    return types.InputBotInlineMessageWidget(
        width=width,
        height=height,
        html=validate_widget_html(html),
        message=str(message or ""),
        allow_queries=allow_queries,
        shared_widget=shared_widget,
        public_data=validate_widget_state(public_data, field="public_data"),
        chat_data=validate_widget_state(chat_data, field="chat_data"),
        reply_markup=reply_markup,
        entities=list(entities) if entities else None,
    )


def build_inline_widget_message_by_id(widget_id: Any, *, message: str,
                                      chat_data: Optional[str] = None,
                                      reply_markup: Optional[Any] = None,
                                      entities: Optional[Any] = None) -> Any:
    """Build a via-bot widget result message by an existing widget id."""
    from telethon import types

    widget_id = int(widget_id)
    if widget_id <= 0:
        raise ValueError("Widget id must be a positive integer")
    return types.InputBotInlineMessageWidgetId(
        widget_id=widget_id,
        message=str(message or ""),
        chat_data=validate_widget_state(chat_data, field="chat_data"),
        reply_markup=reply_markup,
        entities=list(entities) if entities else None,
    )


def answer_widget_query_request(query_id: Any, result: Optional[str] = None) -> Any:
    """Create a ``messages.setBotWidgetQueryAnswer`` request.

    ``result=None`` is the empty answer: the correct reply to a tap the bot
    cannot serve. Never leave a tap unanswered past the 15 s window.
    """
    from telethon import functions

    return functions.messages.SetBotWidgetQueryAnswerRequest(
        query_id=int(query_id),
        result=validate_widget_result(result),
    )


def edit_message_widget_data_request(peer: Any, message_id: Any, *,
                                     public_data: Optional[str] = None,
                                     chat_data: Optional[str] = None) -> Any:
    """Create a ``messages.editMessageWidgetData`` request for the original.

    Only the bot that sent it, only the original — never a forward. Editing
    a shared widget's ``public_data`` here writes its shared object.
    """
    from telethon import functions

    public_data = validate_widget_state(public_data, field="public_data")
    chat_data = validate_widget_state(chat_data, field="chat_data")
    if public_data is None and chat_data is None:
        raise ValueError("A widget state edit needs public_data or chat_data")
    return functions.messages.EditMessageWidgetDataRequest(
        peer=peer,
        id=int(message_id),
        public_data=public_data,
        chat_data=chat_data,
    )


def edit_inline_widget_data_request(inline_message_id: Any, *,
                                    public_data: Optional[str] = None,
                                    chat_data: Optional[str] = None) -> Any:
    """Create a ``messages.editInlineBotMessageWidgetData`` request.

    Addresses a via-bot message by its ``inline_message_id``; ``chat_data``
    is allowed here — a via-bot message is an original.
    """
    from telethon import functions, types

    if not isinstance(inline_message_id, types.InputBotInlineMessageID):
        raise TypeError("Inline widget edits need an InputBotInlineMessageID")
    public_data = validate_widget_state(public_data, field="public_data")
    chat_data = validate_widget_state(chat_data, field="chat_data")
    if public_data is None and chat_data is None:
        raise ValueError("A widget state edit needs public_data or chat_data")
    return functions.messages.EditInlineBotMessageWidgetDataRequest(
        id=inline_message_id,
        public_data=public_data,
        chat_data=chat_data,
    )


def edit_shared_widget_data_request(widget_id: Any, public_data: str, *,
                                    cache_time: Optional[int] = None) -> Any:
    """Create a ``messages.editWidgetData`` request for a shared object.

    Works by ``widget_id`` even when every carrier message is deleted or
    unknown to the bot. ``cache_time`` is the only way to set the object's
    period (``0`` resets it to 30 s). At most one edit per second per object.
    """
    from telethon import functions

    widget_id = int(widget_id)
    if widget_id <= 0:
        raise ValueError("Widget id must be a positive integer")
    if cache_time is not None:
        cache_time = int(cache_time)
        if cache_time < 0:
            raise ValueError("Widget cache_time must not be negative")
    return functions.messages.EditWidgetDataRequest(
        widget_id=widget_id,
        public_data=validate_widget_state(public_data, field="public_data"),
        cache_time=cache_time,
    )


def get_widget_public_data_request(peer: Any, message_id: Any, *,
                                   hash: int = 0) -> Any:
    """Create a ``messages.getWidgetPublicData`` request.

    ``hash=0`` forces a full read; a current hash returns
    ``WidgetPublicDataNotModified`` instead.
    """
    from telethon import functions

    return functions.messages.GetWidgetPublicDataRequest(
        peer=peer,
        msg_id=int(message_id),
        hash=int(hash),
    )


def widget_html_from_media(media: Any) -> str:
    """Return the HTML of a received widget message's media.

    The payload is a zlib stream (RFC 1950) — ``zlib.decompress``, not gzip,
    not raw deflate.
    """
    from telethon import types

    if not isinstance(media, types.MessageMediaWidget):
        raise TypeError("Widget HTML needs a MessageMediaWidget media")
    return zlib.decompress(bytes(media.payload)).decode("utf-8")


def sent_widget_info(result: Any) -> tuple[Optional[str], Optional[int]]:
    """Return (message_id, widget_id) for a sent widget, from its Updates.

    The sent message comes back in the result's updates; its media is a
    ``MessageMediaWidget`` carrying the ``widget_id``. ``(None, None)``
    when no widget message is in the updates (the send itself still happened).
    """
    from telethon import types

    updates = getattr(result, "updates", None) or []
    for update in updates:
        message = getattr(update, "message", None)
        media = getattr(message, "media", None)
        if isinstance(media, types.MessageMediaWidget):
            message_id = getattr(message, "id", None)
            return (
                str(message_id) if message_id is not None else None,
                getattr(media, "widget_id", None),
            )
    return None, None
