"""Message-edit protocol (beta): what an edit turn carries and how it is answered.

A Telegram edit re-sends its message's own ``message_id`` with new text. The
adapter marks the event (``edit_revision`` + metadata below); the turn then
answers the CORRECTED text, and — when the agent already replied to the old
text — the final send edits that reply in place instead of sending a new one.

The pieces, and where each lives:
- ``plugins/platforms/telegram/message_edits.py`` (Telegram-only): the beta
  setting reader, the adapter-side reply cache, the ack text.
- This module (generic): the metadata keys, the UNCHANGED sentinel, the
  history scan that recovers the old text, the one-shot note, and the
  final-send decision. No Telegram imports here, so the runner and the
  delivery layer can use it without a plugin boundary crossing.
- ``run_turn_runner._prepare_turn_message`` prepends the note (API-only, never
  persisted); ``base._send_final_text`` executes the decision.

Transcript rule: the corrected text is appended as a NEW user row (append-only,
prompt-cache safe). The scan therefore takes the LAST row with the message id.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Event metadata flag (MessageEvent.metadata): this event is a message edit.
EDIT_METADATA_IS_EDIT = "is_edit"
#: Event metadata: the Telegram id of the edited message (str).
EDIT_METADATA_MESSAGE_ID = "edit_message_id"
#: Event metadata: Telegram id of the agent's reply to the old text, when the
#: adapter's reply cache still holds it. The final send edits this message.
EDIT_METADATA_CORRECTS_REPLY_ID = "edit_corrects_reply_id"

#: The model's exact reply when the correction changes nothing. The final send
#: then sends nothing (the previous reply stands; the ack already confirmed the
#: edit). Only used when a previous reply exists to keep.
EDIT_UNCHANGED_SENTINEL = "UNCHANGED"

#: Quoted text cap inside the note: enough to judge whether the correction
#: changes the answer, without stuffing a long reply into every edit turn.
_NOTE_QUOTE_MAX_CHARS = 500


def _clip(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > _NOTE_QUOTE_MAX_CHARS:
        return text[:_NOTE_QUOTE_MAX_CHARS] + " …"
    return text


def _text_of(message: Any) -> Optional[str]:
    """The message's text when it is a non-empty string, else None."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def find_edit_context(
    history: Any, edit_message_id: Any,
) -> tuple[Optional[str], Optional[str]]:
    """(old text, previous reply text) for an edited message id, from the loaded
    history (OpenAI-format dicts carrying ``message_id``).

    Takes the LAST user row with the id (the corrected text is appended as a new
    row, so an earlier row is the stale original); the previous reply is the
    first assistant row after it. Either may be None (not found / not replied
    yet / non-text content).
    """
    if not edit_message_id or not isinstance(history, list):
        return None, None
    target = str(edit_message_id)
    user_index = None
    for index in range(len(history) - 1, -1, -1):
        row = history[index]
        if isinstance(row, dict) and row.get("role") == "user" and str(row.get("message_id") or "") == target:
            user_index = index
            break
    if user_index is None:
        return None, None
    old_text = _text_of(history[user_index])
    previous_reply_text = None
    for row in history[user_index + 1:]:
        if isinstance(row, dict) and row.get("role") == "assistant":
            previous_reply_text = _text_of(row)
            break
    return old_text, previous_reply_text


def build_edit_note(
    *, old_text: Optional[str], previous_reply_text: Optional[str] = None,
    can_edit_reply: bool = False,
) -> Optional[str]:
    """The one-shot note prepended (API-only) to an edit turn. None when there
    is no old text to compare against — the turn then just answers the new text.

    ``can_edit_reply`` means the final send will replace the previous reply in
    place, so the model is told the UNCHANGED protocol; without it the answer
    goes out as a new message and no sentinel is needed.
    """
    if not old_text or not str(old_text).strip():
        return None
    old = _clip(old_text)
    if previous_reply_text and str(previous_reply_text).strip():
        previous = _clip(previous_reply_text)
        if can_edit_reply:
            return (
                "[Note: the user edited the message you are answering. "
                f"Original text: \"{old}\". "
                f"Your previous reply to the original was: \"{previous}\". "
                "If the correction changes your answer, write the corrected answer "
                "— it will replace your previous reply. If the correction does not "
                f"change your answer, reply with exactly: {EDIT_UNCHANGED_SENTINEL}]"
            )
        return (
            "[Note: the user edited the message you are answering. "
            f"Original text: \"{old}\". "
            f"Your previous reply to the original was: \"{previous}\". "
            "Answer the corrected text below as a new reply.]"
        )
    return (
        "[Note: the user edited this message before you replied. "
        f"Original text: \"{old}\". Answer the corrected text below.]"
    )


def decide_edit_correction(*, final_text: str, previous_reply_id: Any) -> str:
    """What the final send does for an edit turn: ``"edit"`` (replace the
    previous reply in place), ``"unchanged"`` (send nothing — the previous
    reply stands), or ``"send"`` (no previous reply to edit; normal send)."""
    if isinstance(final_text, str) and final_text.strip() == EDIT_UNCHANGED_SENTINEL:
        return "unchanged"
    if previous_reply_id:
        return "edit"
    return "send"


async def deliver_edit_correction(adapter: Any, event: Any, text_content: str,
                                  record_delivery: Any) -> bool:
    """Deliver an edit turn's final text by editing the previous reply in place
    (or sending nothing when the model answered UNCHANGED). True means the turn
    is delivered; False means the normal send should run instead — no previous
    reply id, or the edit failed (too old, deleted, unsupported). The reply is
    delivered by edit, so no ledger row is owed and the turn marker is released
    here (a crash after this point must not redeliver). ``adapter`` is the
    delivery adapter (duck-typed: it supplies ``_final_delivery_adapter``,
    ``edit_message`` via that adapter, and ``_release_turn_marker``)."""
    from gateway.platforms.base import SendResult  # function-level: base imports this module

    metadata = getattr(event, "metadata", None) or {}
    previous_reply_id = metadata.get(EDIT_METADATA_CORRECTS_REPLY_ID)
    if not previous_reply_id:
        return False
    decision = decide_edit_correction(final_text=text_content, previous_reply_id=previous_reply_id)
    if decision == "send":
        return False
    if decision == "unchanged":
        record_delivery(SendResult(success=True, message_id=str(previous_reply_id)))
        await adapter._release_turn_marker(event)
        return True
    delivery_adapter = adapter._final_delivery_adapter(event.source)
    try:
        result = await delivery_adapter.edit_message(
            event.source.chat_id, str(previous_reply_id), text_content)
    except Exception:
        logger.warning("Edit-correction send failed, falling back to a new message",
                       exc_info=True)
        return False
    if not getattr(result, "success", False):
        logger.warning("Edit-correction send failed (%s), falling back to a new message",
                       getattr(result, "error", "unknown"))
        return False
    record_delivery(result)
    await adapter._release_turn_marker(event)
    return True
