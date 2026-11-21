"""Callback-query answers that survive a tap on an expired Telegram card.

Telethon port of upstream's PTB-shaped ``callback_answer`` (new in the
v0.23.x gap): the behaviour is identical, the transport is MTProto —
no ``telegram`` (python-telegram-bot) import anywhere in this file.

Telegram refuses ``SetBotCallbackAnswer`` once the query is older than its
window (``QUERY_ID_INVALID``). The fork's handlers awaited
``event.answer()`` unguarded, so a tap on a card that had been sitting in
the chat raised out of the handler: the outer ``except`` in
``TelethonAdapter._on_callback_query`` only debug-logs it, whatever the
handler had left to do never ran (a model switch kept its picker state,
since the pop sits after the toast), and the tap produced no feedback at
all. Verified against installed Telethon 1.45.0:
``CallbackQuery.Event.answer()`` sends ``SetBotCallbackAnswerRequest``
and raises ``telethon.errors.rpcerrorlist.QueryIdInvalidError`` on an
expired query; a second answer is a silent no-op via the event's
``_answered`` flag.

``answer_callback`` is the one choke point for those answers. A refused
answer is reported as ``False`` instead of raising. ``answer_or_note``
covers taps on a menu that has already expired — the one case where the
toast was the only thing the user would have seen, and "Picker expired —
use /model again." is still a usable next step as a normal message, posted
in the chat and forum topic the card lives in (via the adapter's normal
``send``; the fork has no ``_send_control_message``). Every other error
keeps raising: a broken answer must stay visible.

Wiring (not done here — ``telethon_adapter.py`` is owned by the sync
coordinator): replace the unguarded ``await event.answer(...)`` calls in
``TelethonAdapter._on_callback_query`` and
``hm_action_callbacks.handle_hm_action_callback`` with
``await answer_callback(event, ...)``, and use ``answer_or_note`` for
expired-menu taps (model picker / sessions pages) so the user still gets
the next step as a chat message.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Telegram truncates a callback-query answer's text at 200 chars.
TOAST_LIMIT = 200

# Wording of the errors that mean "this tap arrived after the query expired".
_STALE_QUERY_PHRASES = (
    "query is too old",
    "query id is invalid",
    "query_id_invalid",
    "response timeout expired",
)


def is_stale_query_error(exc: BaseException) -> bool:
    """True when *exc* is Telegram refusing an answer because the callback query expired.

    Matched on the wording first (cheap, and it works where Telethon is not
    importable), then narrowed to Telethon's ``QueryIdInvalidError`` when the
    library is present so an unrelated failure that happens to carry the
    text still fails loudly.
    """
    text = str(exc).lower()
    if not any(phrase in text for phrase in _STALE_QUERY_PHRASES):
        return False
    try:
        from telethon.errors.rpcerrorlist import QueryIdInvalidError
    except ImportError:
        return True
    return isinstance(exc, QueryIdInvalidError)


async def answer_callback(
    event: Any, text: Optional[str] = None, *, show_alert: bool = False
) -> bool:
    """Ack a button tap; ``False`` when Telegram refused it because the query expired.

    ``event`` is a Telethon ``CallbackQuery`` event; its ``answer()`` is a
    no-op on a second call (``_answered`` flag), which also reads as success.
    """
    try:
        if text is None:
            await event.answer()
        elif show_alert:
            await event.answer(text, alert=True)
        else:
            await event.answer(text)
    except Exception as exc:
        if not is_stale_query_error(exc):
            raise
        logger.debug(
            "[Telegram] callback answer refused for an expired query", exc_info=True
        )
        return False
    return True


async def answer_badge_callback(event: Any, data: str) -> bool:
    """Answer a ``badge:<type>`` tap with its toast from ``_BADGE_TOASTS``.

    The badge is a status indicator: the tap only ever answers a short
    toast, silently (never an alert). An unknown type answers empty and
    never invents a toast.
    """
    from .rich_ui import _BADGE_TOASTS

    toast = _BADGE_TOASTS.get(str(data or "").partition(":")[2].strip())
    return await answer_callback(event, toast)


async def answer_or_note(adapter: Any, event: Any, text: str) -> None:
    """Answer an expired-menu tap; when Telegram refused the late answer, post *text* as a normal
    message through the adapter's topic-aware ``send``, in the tapped card's forum topic."""
    message = getattr(event, "message", None)
    chat_id = getattr(event, "chat_id", None) or getattr(message, "chat_id", None)
    if await answer_callback(event, text) or chat_id is None:
        return
    thread_id = None
    try:
        thread_id = adapter._message_thread_id(message)
    except Exception:  # health: allow BLE001 -- topic lookup is best-effort; any failure sends the note without a thread id, and the failure is logged with its traceback
        logger.debug("[Telegram] expired-tap thread lookup failed", exc_info=True)
        thread_id = None
    try:
        await adapter.send(
            str(chat_id),
            text,
            metadata={"thread_id": str(thread_id)} if thread_id else None,
        )
    except Exception:  # health: allow BLE001 -- expired-tap fallback note: a failed note must not raise into the callback dispatcher; the failure is logged with its traceback above
        logger.debug(
            "[Telegram] expired-tap chat note could not be sent", exc_info=True
        )


async def refuse_tap(event: Any, requester: Optional[str]) -> bool:
    """Answer a tap on someone else's prompt (``gateway.approval_actor`` refused it: *requester* is
    the opener's name) with the localized "only X can answer" alert; the card stays open for them."""
    if not requester:
        return False
    from gateway.approval_actor import refusal_text

    await answer_callback(event, refusal_text(requester)[:TOAST_LIMIT], show_alert=True)
    return True
