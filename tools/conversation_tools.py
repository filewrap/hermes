#!/usr/bin/env python3
"""F4 conversation tools — ``conversations_list`` + ``conversations_turn``.

Thin tools over the conversation-address registry
(``tools/conversation_registry.py``) and the correlated-reply waiter
(``tools/conversation_reply_wait.py``):

- ``conversations_list`` merges the live Telegram dialogs (the
  ``list_chats`` client operation, same live path the ``telegram_client``
  tool uses) with the durable registry and the channel directory, so a
  chat seen before stays listable when the bot client is down.
- ``conversations_turn`` sends one message to a conversation by its
  ``conv_`` ref — through the ``telegram_client`` send path, never a second
  sender — registers the reply waiter first, and returns the actual
  correlated reply from the other side. A timeout is reported as a timeout,
  never dressed up as a send receipt.

Sending to the chat the turn already runs in is allowed: the message goes
out there and the wait matches the reply that comes back (which then also
arrives as the next user turn, as any reply in that chat would).
"""

import json
import logging
import time
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_LIST_DEFAULT_LIMIT = 20
_LIST_MAX_LIMIT = 50
_TURN_DEFAULT_TIMEOUT_S = 60.0
_TURN_MAX_TIMEOUT_S = 300.0
_MESSAGE_MAX_CHARS = 32768


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _telegram_context() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(chat_id, user_id, error) for this turn's Telegram session, if any."""
    from gateway.session_context import get_session_env

    if (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower() != "telegram":
        return None, None, "conversations need a Telegram conversation; this turn is not one."
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    user_id = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    if not chat_id or not user_id.isdecimal():
        return None, None, "The current Telegram chat and actor are required; no action was taken."
    return chat_id, user_id, None


def _live_dialogs(limit: int, chat_id: str, user_id: str) -> Optional[list[dict[str, Any]]]:
    """Live Telegram dialogs through the ``list_chats`` operation, or None.

    None means no live adapter (not a Telegram turn, bot disconnected) —
    the caller falls back to the cached sources. An operation error is also
    None: a failed refresh never fails the listing.
    """
    try:
        from gateway.config import Platform
        from model_tools import _run_async
        from tools.send_message_senders import _live_adapter
        from tools.send_message_tool import _dispatch_on_gateway_loop
        from tools.telegram_client_tool import _request_for_session

        runner, adapter = _live_adapter(Platform.TELEGRAM)
        if runner is None or adapter is None or getattr(adapter, "_client", None) is None:
            return None
        result = _run_async(_dispatch_on_gateway_loop(
            runner,
            lambda: _request_for_session(runner, adapter, chat_id, user_id, "list_chats", {"limit": limit}),
            "conversations_list dispatch failed",
        ))
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        data = result.get("data") or {}
        return data.get("chats") if isinstance(data.get("chats"), list) else None
    except Exception:  # health: allow BLE001 -- a failed live refresh falls back to cached sources
        logger.debug("conversations_list: live dialogs unavailable", exc_info=True)
        return None


def _cached_rows() -> list[dict[str, Any]]:
    """Registry rows merged with the channel directory's known chats."""
    from tools.conversation_registry import list_conversations

    rows = {row["ref"]: dict(row) for row in list_conversations(limit=500)}
    try:
        from gateway.channel_directory import load_directory

        for entry in (load_directory().get("platforms", {}).get("telegram") or []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            from tools.conversation_registry import conv_ref

            ref = conv_ref("telegram", entry["id"], entry.get("thread_id"))
            rows.setdefault(ref, {
                "ref": ref, "platform": "telegram", "chat_id": str(entry["id"]),
                "thread_id": entry.get("thread_id"), "type": entry.get("type"),
                "name": entry.get("name"), "username": None, "last_seen": 0,
                "last_message_id": None,
            })
    except Exception:  # health: allow BLE001 -- the directory is a fallback source, never a failure
        logger.debug("conversations_list: channel directory unavailable", exc_info=True)
    return list(rows.values())


def _project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": row.get("ref"), "platform": row.get("platform"),
        "chat_id": row.get("chat_id"), "thread_id": row.get("thread_id"),
        "type": row.get("type"), "name": row.get("name"),
        "username": row.get("username"),
        "unread_count": row.get("unread_count"),
        "last_message": row.get("last_message"),
    }


def conversations_list(
    limit: int = _LIST_DEFAULT_LIMIT, search: Optional[str] = None,
    platform: str = "telegram", parent_agent: Any = None,
) -> str:
    """List addressable conversations, freshest first, each with its conv_ ref."""
    try:
        limit = max(1, min(_LIST_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    platform = (platform or "telegram").strip().lower()
    if platform != "telegram":
        return tool_error("Only telegram conversations are addressable yet.", success=False)
    needle = (search or "").strip().casefold()

    rows: dict[str, dict[str, Any]] = {}
    source = "cached"
    chat_id, user_id, _error = _telegram_context()
    if chat_id is not None and user_id is not None:
        live = _live_dialogs(limit, chat_id, user_id)
        if live is not None:
            source = "live"
            from tools.conversation_registry import conv_ref, lookup, record_conversation

            for chat in live:
                ref = conv_ref("telegram", chat.get("id"))
                # A listing refresh records new/renamed chats; re-recording
                # every known chat on every listing would rewrite the file
                # once per row for only a fresher last_seen stamp.
                known = lookup("telegram", chat.get("id"))
                if (known is None or known.get("name") != chat.get("name")
                        or known.get("type") != chat.get("type")
                        or known.get("username") != chat.get("username")):
                    record_conversation(
                        "telegram", chat.get("id"), type=chat.get("type"),
                        name=chat.get("name"), username=chat.get("username"))
                rows[ref] = {
                    "ref": ref, "platform": "telegram", "chat_id": str(chat.get("id")),
                    "thread_id": None, "type": chat.get("type"), "name": chat.get("name"),
                    "username": chat.get("username"),
                    "unread_count": chat.get("unread_count"),
                    "last_message": chat.get("last_message"),
                    "last_seen": time.time(),
                }
    for row in _cached_rows():
        rows.setdefault(row["ref"], row)
    out = [row for row in rows.values()
           if not needle or needle in str(row.get("name") or "").casefold()]
    out.sort(key=lambda row: float(row.get("last_seen") or 0), reverse=True)
    out = out[:limit]
    return _ok(conversations=[_project(row) for row in out], count=len(out),
               source=source, limit_applied=limit)


def _send_to_chat(chat_id: str, message: str) -> tuple[Optional[int], Optional[str]]:
    """Send via the telegram_client path; (sent message id, error)."""
    from tools.telegram_client_tool import telegram_client

    try:
        raw = telegram_client({"action": "send_message", "params": {
            "recipient": chat_id, "content": message, "format": "auto"}})
        result = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:  # health: allow BLE001 -- a send failure is a tool error, never a crash
        return None, f"Telegram send failed: {type(exc).__name__}"
    if not isinstance(result, dict):
        return None, "Telegram send returned an unreadable result."
    if result.get("ok") is True:
        data = result.get("data") or {}
        sent_id = data.get("message_id")
        if isinstance(sent_id, int):
            return sent_id, None
        return None, "Telegram send returned no message id."
    error = result.get("error")
    if isinstance(error, dict):
        return None, str(error.get("message") or "Telegram send failed.")
    if isinstance(error, str) and error:
        return None, error
    return None, str(result.get("error") or "Telegram send failed.")


def conversations_turn(
    conversation_ref: Any = None, message: Any = None, timeout_seconds: Any = None,
    parent_agent: Any = None,
) -> str:
    """Send to one conversation by ref and wait for the reply that answers it.

    Returns ``status: "replied"`` with the reply, or ``status: "timeout"``
    when nobody answered in time — never a send receipt in place of a reply.
    """
    from tools.conversation_registry import record_conversation, resolve_ref
    from tools.conversation_reply_wait import (
        await_reply, cancel_waiter, register_waiter, set_sent_message,
    )

    if not isinstance(conversation_ref, str) or not conversation_ref.startswith("conv_"):
        return tool_error("conversation_ref must be a conv_ ref from conversations_list.", success=False)
    entry = resolve_ref(conversation_ref)
    if entry is None:
        return tool_error(
            f"Unknown conversation ref {conversation_ref}; list conversations first — the ref may "
            "belong to another profile or predate this registry.", success=False)
    if entry.get("platform") != "telegram":
        return tool_error("Only telegram conversations are addressable yet.", success=False)
    if not isinstance(message, str) or not message.strip():
        return tool_error("message must be a non-empty string.", success=False)
    if len(message) > _MESSAGE_MAX_CHARS:
        return tool_error(f"message must be at most {_MESSAGE_MAX_CHARS} characters.", success=False)
    timeout: float = _TURN_DEFAULT_TIMEOUT_S
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            return tool_error("timeoutSeconds must be a number.", success=False)
        timeout = max(1.0, min(_TURN_MAX_TIMEOUT_S, float(timeout_seconds)))
    _chat_id, _user_id, error = _telegram_context()
    if error is not None:
        return tool_error(error, success=False)
    if parent_agent is None:
        return tool_error("conversations_turn requires a live agent turn.", success=False)
    # Turn taint (PROVISIONAL, wave 5): a tainted turn cannot send messages to
    # another chat. This tool always sends to another conversation; the nested
    # telegram_client send it would make is covered by this refusal, not by a
    # second check down the call chain.
    from agent.turn_taint import refuse_tainted_cross_chat_send

    refusal = refuse_tainted_cross_chat_send(parent_agent)
    if refusal is not None:
        return tool_error(refusal, success=False)

    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    waiter = register_waiter(
        home, "telegram", entry["chat_id"], entry.get("thread_id"),
        chat_type=entry.get("type") or "dm", timeout_seconds=timeout)
    sent_id, send_error = _send_to_chat(str(entry["chat_id"]), message)
    if send_error is not None:
        cancel_waiter(waiter)
        return tool_error(send_error, success=False)
    set_sent_message(waiter, sent_id)
    record_conversation(
        "telegram", entry["chat_id"], thread_id=entry.get("thread_id"),
        type=entry.get("type"), name=entry.get("name"),
        username=entry.get("username"), last_message_id=sent_id)

    last_touch = [0.0]

    def _touch() -> None:
        now = time.monotonic()
        if now - last_touch[0] < 10.0:
            return
        last_touch[0] = now
        touch = getattr(parent_agent, "_touch_activity", None)
        if callable(touch):
            touch("waiting for a reply in another conversation")

    status, reply = await_reply(
        waiter,
        interrupted=lambda: bool(getattr(parent_agent, "_interrupt_requested", False)),
        on_slice=_touch,
    )
    base = {
        "conversation_ref": conversation_ref,
        "conversation": _project(entry),
        "sent_message_id": sent_id,
    }
    if status == "replied":
        return _ok(status="replied", reply={
            "message_id": (reply or {}).get("message_id"),
            "sender_id": (reply or {}).get("sender_id"),
            "text": (reply or {}).get("text"),
            "date": (reply or {}).get("date"),
        }, **base)
    if status == "interrupted":
        return tool_error(
            "The wait for the reply was interrupted; the message was sent. Check the "
            "conversation for a reply instead of resending.", success=False)
    return _ok(
        status="timeout", timeout_seconds=timeout,
        message=(
            "No reply arrived in that conversation within the wait. The message was sent; "
            "check the conversation later instead of resending it."),
        **base)


CONVERSATIONS_LIST_SCHEMA = {
    "name": "conversations_list",
    "description": (
        "List the Telegram conversations this bot account can message. "
        "Each has a stable conv_ ref for conversations_turn. Live dialogs when the bot client is connected, "
        "otherwise chats seen before. Use search to match a conversation by name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max conversations to return. Default 20, max 50.",
                "default": 20,
            },
            "search": {
                "type": "string",
                "description": "Match against the conversation name.",
            },
            "platform": {
                "type": "string",
                "description": "Only 'telegram' is addressable yet.",
                "default": "telegram",
            },
        },
        "required": [],
    },
}

CONVERSATIONS_TURN_SCHEMA = {
    "name": "conversations_turn",
    "description": (
        "Send one message to a Telegram conversation by its conv_ ref. "
        "Waits for the reply that answers it. Returns the actual reply from the other side — a message that "
        "replies to the sent message, or in a DM the next message after the send — or a "
        "timeout when nobody answers. Never resend on a timeout; the message went out."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conversation_ref": {
                "type": "string",
                "description": "The conv_ ref from conversations_list for the conversation to message.",
            },
            "message": {
                "type": "string",
                "description": "Message to send to that conversation.",
            },
            "timeoutSeconds": {
                "type": "number",
                "description": "Seconds to wait for the reply (default 60, max 300).",
                "default": 60,
            },
        },
        "required": ["conversation_ref", "message"],
    },
}


def check_conversation_requirements() -> bool:
    """No external requirement: cached sources always answer the listing."""
    return True


registry.register(
    name="conversations_list",
    toolset="conversations",
    schema=CONVERSATIONS_LIST_SCHEMA,
    handler=lambda args, **kw: conversations_list(
        limit=args.get("limit", _LIST_DEFAULT_LIMIT), search=args.get("search"),
        platform=args.get("platform", "telegram"), parent_agent=kw.get("parent_agent")),
    check_fn=check_conversation_requirements,
    emoji="💬")

registry.register(
    name="conversations_turn",
    toolset="conversations",
    schema=CONVERSATIONS_TURN_SCHEMA,
    handler=lambda args, **kw: conversations_turn(
        conversation_ref=args.get("conversation_ref"), message=args.get("message"),
        timeout_seconds=args.get("timeoutSeconds"), parent_agent=kw.get("parent_agent")),
    check_fn=check_conversation_requirements,
    emoji="💬")
