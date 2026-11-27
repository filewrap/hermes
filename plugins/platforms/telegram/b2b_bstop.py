"""Owner-only hard stop for a B2B talk.

The hammer, not a soft notice: interrupt any running turn on the talk's
session key, mark the talk record killed (permanent, never resumable), arm a
one-shot closed note so a later turn does not continue the talk from history,
and send the leave signal so the other side stops too.
"""
from __future__ import annotations

import logging
from typing import Any

from .b2b_handshake import (
    CODE_LEAVE,
    arm_talk_closed_note,
    session_key_profile,
    talk_session_key,
)
from .bot_operations import BotOperationsError, TelegramBotOperations

logger = logging.getLogger(__name__)


async def handle_bstop(adapter: Any, source: Any, arguments: str, *,
                       reply_to: Any = None, send_reply: bool = True) -> str:
    """Run /bstop; returns the one-line confirmation (sent when asked)."""
    operations = TelegramBotOperations(adapter)
    if not operations._is_owner(source):
        text = "Only the configured owner can stop a talk."
        if send_reply:
            await _send(adapter, source, text, reply_to)
        return text
    peer_arg = (arguments or "").strip().split(maxsplit=1)[0] if arguments else ""
    try:
        store = operations._store(source)
    except (BotOperationsError, OSError, ValueError):
        text = "Profile routing is unavailable; nothing was stopped."
        if send_reply:
            await _send(adapter, source, text, reply_to)
        return text
    targets = _resolve_targets(store, source, peer_arg)
    if targets is None:
        text = "That bot is not a configured peer."
        if send_reply:
            await _send(adapter, source, text, reply_to)
        return text
    if not targets:
        text = "No live talk to stop."
        if send_reply:
            await _send(adapter, source, text, reply_to)
        return text
    killed: list[str] = []
    leave_failed: list[str] = []
    for peer_id, leave_chat_id, scope in targets:
        name = _peer_label(store, peer_id)
        sent = await _kill_one(adapter, operations, store, source, peer_id, leave_chat_id, scope)
        killed.append(name)
        if not sent:
            leave_failed.append(name)
    if len(killed) == 1:
        text = f"Stopped the talk with {killed[0]}."
    else:
        text = f"Stopped {len(killed)} talks: {', '.join(killed)}."
    if leave_failed:
        text += f" The leave signal did not send for {', '.join(leave_failed)}."
    if send_reply:
        await _send(adapter, source, text, reply_to)
    return text


def _resolve_targets(store: Any, source: Any,
                     peer_arg: str) -> list[tuple[int, str, str]] | None:
    """(peer id, leave chat id, leave scope) per talk to kill.

    None when the named peer is not configured; [] when nothing is live.
    """
    if peer_arg:
        peer = _resolve_peer_arg(store, peer_arg)
        if peer is None:
            return None
        peer_id = int(peer["id"])
        if str(getattr(source, "chat_type", "")) in {"group", "forum", "channel"} \
                and store.is_admitted(peer_id, "group", source.chat_id):
            return [(peer_id, str(source.chat_id), "group")]
        return [(peer_id, str(peer_id), "dm")]
    live = [talk for talk in store.list_talks()
            if talk.get("state") == "live" and not talk.get("killed")]
    if str(getattr(source, "chat_type", "")) in {"group", "forum", "channel"}:
        return [(int(talk["peer_id"]), str(source.chat_id), "group")
                for talk in live
                if store.is_admitted(talk["peer_id"], "group", source.chat_id)]
    return [(int(talk["peer_id"]), str(talk["peer_id"]), "dm") for talk in live]


def _resolve_peer_arg(store: Any, peer_arg: str) -> dict[str, Any] | None:
    if peer_arg.isdigit():
        return store.peer(int(peer_arg))
    username = peer_arg.lstrip("@").lower()
    for peer in store.peers():
        if str(peer.get("username") or "").lstrip("@").lower() == username:
            return peer
    return None


def _peer_label(store: Any, peer_id: int) -> str:
    peer = store.peer(peer_id) or {}
    name = str(peer.get("name") or "").strip()
    username = str(peer.get("username") or "").lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    return f"@{username}" if username else str(peer_id)


async def _kill_one(adapter: Any, operations: TelegramBotOperations, store: Any,
                    source: Any, peer_id: int, leave_chat_id: str, scope: str) -> bool:
    """Interrupt, kill, arm the note, and signal the peer. True when the leave sent."""
    talk_key = talk_session_key(session_key_profile(adapter, source), peer_id)
    runner = getattr(adapter, "gateway_runner", None)
    interrupt = getattr(runner, "_interrupt_and_clear_session", None) if runner is not None else None
    if callable(interrupt):
        try:
            await interrupt(talk_key, source, interrupt_reason="bstop", invalidation_reason="bstop")
        except Exception:
            logger.debug("Talk interrupt failed", exc_info=True)
    try:
        from tools.async_delegation import interrupt_for_session
        interrupt_for_session(session_key=talk_key, reason="bstop")
    except Exception:
        logger.debug("Talk delegation interrupt failed", exc_info=True)
    try:
        store.mark_talk_killed(peer_id)
    except (BotOperationsError, OSError, ValueError):
        pass
    try:
        home = operations.profile_home(source)
        arm_talk_closed_note(home, talk_key, peer_id)
    except Exception:
        logger.debug("Talk closed-note arm failed", exc_info=True)
    peer = store.peer(peer_id) or {}
    username = str(peer.get("username") or "").lstrip("@")
    leave_text = f"{CODE_LEAVE} {leave_chat_id}"
    if scope == "group" and username:
        leave_text = f"@{username} {leave_text}"
    try:
        await adapter.send(str(leave_chat_id), leave_text)
        return True
    except Exception:
        logger.debug("Leave signal failed", exc_info=True)
        return False


async def _send(adapter: Any, source: Any, text: str, reply_to: Any) -> None:
    try:
        await adapter.send(str(source.chat_id), text,
                           reply_to=reply_to if isinstance(reply_to, int) else None)
    except Exception:
        logger.debug("Stop confirmation failed", exc_info=True)
