"""Structured Telegram operations through the active bot transport and native approvals."""
from __future__ import annotations

import json
import logging
from typing import Any

from gateway.config import Platform
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TELEGRAM_CLIENT_SCHEMA = {
    "name": "telegram_client",
    "description": (
        "Operate Telegram through the current conversation's bot identity. "
        "Any admitted human may use every action except purge, which is owner-only; "
        "use action=capabilities to discover the operations and their parameters. "
        "Reads and writes run immediately on call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Operation name; use capabilities to discover operations and their parameters."},
            "params": {"type": "object", "description": "Parameters for the operation, as described by capabilities. No credential or path parameters."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def telegram_client(args: dict[str, Any], **_kwargs: Any) -> str:
    """Resolve the trusted session route, then invoke its adapter's operation controller."""
    if (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower() != Platform.TELEGRAM.value:
        return tool_error("Telegram operations are available only inside a Telegram conversation.")
    chat_id = (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip()
    user_id = (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip()
    if not chat_id or not user_id.isdecimal():
        return tool_error("The current Telegram chat and human actor are required; no action was taken.")
    action = str(args.get("action") or "").strip()
    params = args.get("params", {})
    if not action or not isinstance(params, dict):
        return tool_error("An operation name and an object of parameters are required.")
    if any(key in params for key in ("user_is_owner", "confirmed", "confirmation_token")):
        return tool_error("Approval fields cannot be supplied as operation parameters.")
    # Turn taint (PROVISIONAL, wave 5): a tainted turn cannot send messages to
    # another chat — send_message only to this chat by numeric id, never
    # cross_chat_reply. Same-chat sends, reads, reactions, and pins are untouched.
    # Direct callers (conversations_turn's _send_to_chat) pass no parent_agent,
    # so they are gated at their own entry point instead; this check covers the
    # registry-dispatched path, which is the model's path.
    from agent.turn_taint import refuse_tainted_telegram_send

    refusal = refuse_tainted_telegram_send(
        _kwargs.get("parent_agent"), action=action, params=params, current_chat_id=chat_id)
    if refusal is not None:
        return tool_error(refusal)
    try:
        from tools.send_message_senders import _live_adapter
        runner, adapter = _live_adapter(Platform.TELEGRAM)
        if runner is not None:
            from gateway.session_identity import identity_of
            cached = getattr(runner, "_get_cached_session_source", None)
            session_key = get_session_env("HERMES_SESSION_KEY", "") or ""
            source = cached(session_key) if callable(cached) and session_key else None
            identity = identity_of(source)
            if identity is not None:
                adapter = identity.adapter()  # receiving transport may differ from runtime profile
        if runner is None or adapter is None or getattr(adapter, "_client", None) is None:
            return tool_error("The current profile's connected Telegram bot is unavailable; no action was taken.")
        from model_tools import _run_async
        from tools.send_message_tool import _dispatch_on_gateway_loop
        result = _run_async(_dispatch_on_gateway_loop(
            runner, lambda: _request_for_session(runner, adapter, chat_id, user_id, action, params),
            "Telegram operation dispatch failed",
        ))
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:
        # RPC exception text can contain secrets or untrusted peer-provided strings.
        logger.warning("Telegram operation failed (%s)", type(exc).__name__)
        return tool_error(f"Telegram operation failed: {type(exc).__name__}")


async def _request_for_session(runner: Any, adapter: Any, chat_id: str, user_id: str,
                               action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute on the owning loop; deny stale or cross-profile routes before any RPC."""
    from hermes_constants import get_hermes_home
    from pathlib import Path
    from gateway.session_identity import identity_of
    session_key = get_session_env("HERMES_SESSION_KEY", "") or ""
    cached = getattr(runner, "_get_cached_session_source", None)
    source = cached(session_key) if callable(cached) and session_key else None
    thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
    if source is None:
        source = adapter.build_source(
            chat_id, chat_type=get_session_env("HERMES_SESSION_CHAT_TYPE", "") or "dm",
            user_id=user_id, thread_id=thread_id,
            message_id=get_session_env("HERMES_SESSION_MESSAGE_ID", "") or None,
        )
    identity = identity_of(source)
    if identity is not None and identity.adapter() is not adapter:
        return {"error": "The Telegram conversation's receiving bot changed or is unavailable."}
    if (str(source.chat_id) != chat_id or str(source.user_id) != user_id
            or str(getattr(source, "thread_id", "") or "") != str(thread_id or "")
            or source.platform != Platform.TELEGRAM
            or getattr(source, "is_bot", False) or getattr(source, "profile_route_rejected", False)):
        return {"error": "The Telegram session route is stale or does not match this actor and topic."}
    home = adapter._settings_home(source)
    if home is None or Path(home).resolve() != Path(get_hermes_home()).resolve():
        return {"error": "The Telegram route does not belong to the active profile."}
    if not adapter._authorized(source):
        return {"error": "This Telegram account is not admitted in the active chat."}
    return await adapter.request_telegram_operation(source, action, params)


registry.register(name="telegram_client", toolset="telegram_admin", schema=TELEGRAM_CLIENT_SCHEMA,
                  handler=telegram_client)
