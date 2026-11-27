"""Manage Telegram Business Mode (chat automation) from plain words.

One owner-only tool, action-dispatched like ``telegram_client``. Reads and
mutations share the tool; every action returns JSON. State reads and writes
go through the wave-1 manager + store
(``plugins/platforms/telegram/business_state.py`` /
``business_manager.py``) built from the current profile home at call time —
never a module constant — so each served profile gets its own connections
and recent messages. Trigger and persona changes are persisted by
``business_config.save_business_settings`` (the same store the UI worker
writes); the manager itself never writes ``config.yaml``.

Delivery goes through the live adapter's ``send_business_message``
(the ``invokeWithBusinessConnection`` wrapper), resolved the same way
``telegram_client`` resolves it; when no adapter is connected the send
action reports that instead of delivering. Send is explicit-text only —
there is no draft to send. Pause / resume / mute / unmute are pure
state and work now.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tools.gateway_owner import owner_telegram_id, requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TELEGRAM_BUSINESS_TOOL_NAME = "telegram_business"

_ACTIONS = (
    "status", "recent", "connections", "send",
    "pause", "resume", "mute", "unmute", "trigger", "persona",
)

TELEGRAM_BUSINESS_SCHEMA = {
    "name": TELEGRAM_BUSINESS_TOOL_NAME,
    "description": (
        "Manage Telegram Business Mode: list business connections and "
        "recent customer messages; send a reply to a customer, pause "
        "or resume replying, mute a customer chat, and change the trigger "
        "and reply persona. Owner-only. Sending a reply to the customer "
        "needs the connected business adapter; when it is unavailable the "
        "send action reports that instead of delivering."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": (
                    "What to do: status, recent, connections, send, "
                    "pause, resume, mute, unmute, trigger, persona."
                ),
            },
            "connection_id": {
                "type": "string",
                "description": "Business connection id, for send with explicit text.",
            },
            "customer_chat_id": {
                "type": "string",
                "description": "Customer chat id, for send with explicit text and mute/unmute.",
            },
            "text": {
                "type": "string",
                "description": "Reply text to send, for send with explicit text.",
            },
            "trigger_mode": {
                "type": "string",
                "enum": ["direct", "trigger"],
                "description": "Replying trigger mode: direct (every customer message) or trigger (only on the trigger word).",
            },
            "trigger_word": {
                "type": "string",
                "description": "Single trigger word; empty string turns the word off.",
            },
            "persona": {
                "type": "string",
                "description": "Owner persona used when replying.",
            },
            "limit": {
                "type": "integer",
                "description": "How many recent messages to list (default 10, at most 50).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


async def _unavailable_send_message(*args: Any, **kwargs: Any) -> None:
    """Owner-DM delivery is never invoked from the tool context."""
    raise RuntimeError("business owner DM delivery is not available in the tool context")


async def _unavailable_reply(*args: Any, **kwargs: Any) -> str:
    """Reply turns are never invoked from the tool context."""
    raise RuntimeError("business replying is not available in the tool context")


def _owner_chat_ids(db: Any) -> list[str]:
    """Owner DM chat ids that own connections in this profile's store.

    The owner DM chat id IS the owner's user id, so the connection rows
    carry it; fall back to the current DM chat, then the launch owner's id.
    """
    seen: list[str] = []
    for row in db.list_telegram_business_connections(enabled_only=False):
        chat_id = str(row.get("owner_chat_id") or "").strip()
        if chat_id and chat_id not in seen:
            seen.append(chat_id)
    if seen:
        return seen
    from gateway.session_context import get_session_env

    chat_type = (get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "").strip().lower()
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    if chat_type in {"dm", "private"} and chat_id:
        return [chat_id]
    owner_id = (owner_telegram_id() or "").strip()
    return [owner_id] if owner_id else []


def _manager(home: Path, db: Any) -> Any:
    """State-only manager: no adapter, so delivery and replying stay unavailable."""
    from plugins.platforms.telegram.business_config import load_business_settings
    from plugins.platforms.telegram.business_manager import BusinessModeManager

    settings = load_business_settings(home)
    return BusinessModeManager(
        db,
        _unavailable_send_message,
        _unavailable_reply,
        send_business_message=None,
        debounce_seconds=settings["debounce_seconds"],
        max_customer_text_chars=settings["max_customer_text_chars"],
        persona=settings["owner_persona"],
        trigger_mode=settings["trigger_mode"],
        trigger_word=settings["trigger_word"],
    )


def _recent_messages(manager: Any, owner_chat_ids: list[str], limit: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for owner_chat_id in owner_chat_ids:
        messages.extend(manager.recent_messages(owner_chat_id, limit=limit))
    messages.sort(key=lambda m: (m.get("created_at") or 0, m.get("id") or 0), reverse=True)
    return messages[:limit]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def telegram_business(args: dict[str, Any], **kwargs: Any) -> str:
    """Owner-only Business Mode management; every action returns JSON."""
    if not requester_is_owner(kwargs.get("parent_agent")):
        return tool_error("Only the owner can manage Telegram Business Mode.")
    action = str(args.get("action") or "").strip()
    if not action:
        return tool_error("A business action is required.")
    if action not in _ACTIONS:
        return tool_error(
            f"Unknown business action: {action}. "
            f"Known actions: {', '.join(_ACTIONS)}.")
    from hermes_constants import get_hermes_home
    from plugins.platforms.telegram.business_config import load_business_settings
    from plugins.platforms.telegram.business_state import BusinessStateDB

    home = Path(get_hermes_home())
    db = BusinessStateDB(home / "telegram-business" / "state.db")
    try:
        manager = _manager(home, db)
        settings = load_business_settings(home)
        owner_chat_ids = _owner_chat_ids(db)
        return _dispatch(action, args, db, manager, settings, home,
                         owner_chat_ids, kwargs.get("parent_agent"))
    except ValueError as exc:
        return tool_error(str(exc) or "Could not apply this business change.")
    finally:
        db.close()


def _dispatch(action: str, args: dict[str, Any], db: Any, manager: Any,
              settings: dict[str, Any], home: Path, owner_chat_ids: list[str],
              parent_agent: Any = None) -> str:
    """Run one action against the state-only manager; the owner gate already passed."""
    ctx = SimpleNamespace(
        action=action, args=args, db=db, manager=manager,
        settings=settings, home=home, owner_chat_ids=owner_chat_ids,
        parent_agent=parent_agent,
    )
    return _ACTION_HANDLERS[action](ctx)


def _status(ctx: Any) -> str:
    return json.dumps({
        "enabled": ctx.settings["enabled"],
        "connections": ctx.manager.list_connections(enabled_only=False),
        "settings": ctx.settings,
    }, ensure_ascii=False)


def _recent(ctx: Any) -> str:
    limit = ctx.args.get("limit", 10)
    if _as_int(limit) is None or not 1 <= limit <= 50:
        return tool_error("limit must be a whole number from 1 to 50.")
    return json.dumps(
        {"messages": _recent_messages(ctx.manager, ctx.owner_chat_ids, limit)},
        ensure_ascii=False)


def _connections(ctx: Any) -> str:
    return json.dumps(
        {"connections": ctx.manager.list_connections(enabled_only=False)},
        ensure_ascii=False)


def _pause_resume(ctx: Any) -> str:
    enabled = ctx.action == "resume"
    active = ctx.manager.list_connections(enabled_only=True)
    for conn in active:
        ctx.manager.set_auto_draft(conn["connection_id"], enabled)
    result: dict[str, Any] = {
        "auto_draft": enabled,
        "connections": [c["connection_id"] for c in active],
    }
    if not active:
        result["note"] = "No active business connections."
    return json.dumps(result, ensure_ascii=False)


def _mute_unmute(ctx: Any) -> str:
    customer_chat_id = str(ctx.args.get("customer_chat_id") or "").strip()
    if not customer_chat_id:
        return tool_error("mute and unmute need a customer_chat_id.")
    muted = ctx.action == "mute"
    active = ctx.manager.list_connections(enabled_only=True)
    touched: list[str] = []
    for conn in active:
        paused = set(conn.get("paused_chats") or [])
        if muted:
            paused.add(customer_chat_id)
        else:
            paused.discard(customer_chat_id)
        ctx.manager.set_paused_chats(conn["connection_id"], sorted(paused))
        touched.append(conn["connection_id"])
    result: dict[str, Any] = {
        "customer_chat_id": customer_chat_id,
        "muted": muted,
        "connections": touched,
    }
    if not touched:
        result["note"] = "No active business connections."
    return json.dumps(result, ensure_ascii=False)


def _trigger(ctx: Any) -> str:
    from plugins.platforms.telegram.business_config import save_business_settings

    mode = ctx.args.get("trigger_mode")
    word = ctx.args.get("trigger_word")
    if mode is None and word is None:
        return json.dumps({
            "trigger_mode": ctx.settings["trigger_mode"],
            "trigger_word": ctx.settings["trigger_word"],
        }, ensure_ascii=False)
    provided = [key for key, value in
                (("trigger_mode", mode), ("trigger_word", word)) if value is not None]
    validated = ctx.manager.set_trigger(mode=mode, word=word)
    changes = {key: validated[key] for key in provided}
    saved = save_business_settings(ctx.home, changes)
    return json.dumps({
        "trigger_mode": saved["trigger_mode"],
        "trigger_word": saved["trigger_word"],
        "changed": sorted(changes),
    }, ensure_ascii=False)


def _persona(ctx: Any) -> str:
    from plugins.platforms.telegram.business_config import save_business_settings

    persona = ctx.args.get("persona")
    if persona is None:
        return json.dumps({"owner_persona": ctx.settings["owner_persona"]}, ensure_ascii=False)
    if not isinstance(persona, str) or not persona.strip():
        return tool_error("persona must be non-empty text.")
    ctx.manager.set_persona(persona)
    saved = save_business_settings(ctx.home, {"owner_persona": persona})
    return json.dumps({
        "owner_persona": saved["owner_persona"],
        "changed": ["owner_persona"],
    }, ensure_ascii=False)


def _deliver_via_adapter(parent_agent: Any, *, connection_id: str,
                         customer_chat_id: str, text: str) -> None:
    """Send one business reply through the live adapter.

    Resolves the adapter the same way ``telegram_client`` does
    (``tools.send_message_senders._live_adapter`` + gateway-loop
    dispatch). Raises RuntimeError with a plain message when there is
    no Telegram session, no connected adapter, the turn is tainted, or
    delivery fails.
    """
    from agent.turn_taint import refuse_tainted_telegram_send
    from gateway.config import Platform
    from gateway.session_context import get_session_env

    if ((get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
            != Platform.TELEGRAM.value):
        raise RuntimeError(
            "Business delivery needs the connected Telegram adapter; "
            "this turn is not inside a Telegram conversation.")
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    user_id = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    if not chat_id or not user_id.isdecimal():
        raise RuntimeError(
            "Business delivery needs the current Telegram chat and actor.")
    refusal = refuse_tainted_telegram_send(
        parent_agent, action="send_message",
        params={"recipient": customer_chat_id}, current_chat_id=chat_id)
    if refusal is not None:
        raise RuntimeError(refusal)
    from tools.send_message_senders import _live_adapter
    runner, adapter = _live_adapter(Platform.TELEGRAM)
    if runner is None or adapter is None or getattr(adapter, "_client", None) is None:
        raise RuntimeError(
            "The current profile's connected Telegram bot is unavailable; "
            "nothing was sent.")
    from model_tools import _run_async
    from plugins.platforms.telegram.business_adapter import send_business_message
    from tools.send_message_tool import _dispatch_on_gateway_loop
    _run_async(_dispatch_on_gateway_loop(
        runner,
        lambda: send_business_message(
            adapter, connection_id=connection_id,
            customer_chat_id=customer_chat_id, text=text),
        "Business send failed",
    ))


def _stored_customer_name(ctx: Any, customer_chat_id: str) -> str:
    """The customer's stored name from the last message for that chat."""
    for owner_chat_id in ctx.owner_chat_ids:
        rows = ctx.manager.recent_messages(owner_chat_id, limit=50)
        for row in rows:
            if str(row.get("customer_chat_id")) == str(customer_chat_id):
                name = (row.get("customer_name") or "").strip()
                if name:
                    return name
    return ""


def _send(ctx: Any) -> str:
    """Deliver explicit reply text via the live adapter.

    Refuses inside a business reply turn (thread ``biz-...``): the turn
    itself delivers its final text, so a tool send from inside it would
    double-send. Records the sent message in recent with the stored
    customer name.
    """
    from gateway.session_context import get_session_env

    thread_id = (get_session_env("HERMES_SESSION_THREAD_ID", "") or "").strip()
    if thread_id.startswith("biz-"):
        return tool_error(
            "Cannot send from inside a business reply turn — the turn "
            "delivers its own reply.")
    args, db = ctx.args, ctx.db
    connection_id = str(args.get("connection_id") or "").strip()
    customer_chat_id = str(args.get("customer_chat_id") or "").strip()
    text = str(args.get("text") or "").strip()
    if not connection_id or not customer_chat_id or not text:
        return tool_error(
            "send needs connection_id + customer_chat_id + text.")
    if db.get_telegram_business_connection(connection_id) is None:
        return tool_error(f"No business connection with id {connection_id}.")
    error = _try_deliver(ctx, connection_id, customer_chat_id, text)
    if error is not None:
        return tool_error(error)
    db.record_business_message(
        connection_id, customer_chat_id, None,
        _stored_customer_name(ctx, customer_chat_id), text, "out")
    return json.dumps({
        "status": "sent", "connection_id": connection_id,
        "customer_chat_id": customer_chat_id,
    }, ensure_ascii=False)


def _try_deliver(ctx: Any, connection_id: str, customer_chat_id: str,
                 text: str) -> str | None:
    """Deliver, returning an error string or None on success."""
    try:
        _deliver_via_adapter(
            ctx.parent_agent, connection_id=connection_id,
            customer_chat_id=customer_chat_id, text=text)
    except RuntimeError as exc:
        return str(exc) or "Business delivery failed."
    except Exception as exc:  # delivery raised through the gateway loop
        logger.warning("Business delivery failed", exc_info=True)
        return f"Business delivery failed: {type(exc).__name__}"
    return None


_ACTION_HANDLERS = {
    "status": _status,
    "recent": _recent,
    "connections": _connections,
    "send": _send,
    "pause": _pause_resume,
    "resume": _pause_resume,
    "mute": _mute_unmute,
    "unmute": _mute_unmute,
    "trigger": _trigger,
    "persona": _persona,
}


registry.register(
    name=TELEGRAM_BUSINESS_TOOL_NAME,
    toolset="telegram_admin",
    schema=TELEGRAM_BUSINESS_SCHEMA,
    handler=telegram_business,
)
