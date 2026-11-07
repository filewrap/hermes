"""Best-effort intent reactions for admitted Telegram text, never model turns.

The normal MTProto path passes a normalized ``MessageEvent`` only after sender
authorization and group-trigger checks. Isolated tenant routing can call this
helper with the raw Telethon event, but must explicitly confirm admission; the
helper then resolves the same routed profile/user preference as ``/settings``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from gateway.telegram_personal_settings import load_personal_settings
from .intent_reactions import choose_reaction, explicit_reaction_request

logger = logging.getLogger(__name__)
_REACTION_TIMEOUT_SECONDS = 2.0


def _is_normalized_message(event: Any) -> bool:
    return hasattr(event, "source") and hasattr(event, "message_type")


def _message_type_is_text(event: Any) -> bool:
    value = getattr(event, "message_type", None)
    return getattr(value, "value", value) == "text"


async def _telethon_reply_from_our_bot(adapter: Any, event: Any, raw_message: Any) -> str:
    """Return text from the one explicitly replied-to bot message, if available.

    This mirrors the normalized Telethon path's context contract: a reply is
    useful only when it points to this bot's own prior message. It deliberately
    resolves only the direct reply and never searches chat history.
    """
    target_message_id = getattr(raw_message, "reply_to_msg_id", None)
    if not target_message_id:
        return ""
    bot_id = getattr(adapter, "_bot_id", None)
    if bot_id is None:
        return ""
    reply_message = getattr(event, "reply_to_message", None)
    if reply_message is None:
        get_reply_message = getattr(event, "get_reply_message", None)
        if not callable(get_reply_message):
            return ""
        try:
            reply_message = await asyncio.wait_for(
                get_reply_message(), timeout=_REACTION_TIMEOUT_SECONDS,
            )
        except Exception:
            return ""
    if (getattr(reply_message, "id", None) != target_message_id
            or getattr(reply_message, "sender_id", None) != bot_id):
        return ""
    reply_text = getattr(reply_message, "message", "")
    return reply_text if isinstance(reply_text, str) else ""


async def react_to_incoming(
    adapter: Any,
    event: Any,
    *,
    source: Any = None,
    admitted: bool | None = None,
) -> bool:
    """Apply one selected reaction to an admitted text message, returning success.

    ``MessageEvent`` is accepted directly because its caller runs after access,
    group-trigger, and private-settings dispatch. Raw Telethon events are useful
    before isolated-tenant dispatch has converted them to ``MessageEvent``; a
    caller must pass the already-authorized ``source`` and ``admitted=True``.
    No profile home is accepted from the caller: preferences are always resolved
    through the adapter's source-aware settings UI, preventing cross-profile
    reads. Settings and transport failures are cosmetic and never escape.
    """
    normalized = _is_normalized_message(event)
    if normalized:
        source = source or getattr(event, "source", None)
        if not _message_type_is_text(event):
            return False
        raw_message = getattr(event, "raw_message", None)
        text = getattr(event, "text", "")
        message_id = getattr(event, "message_id", None)
        reply_text = (
            getattr(event, "reply_to_text", "") or ""
            if getattr(event, "reply_to_is_own_message", False) else ""
        )
    else:
        # Raw events lack the normal path's admission guarantee. Fail closed
        # unless caller confirms authorization and group-trigger admission.
        if admitted is not True or source is None:
            return False
        raw_message = getattr(event, "message", None)
        if raw_message is None:
            raw_message = event
        text = getattr(raw_message, "message", "") or ""
        message_id = getattr(raw_message, "id", None)
        reply_text = ""
        # Do not react to media captions or non-text Telethon updates.
        if any(getattr(raw_message, field, None) is not None for field in
               ("photo", "video", "voice", "audio", "sticker", "document", "media")):
            return False

    user_id = str(getattr(source, "user_id", "") or "")
    if (not user_id.isdigit() or int(user_id) <= 0
            or getattr(source, "is_bot", False)
            or getattr(source, "chat_type", "") == "channel"
            or not getattr(source, "chat_id", None)
            or not message_id):
        return False
    # Never attribute forwarded content to its forwarder. Slash commands and
    # other unsuitable/uncertain intents produce no emoji in the selector.
    if getattr(raw_message, "fwd_from", None):
        return False
    if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("/"):
        return False

    try:
        home = adapter._settings_ui()._home(source)
        preferences = load_personal_settings(home, user_id)
    except Exception as exc:
        logger.warning("Telegram intent reaction preference lookup failed (%s)", type(exc).__name__)
        return False
    if (not preferences.get("reactions_enabled", True)
            or not preferences.get("text_reactions_enabled", False)):
        return False

    if not normalized:
        reply_text = await _telethon_reply_from_our_bot(adapter, event, raw_message)
    emoji = choose_reaction(text, reply_text=reply_text)
    if emoji is None:
        return False
    try:
        delivered = await asyncio.wait_for(
            adapter.send_reaction(source.chat_id, message_id, emoji),
            timeout=_REACTION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("Telegram intent reaction delivery timed out")
        return False
    except Exception as exc:
        # Do not log exception text/tracebacks here: transport errors can embed
        # message or peer details. A class-only warning is useful and safe.
        logger.warning("Telegram intent reaction delivery failed (%s)", type(exc).__name__)
        return False
    if delivered is False:
        # Native adapter send_reaction is itself best-effort and returns False
        # after handling its API exception; surface that otherwise silent path.
        logger.warning("Telegram intent reaction delivery was rejected")
        return False
    return True


async def react_to_telethon_incoming(adapter: Any, event: Any, source: Any) -> bool:
    """React to an already-admitted raw Telethon event before isolated dispatch.

    The adapter must call this only after authorization, group-trigger checks,
    and any private UI/command early returns. It deliberately does not perform
    authorization itself; the routed ``source`` carries the trusted profile
    scope used to load the per-user preference.
    """
    return await react_to_incoming(adapter, event, source=source, admitted=True)


async def handle_explicit_reaction_request(adapter: Any, event: Any, source: Any, text: str) -> bool | None:
    """Execute the exact `React to this message <emoji>` request without an agent turn.

    Call only after normal Telegram authorization and group-trigger admission. A
    direct reply targets the replied-to message; otherwise the request message
    itself is the target. ``None`` means this was not an explicit request.
    """
    emoji = explicit_reaction_request(text)
    if emoji is None:
        return None
    raw_message = getattr(event, "message", None) or event
    current_id = getattr(raw_message, "id", None)
    reply_id = getattr(raw_message, "reply_to_msg_id", None)
    if reply_id in (None, ""):
        reply_header = getattr(raw_message, "reply_to", None)
        reply_id = getattr(reply_header, "reply_to_msg_id", None)
    topic_id = str(getattr(source, "thread_id", "") or "")
    # Topic-root headers are routing context rather than an explicit reply.
    target_id = reply_id if reply_id not in (None, "") and str(reply_id) != topic_id else current_id
    if not str(target_id or "").isdecimal():
        logger.warning("Telegram explicit reaction request had no usable message ID")
        return True
    try:
        delivered = await asyncio.wait_for(
            adapter.send_reaction(source.chat_id, str(target_id), emoji),
            timeout=_REACTION_TIMEOUT_SECONDS,
        )
        if delivered is False:
            logger.warning("Telegram explicit reaction delivery was rejected")
    except TimeoutError:
        logger.warning("Telegram explicit reaction delivery timed out")
    except Exception as exc:
        logger.warning("Telegram explicit reaction delivery failed (%s)", type(exc).__name__)
    return True
