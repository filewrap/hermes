"""Feed a terminal final-send failure back to the agent and the user.

A final reply that is refused for good (a rich payload Telegram rejects, a
permanent formatting error) used to vanish silently: the turn ended as if the
reply had landed, the transcript already held it, and the delivery ledger just
re-sent the same doomed payload until it was abandoned. This module is the
feedback step ``send_final_ledgered`` runs after finalizing a failed send:

- one-shot note on the runner (same shape as ``_pending_model_notes``), which
  ``_prepare_turn_message`` prepends API-only to the agent's next turn, so the
  agent learns its reply never arrived — with the real reason;
- one short, explicitly-marked MTProto notice to the user, sent directly via
  the adapter (never through ``_send_with_retry``, never the failed content,
  never a Bot API downgrade), best-effort and swallowed on failure.

Only a *definitive* failure feeds back: success, partial delivery (the head
is already on screen), a flood penalty (a notice would re-enter the ban), a
dead target (nobody is there to read it), and a timeout (the send may have
landed) are all skipped.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from agent.i18n import t
from gateway.dead_targets import classify_dead_error

logger = logging.getLogger(__name__)

# Telethon renders an RPC failure as "RPCError 400: <server message> (caused by
# <RequestClass>)"; the server message is the part worth showing.
_RPC_REASON_RE = re.compile(r"^RPCError \d+: (.+?) \(caused by [^)]+\)$")

_NOTE_REASON_MAX = 200
_NOTICE_REASON_MAX = 120


def _failure_detail(result: Any) -> str:
    """The real reason a send failed: the adapter's ``raw_response`` detail when
    present (the RPC error text), else the typed ``error`` string."""
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict):
        detail = raw.get("error_detail")
        if detail:
            return str(detail)
    return str(getattr(result, "error", "") or "")


def _short_reason(detail: str) -> str:
    """The server's message on its own (``RICH_MESSAGE_INVALID``), for the user
    notice; falls back to the detail itself, truncated."""
    match = _RPC_REASON_RE.match((detail or "").strip())
    if match:
        return match.group(1)
    return (detail or "unknown error").strip()[:_NOTICE_REASON_MAX]


def build_delivery_failure_note(*, reason: str, chars: int) -> str:
    """The one-shot note the agent sees on its next turn. States plainly that
    the previous reply never arrived, why, and how much was lost — the
    transcript already shows the reply as delivered, so without this the agent
    would assume the user read it."""
    reason = (reason or "unknown error").strip()[:_NOTE_REASON_MAX]
    return (
        "[Note: your previous reply to this chat was not delivered — "
        f"Telegram rejected it ({reason}). "
        f"The user never saw those {chars} characters. Do not assume they read it; "
        "offer to resend it in a simpler form if it matters.]"
    )


def record_delivery_failure_note(runner: Any, session_key: str, note: str) -> None:
    """Store the note on the runner's per-session dict, same shape as
    ``_pending_model_notes`` (popped once by ``_prepend_pending_note``). Never
    raises: feedback must not break the turn it follows."""
    if runner is None or not session_key or not note:
        return
    notes = getattr(runner, "_pending_delivery_failure_notes", None)
    if not isinstance(notes, dict):
        notes = {}
        try:
            runner._pending_delivery_failure_notes = notes
        except Exception:
            logger.debug("Could not record delivery-failure note", exc_info=True)
            return
    notes[session_key] = note


def terminal_delivery_failure(result: Any) -> bool:
    """True only for a definitive failure: refused for good, nothing delivered,
    no server penalty pending, and the target is alive. Everything else (a
    success, a partial delivery, a flood wait, a dead target, a timeout that
    may have landed) is not feedback — it is either nothing to say or a retry
    the ledger already owns."""
    if result is None or getattr(result, "success", False):
        return False
    error = str(getattr(result, "error", "") or "")
    if not error:
        return False
    # Local import: the base adapter imports this module from send_final_ledgered.
    from gateway.platforms.base import BasePlatformAdapter

    if BasePlatformAdapter._is_partial_delivery(result):
        return False
    if getattr(result, "retryable", False) or getattr(result, "retry_after", None) is not None:
        return False
    if BasePlatformAdapter._is_rate_limited_error(error):
        return False
    if BasePlatformAdapter._is_timeout_error(error):
        return False
    if classify_dead_error(error) is not None:
        return False
    return True


async def notify_delivery_failure(
    adapter: Any, *, chat_id: Any, reason: str, reply_to: Optional[str] = None,
    metadata: Any = None,
) -> bool:
    """One best-effort MTProto notice that the previous reply never arrived.
    Plain text (no failed content, no Bot API), sent directly via the adapter —
    never re-entering ``_send_with_retry``. Returns whether it landed; never
    raises."""
    if adapter is None:
        return False
    try:
        text = t("gateway.notify.delivery_failed_notice", reason=reason)
        result = await adapter.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)
        return bool(getattr(result, "success", False))
    except Exception:
        logger.debug("Delivery-failure notice send failed", exc_info=True)
        return False


async def feedback_terminal_delivery_failure(
    *, runner: Any, session_key: str, adapter: Any, chat_id: Any, content_chars: int,
    result: Any, reply_to: Optional[str] = None, metadata: Any = None,
) -> None:
    """The feedback step for a failed final send: record the agent's note, then
    notify the user. Never raises."""
    try:
        if not terminal_delivery_failure(result):
            return
        detail = _failure_detail(result)
        record_delivery_failure_note(
            runner, session_key,
            build_delivery_failure_note(reason=detail, chars=content_chars))
        await notify_delivery_failure(
            adapter, chat_id=chat_id, reason=_short_reason(detail),
            reply_to=reply_to, metadata=metadata)
    except Exception:
        logger.debug("Delivery-failure feedback failed", exc_info=True)
