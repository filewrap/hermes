"""GuestBot settings and status messages for the Telegram MTProto adapter.

Kept out of ``telethon_adapter.py`` (already over the file-size cap): the
profile-persisted GuestBot toggle and the per-turn status message live here;
the adapter keeps thin delegates so existing call sites are unchanged.

The "Thinking…" status message, edited in place with the answer, is the feedback.
"""
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GUEST_THREAD_PREFIX = "guest-"

# Wall-clock cap on one guest turn. The turn itself has no wall-clock
# budget (``agent.run_budget_seconds`` defaults to None) and the iteration
# limit defaults to unlimited, so a normal multi-iteration turn on a slow
# model outruns the old 55 s cap and the guest was told "took too long" for
# a turn that would have finished. 300 s sits above a normal long turn while
# still bounding a wedged handler, so the per-user lock releases and the
# guest gets an answer instead of silence. The status message keeps the
# guest informed for the whole wait.
_GUEST_HANDLER_TIMEOUT_SECONDS = 300


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


# crc32(b'InputPeer'): a raw send needs a real input peer, never a bare id.
_INPUT_PEER_SUBCLASS_ID = 0xC91C90B6


def _is_input_peer(peer: Any) -> bool:
    return getattr(peer, "SUBCLASS_OF_ID", None) == _INPUT_PEER_SUBCLASS_ID


async def _resolve_guest_peer(adapter: Any, chat_id: Any, peer: Any) -> Any:
    """InputPeer for the guest chat, preferring the update's own peer_id.

    Guest chats are exactly the chats the bot may not have in its entity
    cache, so ``_input_peer_for`` (cache, then Telethon's entity cache)
    can fail. The update's peer only casts directly for a basic group
    (``PeerChat``): ``get_input_peer`` raises for a bare ``PeerUser`` or
    ``PeerChannel`` because neither carries an access hash — those fall
    through to the cache path here, and to the fresh network path in
    ``send_guest_status_message`` when the cache misses too.
    Returns None when nothing resolves to a real InputPeer.
    """
    if peer is not None:
        try:
            from telethon import utils as tl_utils

            resolved = tl_utils.get_input_peer(peer)
            if _is_input_peer(resolved):
                return resolved
        except Exception:
            logger.debug("GuestBot status: update peer did not resolve for chat %s",
                         chat_id, exc_info=True)
    try:
        resolved = await adapter._input_peer_for(chat_id)
    except Exception:
        logger.debug("GuestBot status: peer cache did not resolve chat %s",
                     chat_id, exc_info=True)
        return None
    return resolved if _is_input_peer(resolved) else None


async def _fresh_user_peer(adapter: Any, client: Any, chat_id: Any) -> Any:
    """Resolve a DM user fresh from the network (``GetUsersRequest`` with
    ``access_hash=0`` works for a bot when the user has messaged it).
    Same slow path as the streaming-response sink."""
    try:
        user_id = int(str(chat_id).strip())
    except (TypeError, ValueError):
        return None
    try:
        from telethon import functions, types, utils as tl_utils

        users = await client(functions.users.GetUsersRequest(
            [types.InputUser(user_id, 0)]))
        user = users[0] if users else None
        if user is None or isinstance(user, types.UserEmpty):
            return None
        resolved = tl_utils.get_input_peer(user)
        if not _is_input_peer(resolved):
            return None
        remember = getattr(adapter, "_remember_peer", None)
        if callable(remember):
            try:
                remember(chat_id, resolved)
            except Exception:
                pass
        return resolved
    except Exception:
        logger.debug("GuestBot status: fresh user resolution failed for chat %s",
                     chat_id, exc_info=True)
        return None


async def _fresh_channel_peer(adapter: Any, client: Any, chat_id: Any, peer: Any) -> Any:
    """Resolve a group channel fresh from the network (``GetChannelsRequest``
    with ``access_hash=0``, the same slow path Telethon's entity cache takes
    for a channel the bot is a member of)."""
    try:
        channel_id = int(peer.channel_id)
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        from telethon import functions, types, utils as tl_utils

        channels = await client(functions.channels.GetChannelsRequest(
            [types.InputChannel(channel_id, 0)]))
        chats = getattr(channels, "chats", None) or []
        channel = chats[0] if chats else None
        if channel is None:
            return None
        resolved = tl_utils.get_input_peer(channel)
        if not _is_input_peer(resolved):
            return None
        remember = getattr(adapter, "_remember_peer", None)
        if callable(remember):
            try:
                remember(chat_id, resolved)
            except Exception:
                logger.debug("GuestBot status: could not cache fresh channel peer for chat %s",
                             chat_id, exc_info=True)
        return resolved
    except Exception:
        logger.debug("GuestBot status: fresh channel resolution failed for channel %s",
                     channel_id, exc_info=True)
        return None


async def _fresh_guest_peer(adapter: Any, client: Any, chat_id: Any, peer: Any) -> Any:
    """Fresh network resolution for a guest chat the cache cannot resolve:
    a DM user via ``GetUsersRequest``, a group channel via
    ``GetChannelsRequest`` (both with ``access_hash=0``). Returns None when
    the peer type has no fresh path or the server does not resolve it."""
    if peer is None:
        return None
    name = type(peer).__name__
    if name == "PeerUser":
        return await _fresh_user_peer(adapter, client, chat_id)
    if name == "PeerChannel":
        return await _fresh_channel_peer(adapter, client, chat_id, peer)
    return None


async def _try_send_status(client: Any, input_peer: Any,
                           reply_to_message_id: Any) -> Any:
    """Send the status as a reply to the query message; a reply the server
    rejects (guest context) falls back to a plain status — a status
    without a reply is still better than no status at all."""
    try:
        return await client.send_message(input_peer, _GUEST_STATUS_TEXT,
                                         reply_to=reply_to_message_id)
    except Exception:
        logger.debug("GuestBot status: reply send failed; retrying without reply_to",
                     exc_info=True)
    try:
        return await client.send_message(input_peer, _GUEST_STATUS_TEXT)
    except Exception:
        return None


async def send_guest_status_message(adapter: Any, chat_id: Any, *,
                                    reply_to_message_id: Any,
                                    peer: Any = None) -> Optional[str]:
    """Send the guest status message as a reply to the guest's query message.

    Best-effort: returns the sent message id, or None when the peer cannot be
    resolved or the send fails. A None result means the turn falls back to the
    proven guest-result delivery — the guest still gets the answer. Terminal
    failures log at WARNING (not debug) so a live failure is diagnosable
    from the host logs instead of vanishing silently.
    """
    client = getattr(adapter, "_client", None)
    if client is None or reply_to_message_id is None:
        return None
    input_peer = await _resolve_guest_peer(adapter, chat_id, peer)
    resolved_fresh = False
    if input_peer is None:
        # Cold-cache guest chat: the update peer is a bare PeerUser /
        # PeerChannel that get_input_peer cannot cast, and the entity cache
        # missed — resolve fresh from the network before giving up. This is
        # the live guest case: without it the status never sends while the
        # answer still lands via the guest result (which needs only the
        # query_id), so the failure looks like a silent status skip.
        input_peer = await _fresh_guest_peer(adapter, client, chat_id, peer)
        resolved_fresh = input_peer is not None
        if input_peer is None:
            logger.warning("GuestBot status message: peer unresolvable for chat %s; no status sent",
                           chat_id)
            return None
    # Cache the resolved peer: the answer edit goes through
    # ``adapter.edit_message`` → ``_input_peer_for``, which would fail
    # the same way for an uncached guest chat if the peer were not cached.
    remember = getattr(adapter, "_remember_peer", None)
    if callable(remember):
        try:
            remember(chat_id, input_peer)
        except Exception:
            pass
    sent = await _try_send_status(client, input_peer, reply_to_message_id)
    if sent is None and not resolved_fresh:
        # Zero/stale access hash: resolve the guest fresh and retry once.
        fresh = await _fresh_guest_peer(adapter, client, chat_id, peer)
        if fresh is not None:
            sent = await _try_send_status(client, fresh, reply_to_message_id)
    if sent is None:
        logger.warning("GuestBot status message: send failed for chat %s; "
                       "answer falls back to guest result", chat_id)
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
