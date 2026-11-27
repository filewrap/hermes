"""Build and dispatch a model turn for an admitted peer bot.

The peer path shares the turn's event shape with the human path but never
touches human-only handling: no UI interception, no slash commands, no
personal memory scope. Peer text starting with ``/`` is model input.
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.platforms.event import MessageEvent, MessageType

from .telegram_event_context import group_trigger_text, telegram_event_channel_prompt

logger = logging.getLogger(__name__)


async def dispatch_peer_turn(adapter: Any, event: Any, source: Any, messages: list[Any],
                            text: str, *, closed_note: str = "") -> None:
    """Route one admitted peer message into the gateway turn path."""
    if closed_note:
        text = f"{closed_note}\n\n{text}"
    message = getattr(event, "message", None)
    cached_probe = getattr(event, "_hermes_skill_probe_media", None)
    if cached_probe is not None:
        paths, media_types, note = cached_probe
    else:
        paths, media_types, note = await adapter._cache_inbound_media(message)
    if note:
        text = f"{text}\n{note}".strip()
    if not text.strip() and not paths and not note:
        return
    if getattr(message, "photo", None):
        kind = MessageType.PHOTO
    elif getattr(message, "video", None):
        kind = MessageType.VIDEO
    elif getattr(message, "voice", None):
        kind = MessageType.VOICE
    elif getattr(message, "audio", None):
        kind = MessageType.AUDIO
    elif getattr(message, "sticker", None):
        kind = MessageType.STICKER
    elif getattr(message, "document", None) or paths:
        kind = MessageType.DOCUMENT
    else:
        kind = MessageType.TEXT
    reply_to = getattr(message, "reply_to_msg_id", None)
    quote_text = adapter._quoted_excerpt(message)
    reply_message, reply_sender, text = await adapter._resolve_reply_context(
        message, reply_to, paths, media_types, text)
    event_text = group_trigger_text(
        text, source.chat_type, messages, adapter._bot_username, adapter._bot_id)
    event_obj = MessageEvent(
        text=event_text, message_type=kind, user_id=source.user_id, user_name=source.user_name,
        source=source, message_id=str(getattr(message, "id", "")) or None,
        channel_prompt=telegram_event_channel_prompt(
            adapter.config.extra, source, adapter._telegram_identity_prompt()),
        media_urls=paths, media_types=media_types, raw_message=message,
        reply_to_message_id=str(reply_to) if reply_to else None,
        reply_to_text=str(getattr(reply_message, "message", "") or "") or None,
        quoted_text=quote_text,
        reply_to_author_id=(str(getattr(reply_message, "sender_id", "")) or None) if reply_message else None,
        reply_to_author_name=(str(getattr(reply_sender, "username", None)
                                  or getattr(reply_sender, "first_name", "") or "") or None),
        reply_to_is_own_message=bool(reply_message and getattr(reply_message, "sender_id", None) == adapter._bot_id),
        metadata={"thread_id": source.thread_id} if source.thread_id else {},
        allow_gateway_control=False,
    )
    event_obj._b2b_admitted = bool(getattr(event, "_b2b_admitted", False))
    event_obj._b2b_secure = bool(getattr(event, "_b2b_secure", False))
    from .incoming_reactions import react_to_incoming
    await react_to_incoming(adapter, event_obj)
    await adapter.handle_message(event_obj)
