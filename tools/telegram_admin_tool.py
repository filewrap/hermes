"""Agent actions for message history and pin state in the active Telegram chat."""
from __future__ import annotations

import json
import logging
from typing import Any

from gateway.config import Platform
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


TELEGRAM_ADMIN_SCHEMA = {
    "name": "telegram_chat_admin",
    "description": (
        "Read, search, or list pinned messages, and pin/unpin in this chat only. "
        "The current chat is taken from this turn; there is deliberately no chat_id argument, so "
        "this tool cannot target another conversation. Use the returned message_id for pin/unpin. "
        "History defaults to the active forum topic; set current_thread_only=false to search across the whole chat. "
        "In a topic, query and pinned filters are applied to the fetched page; use next_offset_id to scan older pages. "
        "Only pin or unpin when the user explicitly asks; pin operations are silent when Telegram "
        "allows it. Actions: list_messages, list_pinned_messages, pin_message, unpin_message."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_messages", "list_pinned_messages", "pin_message", "unpin_message"],
                "description": "The operation to perform in the active Telegram chat.",
            },
            "message_id": {
                "type": "string",
                "description": "Telegram message ID from this chat; required for pin_message and unpin_message.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Messages per page for list actions (default 20; maximum 50).",
            },
            "offset_id": {
                "type": "string",
                "description": "Pagination cursor: pass the last message_id from the previous page to read older messages.",
            },
            "query": {
                "type": "string",
                "description": "Optional text query. Whole-chat scope uses Telegram search; topic scope applies a literal match to the fetched topic page.",
            },
            "current_thread_only": {
                "type": "boolean",
                "description": "For list actions, stay in the active forum topic when true (default); false searches all topics in this chat.",
            },
        },
        "required": ["action"],
    },
}


def telegram_chat_admin(args: dict[str, Any], **_kwargs: Any) -> str:
    """Dispatch a bounded current-chat Telegram operation through the live adapter."""
    platform = (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
    if platform != Platform.TELEGRAM.value:
        return tool_error("Telegram chat actions are available only inside a Telegram conversation.")
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    if not chat_id:
        return tool_error("The active Telegram chat could not be determined; no action was taken.")

    action = str(args.get("action") or "").strip()
    allowed = {"list_messages", "list_pinned_messages", "pin_message", "unpin_message"}
    if action not in allowed:
        return tool_error("Unknown Telegram chat action.", available_actions=sorted(allowed))

    if action in {"pin_message", "unpin_message"} and not str(args.get("message_id") or "").strip():
        return tool_error(f"message_id is required for {action}.")

    try:
        from tools.send_message_senders import _live_adapter
        runner, adapter = _live_adapter(Platform.TELEGRAM)
        if runner is None or adapter is None:
            return tool_error("The active Telegram adapter is unavailable; no action was taken.")
        if getattr(adapter, "_client", None) is None:
            return tool_error("The active Telegram MTProto client is disconnected.")

        from model_tools import _run_async
        if action in {"list_messages", "list_pinned_messages"}:
            limit = max(1, min(int(args.get("limit", 20) or 20), 50))
            try:
                offset_id = max(0, int(args.get("offset_id", 0) or 0))
            except (TypeError, ValueError):
                return tool_error("offset_id must be a numeric Telegram message ID.")
            active_thread = get_session_env("HERMES_SESSION_THREAD_ID", "") or ""
            current_thread_only = args.get("current_thread_only", True)
            if not isinstance(current_thread_only, bool):
                return tool_error("current_thread_only must be true or false.")
            thread_filter = active_thread if active_thread and current_thread_only else None
            query = str(args.get("query") or "").strip() or None
            page = _run_async(adapter.list_chat_messages(
                chat_id, limit=limit, offset_id=offset_id,
                pinned_only=(action == "list_pinned_messages"),
                query=query, thread_id=thread_filter,
            ))
            messages = page["messages"]
            return json.dumps({
                "success": True,
                "action": action,
                "chat_id": chat_id,
                "current_thread_id": active_thread or None,
                "current_thread_only": current_thread_only,
                "query": query,
                "messages": messages,
                "next_offset_id": page["next_offset_id"],
                "scanned_count": page["scanned_count"],
            }, ensure_ascii=False)

        result = _run_async(adapter.pin_chat_message(
            chat_id, args.get("message_id"), unpin=(action == "unpin_message"),
        ))
        return json.dumps({"success": True, "action": action, "chat_id": chat_id, **result}, ensure_ascii=False)
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        error_type = type(exc).__name__
        logger.warning("Telegram current-chat action '%s' failed (%s)", action, error_type)
        if error_type in {"ChatAdminRequiredError", "ChatWriteForbiddenError"}:
            return tool_error("Telegram denied the action; the bot may need permission to pin messages in this chat.")
        return tool_error(f"Telegram chat action failed: {error_type}")


registry.register(
    name="telegram_chat_admin",
    toolset="telegram_admin",
    schema=TELEGRAM_ADMIN_SCHEMA,
    handler=telegram_chat_admin,
)
