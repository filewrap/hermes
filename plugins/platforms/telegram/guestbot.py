"""GuestBot settings and status messages for the Telegram MTProto adapter.

Kept out of ``telethon_adapter.py`` (already over the file-size cap): the
profile-persisted GuestBot toggle and the per-turn status message live here;
the adapter keeps thin delegates so existing call sites are unchanged.

Guest turns never stream: there is no guest draft sink (removed 2026-11-07 —
guestbot cannot send a typing indicator; the draft never worked live). The
"Thinking…" status message, edited in place with the answer, is the feedback.
"""
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GUEST_THREAD_PREFIX = "guest-"


def _secret(name: str, default: str = "") -> str:
    from gateway.platforms._shared import get_scoped_secret
    return str(get_scoped_secret(name, default) or "").strip()


def guestbot_enabled(adapter: Any) -> bool:
    if adapter._guestbot_enabled_runtime is not None:
        return adapter._guestbot_enabled_runtime
    extra = getattr(adapter.config, "extra", {}) or {}
    if "guest_mode" in extra:
        return adapter._truthy_setting(extra.get("guest_mode"), default=True)
    raw = _secret("TELEGRAM_GUEST_MODE")
    if raw:
        return adapter._truthy_setting(raw, default=True)
    # Native Guest Mode is opt-out; Telegram's allowlist/auth checks still apply.
    return True


def _persist_guestbot_flag(adapter: Any, source: Any, *, key: str, runtime_attr: str,
                           enabled: bool) -> None:
    """Persist one profile-local GuestBot toggle; never changes BotFather settings."""
    from hermes_cli.config import atomic_config_write, read_user_config_raw

    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    home = resolver(source) if callable(resolver) else None
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    config_path = Path(home) / "config.yaml"
    config = read_user_config_raw(config_path)
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        raise ValueError("The platforms section of config.yaml is not a mapping")
    telegram = platforms.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("The Telegram section of config.yaml is not a mapping")
    telegram[key] = bool(enabled)
    atomic_config_write(config_path, config)
    extra = getattr(adapter.config, "extra", None)
    if not isinstance(extra, dict):
        extra = {}
        adapter.config.extra = extra
    extra[key] = bool(enabled)
    setattr(adapter, runtime_attr, bool(enabled))


def persist_guestbot_setting(adapter: Any, source: Any, enabled: bool) -> None:
    _persist_guestbot_flag(adapter, source, key="guest_mode",
                           runtime_attr="_guestbot_enabled_runtime", enabled=enabled)


def is_guest_thread(thread_id: Any) -> bool:
    return str(thread_id or "").startswith(_GUEST_THREAD_PREFIX)


# Status message: a real message replying to the guest query, edited in place
# with the answer. Plain text, no emoji — there is no guestbot-specific
# placeholder in the codebase, and the generic locale thinking strings carry
# emoji this path does not want.
_GUEST_STATUS_TEXT = "Thinking…"


async def send_guest_status_message(adapter: Any, chat_id: Any, *,
                                    reply_to_message_id: Any) -> Optional[str]:
    """Send the guest status message as a reply to the guest's query message.

    Best-effort: returns the sent message id, or None when the peer cannot be
    resolved or the send fails. A None result means the turn falls back to the
    proven guest-result delivery — the guest still gets the answer.
    """
    client = getattr(adapter, "_client", None)
    if client is None or reply_to_message_id is None:
        return None
    try:
        peer = await adapter._input_peer_for(chat_id)
    except Exception:
        logger.debug("GuestBot status message: could not resolve peer for %s",
                     chat_id, exc_info=True)
        return None
    try:
        sent = await client.send_message(peer, _GUEST_STATUS_TEXT,
                                         reply_to=reply_to_message_id)
    except Exception:
        logger.debug("GuestBot status message send failed for chat %s",
                     chat_id, exc_info=True)
        return None
    message_id = getattr(sent, "id", None)
    return str(message_id) if message_id is not None else None


async def deliver_guest_response(adapter: Any, query_id: Any, chat_id: Any,
                                 status_message_id: Optional[str], text: str) -> None:
    """Deliver a guest answer: edit the status message when one exists.

    The edit keeps the response in the same place the guest is already
    looking, with the same rich formatting the guest result would carry. On
    any edit failure the stale status is removed best-effort and the answer
    goes out through the proven guest-result path, so the guest always gets it.
    """
    if status_message_id is not None:
        result = await adapter.edit_message(chat_id, status_message_id, text)
        if result.success:
            return
        logger.debug("GuestBot status edit failed for chat %s; falling back to guest result",
                     chat_id)
        await adapter.delete_message(chat_id, status_message_id)
    await adapter._send_guest_result(query_id, text)
