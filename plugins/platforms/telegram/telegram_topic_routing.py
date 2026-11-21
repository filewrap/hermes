"""Route a client-created Telegram DM topic's thread-less opener into that topic (#136567).

Telegram for Android delivers a client-created DM topic's opening message with no
``message_thread_id``; the ``forum_topic_created`` service message is the only update naming the
new topic. Without this, lobby recovery pins the opener to the user's most-recent bound topic.

Telethon shape: ``NewMessage`` never fires for service messages (``MessageService`` is not a
``Message``), so the creation is recorded from a ``Raw(UpdateNewMessage)`` handler reading the
``MessageActionTopicCreate`` action, and adopted in ``_process_inbound_event`` right after
``_source()``. The 5 s follow window is measured on ``message.date`` stamps, never handler time.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("plugins.platforms.telegram.adapter")


class TelegramTopicRoutingMixin:
    _NEW_TOPIC_FOLLOW_SECONDS = 5.0
    _NEW_TOPIC_OPENER_MAX_PENDING = 256
    _NEW_TOPIC_RECORD_TTL_SECONDS = 600.0

    @staticmethod
    def _creator_key(sender_id) -> str:
        return str(sender_id or "")

    @staticmethod
    def _sent_at(message) -> Optional[float]:
        date = getattr(message, "date", None)
        return date.timestamp() if hasattr(date, "timestamp") else None

    def _record_created_topic(self, *, chat_id, sender_id, thread_id, sent_at) -> None:
        """Record an authorized, non-bot private-chat topic creation.

        Routing signal only, never an agent turn; the name is NOT cached in any topic catalog
        (operator-declared topics only)."""
        pending = self.__dict__.setdefault("_pending_created_topics", {})
        now = time.monotonic()
        for key in [k for k, rec in pending.items()
                    if now - rec[2] > self._NEW_TOPIC_RECORD_TTL_SECONDS]:
            del pending[key]
        while len(pending) >= self._NEW_TOPIC_OPENER_MAX_PENDING:
            del pending[min(pending, key=lambda k: pending[k][2])]
        pending[(str(chat_id), self._creator_key(sender_id))] = (str(thread_id), sent_at, now)

    def _adopt_created_topic_thread(self, message, chat_id, chat_type, thread_id):
        pending = self.__dict__.get("_pending_created_topics")
        if chat_type != "dm" or not pending:
            return thread_id
        sender_id = getattr(message, "sender_id", None)
        key = (str(chat_id), self._creator_key(sender_id))
        record = pending.get(key)
        sent_at = self._sent_at(message)
        if record is None or sent_at is None or sent_at < record[1]:
            return thread_id
        del pending[key]
        if thread_id is not None or getattr(getattr(message, "reply_to", None), "reply_to_top_id", None) is not None:
            return thread_id
        created_thread_id, last_sent_at, _recorded = record
        if sent_at - last_sent_at > self._NEW_TOPIC_FOLLOW_SECONDS:
            return thread_id
        pending[key] = (created_thread_id, sent_at, time.monotonic())
        return created_thread_id
