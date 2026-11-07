"""Chat-history reads for the Telegram MTProto adapter.

Kept out of ``telethon_adapter.py`` (already over the file-size cap): the
bounded chat/topic page read lives here as a mixin; the adapter only wires
it into its bases, so callers keep using ``adapter.list_chat_messages``.
"""
from __future__ import annotations

from typing import Any, Optional


class TelegramChatHistoryMixin:
    """Bounded reads of one chat's (or topic's) messages."""

    async def list_chat_messages(self, chat_id: Any, *, limit: int = 20,
                                 offset_id: int = 0, pinned_only: bool = False,
                                 query: Optional[str] = None, thread_id: Any = None) -> dict[str, Any]:
        """Read a bounded page from one chat/topic and return a cursor for scanned messages."""
        if self._client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        from telethon import types

        peer = await self._input_peer_for(chat_id)
        page_limit = max(1, min(int(limit), 50))
        kwargs: dict[str, Any] = {"limit": page_limit,
                                  "offset_id": max(0, int(offset_id))}
        search_query = str(query or "").strip()[:256] or None
        thread_filter = None
        if thread_id not in (None, ""):
            try:
                thread_filter = int(thread_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("thread_id must be a numeric Telegram topic ID") from exc
            if thread_filter <= 0:
                raise ValueError("thread_id must be positive")
            kwargs["reply_to"] = thread_filter
        # Telethon's GetRepliesRequest does not support server-side search/filter;
        # in a topic, apply those constraints to the bounded reply page below.
        if search_query and thread_filter is None:
            kwargs["search"] = search_query
        if pinned_only and thread_filter is None:
            kwargs["filter"] = types.InputMessagesFilterPinned()
        messages = await self._client.get_messages(peer, **kwargs)
        if messages is None:
            messages = []
        elif not isinstance(messages, (list, tuple)):
            messages = [messages]
        result = []
        next_offset_id = None
        for message in messages:
            if message is None or not getattr(message, "id", None):
                continue
            next_offset_id = str(message.id)
            message_thread_id = self._message_thread_id(message)
            if thread_filter is not None and message_thread_id != str(thread_filter) \
                    and int(message.id) != thread_filter:
                continue
            message_text = str(getattr(message, "message", None) or getattr(message, "text", None) or "")
            if thread_filter is not None and search_query \
                    and search_query.casefold() not in message_text.casefold():
                continue
            if thread_filter is not None and pinned_only and not bool(getattr(message, "pinned", False)):
                continue
            date = getattr(message, "date", None)
            result.append({
                "message_id": str(message.id),
                "thread_id": message_thread_id,
                "sender_id": str(getattr(message, "sender_id", "") or "") or None,
                "date": date.isoformat() if hasattr(date, "isoformat") else None,
                "text": message_text[:2000],
                "pinned": bool(getattr(message, "pinned", False)),
                "outgoing": bool(getattr(message, "out", False)),
            })
        return {"messages": result, "next_offset_id": next_offset_id,
                "scanned_count": len(messages)}



