"""Self-deleting Telethon messages, after Ultroid's eor/try_delete pattern.

One async helper, ``delete_after``, is plain-setattr'd onto Telethon's
``Message`` class (no registry, no patching system) and used for the
5-second self-deleting unknown-command message. The adapter's ``send``
schedules it through :func:`schedule_sent_message_self_delete` when the
send metadata carries ``telegram_self_delete_after_s``. Every failure is
swallowed (log at most) — the delete must never crash the turn.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.tl.custom.message import Message

logger = logging.getLogger(__name__)

#: Send-metadata key carrying the self-delete delay in seconds.
SELF_DELETE_METADATA_KEY = "telegram_self_delete_after_s"

#: Default self-delete delay for the unknown-command reply.
DEFAULT_SELF_DELETE_DELAY_S = 5.0


async def delete_after(self: Any, delay: float = DEFAULT_SELF_DELETE_DELAY_S) -> None:
    """Delete this message after ``delay`` seconds; never raise."""
    try:
        await asyncio.sleep(delay)
        await self.delete()
    except Exception:
        logger.debug("Telegram self-delete failed", exc_info=True)


def install_message_self_delete() -> None:
    """Patch ``delete_after`` onto Telethon's Message (plain setattr)."""
    Message.delete_after = delete_after


async def _self_delete_after(adapter: Any, chat_id: str, message_id: str, delay: float) -> None:
    message = None
    try:
        peer = adapter._peer_for(chat_id)
        message = await adapter._client.get_messages(peer, ids=int(message_id))
    except Exception:
        logger.debug("Telegram self-delete fetch failed", exc_info=True)
    if message is not None:
        try:
            await message.delete_after(delay)
            return
        except Exception:
            logger.debug("Telegram self-delete via message failed", exc_info=True)
    await asyncio.sleep(delay)
    try:
        await adapter.delete_message(chat_id, message_id)
    except Exception:
        logger.debug("Telegram self-delete by id failed", exc_info=True)


def schedule_sent_message_self_delete(
    adapter: Any, metadata: dict | None, chat_id: str, message_id: Any,
) -> Any:
    """Schedule deletion of a just-sent message; never raise.

    Reads the delay from ``metadata["telegram_self_delete_after_s"]``
    (seconds, positive only); no-ops otherwise. Fetches the sent message
    and deletes it through its patched ``delete_after``; if the fetch (or
    the helper) fails, falls back to deleting by id after the delay.
    """
    delay = (metadata or {}).get(SELF_DELETE_METADATA_KEY)
    if not isinstance(delay, (int, float)) or delay <= 0 or not message_id:
        return None
    task = asyncio.create_task(
        _self_delete_after(adapter, chat_id, str(message_id), float(delay)))
    tasks = getattr(adapter, "_background_tasks", None)
    if tasks is None:
        tasks = adapter._background_tasks = set()
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
