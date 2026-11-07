"""MTProto topic addressing and request-correlated send receipts.

An explicit reply anchor is valid only if Telegram reads it back in the exact
peer and topic being addressed. Missing, deleted, and cross-topic anchors fail
closed instead of silently becoming replies in General or another topic.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def topic_reply_header(reply_to: int | None, topic_id: int | None, *,
                       quote_text: str | None = None, quote_entities: Any = None,
                       quote_offset: int | None = None) -> Any:
    """Reply header for a topic send, optionally quoting an excerpt.

    ``quote_text`` must be an excerpt of the replied-to message; callers
    validate that against the observed message before building the header.
    ``quote_offset`` is the excerpt's offset inside the original message.
    """
    from telethon import types

    topic_id = int(topic_id) if topic_id is not None else None
    target = reply_to if reply_to is not None else topic_id
    if target is None:
        return None
    # Always carry top_msg_id for a real topic, including a send addressed
    # directly at the topic root (target == topic_id). Omitting it there
    # made root-addressed sends land outside the topic in threaded DMs, and
    # broke the delivery verification that reads the echoed reply_to_top_id
    # back (verified_topic_reply / sent_message_id): a card sent without it
    # could never be edited again. Replying to the root message with
    # top_msg_id set is how clients normally post inside a topic.
    top_msg_id = int(topic_id) if topic_id not in (None, 1) else None
    return types.InputReplyToMessage(
        reply_to_msg_id=int(target),
        top_msg_id=top_msg_id,
        quote_text=quote_text,
        quote_entities=quote_entities,
        quote_offset=quote_offset,
    )


async def verified_topic_reply(adapter: Any, peer: Any, reply_to: int | None,
                               topic_id: int, metadata: dict) -> int | None:
    """Verify explicit anchors; only an omitted anchor addresses the topic root."""
    if reply_to in (None, topic_id):
        return topic_id
    getter = getattr(adapter._client, "get_messages", None)
    if callable(getter):
        try:
            message = await getter(peer, ids=int(reply_to))
            if (getattr(message, "id", None) == int(reply_to)
                    and _same_peer(getattr(message, "peer_id", None), peer)
                    and adapter._message_thread_id(message) == str(topic_id)):
                return int(reply_to)
            if (getattr(message, "id", None) == int(reply_to)
                    and _same_peer(getattr(message, "peer_id", None), peer)
                    and getattr(message, "out", False)
                    and getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None) == int(topic_id)):
                # Threaded DMs may echo no top id: our own message replying
                # to the topic root is in the topic. Without this every
                # progress-card edit is refused and the card never updates.
                return int(reply_to)
        except Exception as exc:
            logger.warning("Could not verify Telegram topic reply anchor (%s)", type(exc).__name__)
    # Missing/deleted/cross-peer/cross-topic anchors are never repaired by
    # substituting the root: that would silently change the user's reply target.
    return None


def _same_peer(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return False
    try:
        from telethon.utils import get_peer_id
        return int(get_peer_id(actual)) == int(get_peer_id(expected))
    except (TypeError, ValueError):
        return False


def sent_message_id(response: Any, *, random_id: int | None = None,
                    peer: Any = None, topic_id: int | None = None) -> str | None:
    """Handle native Updates/UpdateShort envelopes without borrowing unrelated IDs."""
    if response is None:
        return None
    direct = getattr(response, "id", None)
    if direct is not None and type(response).__name__ != "UpdateMessageID":
        return str(direct)
    pending = [response]
    seen = set()
    messages = []
    while pending:
        item = pending.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if type(item).__name__ == "UpdateMessageID":
            if random_id is None or getattr(item, "random_id", None) == random_id:
                return str(item.id)
        message = getattr(item, "message", None)
        if message is not None and not isinstance(message, str):
            messages.append(message)
        pending.extend(getattr(item, "updates", ()) or ())
        nested = getattr(item, "update", None)
        if nested is not None:
            pending.append(nested)
    for message in messages:
        if random_id is None and getattr(message, "id", None) is not None:
            return str(message.id)
        # Some native replies contain just an outgoing UpdateNewMessage. Accept
        # it only in the requested peer/topic, never the first update in a batch.
        if not getattr(message, "out", False) or peer is None:
            continue
        from telethon.utils import get_peer_id
        try:
            if get_peer_id(message.peer_id) != get_peer_id(peer):
                continue
        except (TypeError, ValueError):
            continue
        reply = getattr(message, "reply_to", None)
        actual_topic = getattr(reply, "reply_to_top_id", None)
        if actual_topic is None and getattr(reply, "forum_topic", False):
            actual_topic = getattr(reply, "reply_to_msg_id", None)
        if actual_topic == topic_id:
            return str(message.id)
        # Threaded DMs may echo neither a top id nor a forum flag: an
        # outgoing message that replies to the topic root is in the topic.
        # Without this the card send returns no ID, the scheduler disables
        # the card, and it freezes at its initial state forever.
        if topic_id is not None and getattr(reply, "reply_to_msg_id", None) == topic_id:
            return str(message.id)
    return None
