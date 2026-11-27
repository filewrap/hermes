"""Best-effort ephemeral MTProto sends, edits, deletes and silent answers.

Ephemeral messages live outside normal chat history: they are addressed to
one receiver (``receiver_id``) inside one peer (a DM or a group), and only
that receiver ever sees them. Every helper returns ``False`` instead of
raising, and none of them ever logs message text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EphemeralCallback:
    """The addressing context of one tap on an ephemeral message."""

    query_id: int
    message_id: int
    chat_id: str
    user_id: str


async def input_user_for(adapter: Any, user_id: Any) -> Any | None:
    """Resolve ``user_id`` to an ``InputUser`` for ephemeral addressing.

    Returns None when the id is not numeric or the peer cannot be resolved
    (the caller then falls back instead of sending to a wrong receiver).
    """
    from telethon import types

    raw = str(user_id or "")
    if not raw.lstrip("-").isdecimal():
        return None
    try:
        peer = await adapter._input_peer_for(raw)
    except Exception:
        logger.warning(
            "Could not resolve Telegram ephemeral receiver for user %s",
            raw, exc_info=True,
        )
        return None
    if not isinstance(peer, types.InputPeerUser):
        logger.warning(
            "Could not resolve Telegram ephemeral receiver for user %s: not a user peer",
            raw,
        )
        return None
    return types.InputUser(user_id=int(raw), access_hash=peer.access_hash)


async def send_ephemeral(
    adapter: Any, *, receiver_id: Any, peer: Any, text: str,
    reply_markup: Any = None, reply_to: Any = None,
) -> bool:
    """Send one ephemeral message; True when Telegram accepted it."""
    from telethon import functions

    from .mtproto_rich import input_rich_message

    try:
        request = functions.ephemeral.SendMessageRequest(
            receiver_id=receiver_id, message=text, peer=peer,
            rich_message=input_rich_message(text, rich_format="html"),
            reply_markup=reply_markup, reply_to=reply_to,
        )
        await adapter._mtproto_rich_request(request)
    except Exception:
        logger.warning("Could not send Telegram ephemeral message", exc_info=True)
        return False
    return True


async def edit_ephemeral(
    adapter: Any, *, receiver_id: Any, peer: Any, message_id: int, text: str,
    reply_markup: Any = None,
) -> bool:
    """Edit one ephemeral message in place; a missing reply_markup clears
    the keyboard (same flag-absent serialisation as the normal edit path)."""
    from telethon import functions

    from .mtproto_rich import input_rich_message

    try:
        request = functions.ephemeral.EditMessageRequest(
            receiver_id=receiver_id, id=int(message_id), peer=peer,
            message=text, rich_message=input_rich_message(text, rich_format="html"),
            reply_markup=reply_markup,
        )
        await adapter._mtproto_rich_request(request)
    except Exception:
        logger.warning("Could not edit Telegram ephemeral message", exc_info=True)
        return False
    return True


async def delete_ephemeral(
    adapter: Any, *, receiver_id: Any, peer: Any, message_id: int,
) -> bool:
    """Delete one ephemeral message; True when Telegram accepted it."""
    from telethon import functions

    try:
        request = functions.ephemeral.DeleteMessageRequest(
            receiver_id=receiver_id, id=int(message_id), peer=peer,
        )
        await adapter._mtproto_rich_request(request)
    except Exception:
        logger.warning("Could not delete Telegram ephemeral message", exc_info=True)
        return False
    return True


async def answer_ephemeral_callback(adapter: Any, query_id: int) -> bool:
    """Answer an ephemeral callback query silently: no text, no alert."""
    from telethon import functions

    try:
        await adapter._mtproto_rich_request(
            functions.messages.SetBotCallbackAnswerRequest(
                query_id=int(query_id), cache_time=0))
    except Exception:
        logger.warning(
            "Could not answer Telegram ephemeral callback query", exc_info=True)
        return False
    return True
