"""``/whisper``: ephemeral one-to-one secrets inside a group.

The sender's command arrives as an ephemeral command (only the sender
sees it). The target gets an ephemeral "Tap to reveal" card with a Reveal
button; only the target's tap edits that same message into the secret.
Callback data carries only a random id plus the group chat id — never the
text — and the payload is Fernet ciphertext at rest (see
:mod:`whisper_store`).

Attempt order: ephemeral in-group (if the bot is an admin) -> ephemeral DM
to the target -> ephemeral notice to the sender -> silent log. Whisper
content is never posted as a normal group message.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

WHISPER_MAX_TEXT = 4096


async def _notice(
    adapter: Any, *, receiver_id: Any, peer: Any, text: str,
) -> None:
    """Tell one user something ephemerally; best effort, never the secret."""
    from .ephemeral_messaging import input_user_for, send_ephemeral

    receiver = await input_user_for(adapter, receiver_id)
    if receiver is None:
        return
    await send_ephemeral(adapter, receiver_id=receiver, peer=peer, text=text)


async def _resolve_sender_name(adapter: Any, user_id: str) -> str:
    """Display name for the 'Tap to reveal' card; 'Someone' when unknown."""
    try:
        sender = await adapter._client.get_entity(int(user_id))
    except Exception:
        logger.debug("Telegram whisper sender name lookup failed", exc_info=True)
        return "Someone"
    name = (getattr(sender, "first_name", None)
            or getattr(sender, "username", None) or "Someone")
    return str(name)


async def _resolve_reply_target(
    adapter: Any, peer: Any, reply_to_msg_id: int,
) -> str | None:
    """Sender id of the replied-to message, or None when unresolvable."""
    try:
        replied = await adapter._client.get_messages(peer, ids=int(reply_to_msg_id))
    except Exception:
        logger.debug("Telegram whisper reply lookup failed", exc_info=True)
        return None
    if isinstance(replied, (list, tuple)):
        replied = next((item for item in replied if item is not None), None)
    if replied is None:
        return None
    sender_peer = getattr(replied, "from_id", None)
    if sender_peer is None or type(sender_peer).__name__ != "PeerUser":
        return None
    user_id = getattr(sender_peer, "user_id", None)
    return str(user_id) if user_id is not None else None


def _parse_whisper(message: Any) -> tuple[str, int, list[Any], int | None]:
    """Split a /whisper command into (args, args_start, entities, reply_id).

    Offsets are relative to the full message text, matching the entities.
    """
    text = str(getattr(message, "message", "") or "")
    match = re.match(r"^\s*/\S+\s+(.*)$", text, re.DOTALL)
    args = match.group(1) if match else ""
    args_start = match.start(1) if match else len(text)
    reply = getattr(message, "reply_to", None)
    reply_to_msg_id = getattr(reply, "reply_to_msg_id", None)
    return args, args_start, list(getattr(message, "entities", None) or []), reply_to_msg_id


async def _resolve_target(
    adapter: Any, *, peer: Any, args: str, args_start: int,
    entities: list[Any], reply_to_msg_id: int | None,
) -> tuple[str | None, str]:
    """Resolve the whisper target and the remaining text.

    Explicit target first (mention-name entity, @username, raw user id),
    then the replied-to message's sender. Returns (target_id, text);
    target_id is None when nothing resolved.
    """
    text = args
    target_id: str | None = None
    if args:
        first, _, rest = args.partition(" ")
        first = first.strip()
        for entity in entities:
            if (type(entity).__name__ == "MessageEntityMentionName"
                    and getattr(entity, "offset", None) == args_start):
                target_id = str(getattr(entity, "user_id", "") or "")
                text = rest.strip()
                break
        if target_id is None and first.startswith("@") and len(first) > 1:
            try:
                user = await adapter._client.get_entity(first[1:])
                target_id = str(getattr(user, "id", "") or "")
                text = rest.strip()
            except Exception:
                logger.debug("Telegram whisper @username lookup failed", exc_info=True)
        if target_id is None and first.lstrip("-").isdecimal():
            try:
                user = await adapter._client.get_entity(int(first))
                target_id = str(getattr(user, "id", "") or "")
                text = rest.strip()
            except Exception:
                logger.debug("Telegram whisper user-id lookup failed", exc_info=True)
    if target_id is None and reply_to_msg_id is not None:
        target_id = await _resolve_reply_target(adapter, peer, reply_to_msg_id)
        text = args
    return target_id, text


async def _is_group_member(adapter: Any, group_peer: Any, user_id: str) -> bool:
    """True when the user is a member of the group (Telethon permissions)."""
    from telethon.errors.rpcerrorlist import UserNotParticipantError

    try:
        perms = await adapter._client.get_permissions(group_peer, int(user_id))
    except UserNotParticipantError:
        return False
    except Exception:
        logger.debug("Telegram whisper membership check failed", exc_info=True)
        return False
    if perms is None:
        return False
    return getattr(perms, "has_left", False) is not True


async def _bot_is_admin(adapter: Any, group_peer: Any) -> bool:
    """True when the bot itself is an admin of the group."""
    try:
        perms = await adapter._client.get_permissions(group_peer, "me")
    except Exception:
        logger.debug("Telegram whisper admin check failed", exc_info=True)
        return False
    return getattr(perms, "is_admin", False) is True


async def _deliver_whisper_card(
    adapter: Any, *, receiver_id: str, peer: Any, whisper_id: str,
    chat_id: str, sender_name: str,
) -> bool:
    """Send the ephemeral 'Tap to reveal' card; True when accepted."""
    from .ephemeral_messaging import input_user_for, send_ephemeral
    from .mtproto_rich import inline_markup

    receiver = await input_user_for(adapter, receiver_id)
    if receiver is None:
        return False
    markup = inline_markup(
        [[("Reveal", f"hm:whisper:{whisper_id}:{chat_id}", "primary")]])
    return await send_ephemeral(
        adapter, receiver_id=receiver, peer=peer,
        text=f"{sender_name} whispered to you. Tap to reveal.",
        reply_markup=markup,
    )


async def _group_whisper_context(
    adapter: Any, source: Any, sender_id: str, chat_id: str,
) -> tuple[Any, Any] | None:
    """(group_home, group_peer) for an admitted group whisper, else None.

    Silent ignores (no group home, opted out, not admitted, store or peer
    down) are logged here, matching the /imout gates.
    """
    from gateway.allowgc import group_home_for_source, is_opted_out
    from gateway.mongo_multiuser import owner_user_id
    from .telegram_controls import is_allowgc_member

    group_home = group_home_for_source(source)
    if group_home is None:
        logger.info(
            "Telegram /whisper ignored for user %s in chat %s: no group home",
            sender_id, chat_id,
        )
        return None
    if is_opted_out(group_home, sender_id):
        logger.info(
            "Telegram /whisper ignored for user %s in chat %s: opted out",
            sender_id, chat_id,
        )
        return None
    try:
        owner = owner_user_id(group_home)
    except Exception:
        logger.warning(
            "Telegram /whisper ignored for user %s in chat %s: group store unavailable",
            sender_id, chat_id, exc_info=True,
        )
        return None
    if sender_id != owner and not is_allowgc_member(adapter, sender_id, chat_id):
        logger.info(
            "Telegram /whisper ignored for user %s in chat %s: not admitted",
            sender_id, chat_id,
        )
        return None
    try:
        group_peer = await adapter._input_peer_for(chat_id)
    except Exception:
        logger.warning(
            "Telegram /whisper for user %s in chat %s: group peer unavailable",
            sender_id, chat_id, exc_info=True,
        )
        return None
    return group_home, group_peer


async def _deliver_or_notify(
    adapter: Any, *, group_home: Any, group_peer: Any, sender_id: str,
    chat_id: str, target_id: str, whisper_id: str, sender_name: str,
) -> None:
    """Deliver a stored whisper: in-group card (admin) -> target DM -> notice."""
    from .whisper_store import delete_whisper

    delivered = False
    if await _bot_is_admin(adapter, group_peer):
        delivered = await _deliver_whisper_card(
            adapter, receiver_id=target_id, peer=group_peer,
            whisper_id=whisper_id, chat_id=chat_id, sender_name=sender_name)
    if not delivered:
        try:
            target_peer = await adapter._input_peer_for(target_id)
        except Exception:
            logger.debug("Telegram whisper target peer lookup failed", exc_info=True)
            target_peer = None
        if target_peer is not None:
            delivered = await _deliver_whisper_card(
                adapter, receiver_id=target_id, peer=target_peer,
                whisper_id=whisper_id, chat_id=chat_id, sender_name=sender_name)
    if not delivered:
        delete_whisper(group_home, whisper_id)
        await _notice(
            adapter, receiver_id=sender_id, peer=group_peer,
            text="I couldn't deliver that whisper.")
        return
    logger.info(
        "Telegram /whisper delivered for user %s in chat %s", sender_id, chat_id,
    )


async def handle_ephemeral_whisper(adapter: Any, source: Any, message: Any) -> None:
    """Handle an ephemeral /whisper command from ``source``.

    Scope is groups (and forums) only; in a DM the sender is told so
    ephemerally. Only admitted members (plus the owner) may whisper, and
    only to a member who is not a bot.
    """
    from .ephemeral_messaging import input_user_for, send_ephemeral
    from .whisper_store import store_whisper

    sender_id = str(getattr(source, "user_id", "") or "")
    chat_id = str(getattr(source, "chat_id", "") or "")
    try:
        sender_peer = await adapter._input_peer_for(sender_id)
        sender_receiver = await input_user_for(adapter, sender_id)
    except Exception:
        logger.debug("Telegram whisper sender peer lookup failed", exc_info=True)
        return
    if source.chat_type in {"dm", "private", "direct"}:
        if sender_receiver is not None:
            await send_ephemeral(
                adapter, receiver_id=sender_receiver, peer=sender_peer,
                text="Whispers are for groups — send /whisper in a group.")
        return
    if source.chat_type not in {"group", "forum"}:
        return
    context = await _group_whisper_context(adapter, source, sender_id, chat_id)
    if context is None:
        return
    group_home, group_peer = context

    async def _notify(text: str) -> None:
        await _notice(adapter, receiver_id=sender_id, peer=group_peer, text=text)

    args, args_start, entities, reply_to_msg_id = _parse_whisper(message)
    target_id, text = await _resolve_target(
        adapter, peer=group_peer, args=args, args_start=args_start,
        entities=entities, reply_to_msg_id=reply_to_msg_id,
    )
    if target_id is None:
        await _notify("I couldn't deliver that whisper.")
        return
    if not text:
        await _notify("Say what to whisper: /whisper @user your message")
        return
    if len(text) > WHISPER_MAX_TEXT:
        await _notify(
            f"That whisper is too long — keep it under {WHISPER_MAX_TEXT} characters.")
        return
    try:
        target = await adapter._client.get_entity(int(target_id))
    except Exception:
        logger.debug("Telegram whisper target lookup failed", exc_info=True)
        await _notify("I couldn't deliver that whisper.")
        return
    if getattr(target, "bot", False) is True:
        logger.info(
            "Telegram /whisper ignored for user %s in chat %s: target is a bot",
            sender_id, chat_id,
        )
        return
    if not await _is_group_member(adapter, group_peer, target_id):
        await _notify("I couldn't deliver that whisper.")
        return
    try:
        whisper_id = store_whisper(
            group_home, sender_id=sender_id, target_id=target_id, text=text)
    except Exception:
        logger.warning(
            "Telegram /whisper for user %s in chat %s: could not store the whisper",
            sender_id, chat_id, exc_info=True,
        )
        await _notify("I couldn't deliver that whisper.")
        return
    sender_name = await _resolve_sender_name(adapter, sender_id)
    await _deliver_or_notify(
        adapter, group_home=group_home, group_peer=group_peer,
        sender_id=sender_id, chat_id=chat_id, target_id=target_id,
        whisper_id=whisper_id, sender_name=sender_name,
    )


async def _edit_whisper_result(adapter: Any, ephemeral: Any, text: str) -> None:
    """Edit the tapped ephemeral message in place, then answer silently."""
    from .ephemeral_messaging import (
        answer_ephemeral_callback,
        edit_ephemeral,
        input_user_for,
    )

    try:
        peer = await adapter._input_peer_for(ephemeral.chat_id)
        receiver = await input_user_for(adapter, ephemeral.user_id)
    except Exception:
        logger.debug("Telegram whisper reveal peer lookup failed", exc_info=True)
        receiver, peer = None, None
    if receiver is not None:
        await edit_ephemeral(
            adapter, receiver_id=receiver, peer=peer,
            message_id=ephemeral.message_id, text=text)
    await answer_ephemeral_callback(adapter, ephemeral.query_id)


async def handle_whisper_callback(
    adapter: Any, source: Any, data: str, *, ephemeral: Any,
) -> None:
    """Handle a tap on a whisper's Reveal button.

    Only the target may reveal; anyone else's tap is answered silently and
    reveals nothing. A tap after expiry edits the card to "(expired)".
    The secret is decrypted in memory only for the revealing edit.
    """
    from gateway.allowgc import resolve_home
    from .ephemeral_messaging import answer_ephemeral_callback
    from .whisper_store import load_whisper

    parts = str(data or "").split(":")
    if len(parts) != 4 or parts[1] != "whisper":
        return
    whisper_id, chat_id = parts[2], parts[3]
    home = resolve_home(chat_id, reap=False)
    if home is None:
        await answer_ephemeral_callback(adapter, ephemeral.query_id)
        return
    status, text, target_id = load_whisper(home, whisper_id)
    if str(ephemeral.user_id) != str(target_id or ""):
        await answer_ephemeral_callback(adapter, ephemeral.query_id)
        return
    if status != "ok" or text is None:
        await _edit_whisper_result(adapter, ephemeral, "(expired)")
        return
    await _edit_whisper_result(adapter, ephemeral, html.escape(text))
