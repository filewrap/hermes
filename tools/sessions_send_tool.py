#!/usr/bin/env python3
"""F4 ``sessions_send`` — message an existing session by its F2 key.

Two targets, in resolution order — both continue the session through the
machinery F3 built, never a new route:

- a live delegate-child run (run id, or the child's session id from
  ``agents_list``): when the child parked in ``sessions_yield`` waiting for
  a message, the message is delivered into that wait and the child's turn
  continues with it; when the child is running, the message is steered in
  at its next iteration boundary (``steer_subagent``).
- a Hermes session id (``sessions_list``): the message is delivered as a
  new turn in that session through the gateway's message-injection path
  (the same admission the plugin injection, heartbeat and completion
  delivery use — session-key admission through the session-authority FIFO,
  with the gateway's authorization and same-profile checks). The target
  must belong to this profile; a session in another profile, the caller's
  own session, or an unknown key is refused, never silently rerouted.

A delivery that cannot reach a live target fails closed with the reason;
nothing is queued for a session that does not exist.
"""

import json
import logging
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_MESSAGE_MAX_CHARS = 100_000


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _resolve_canonical(session_key: str, db: Any) -> tuple[Optional[str], Optional[str]]:
    """Canonical session id for *session_key* via the SessionDB, or (None, error)."""
    try:
        from tools.sessions_tools import _resolve_db

        resolved_db, owned, error = _resolve_db(db, write=False)
        if error is not None:
            return None, error
        try:
            canonical = resolved_db.resolve_resume_session_id(session_key)
            return str(canonical) if canonical else session_key, None
        finally:
            if owned:
                from tools.sessions_tools import _release

                _release(True, resolved_db)
    except Exception as exc:  # health: allow BLE001 -- a DB failure is a tool error, never a crash
        logger.debug("sessions_send: session lookup failed", exc_info=True)
        return None, f"Session lookup failed: {type(exc).__name__}"


def _send_to_child(
    session_key: str, message: str, parent_agent: Any,
) -> Optional[dict[str, Any]]:
    """Deliver to a live delegate-child run, or None when the key is not one.

    Returns the delivery receipt, or an ``{"error": ...}`` dict when the
    key names a run that cannot take the message.
    """
    from tools.subagent_run_registry import (
        _owns_run, deliver_message, find_run_by_child_session, get_run,
    )

    record = get_run(session_key) or find_run_by_child_session(session_key)
    if record is None:
        return None
    if parent_agent is None:
        return {"error": "Messaging a subagent run requires a live agent turn."}
    if not _owns_run(record, parent_agent):
        return {"error": "That subagent run belongs to another session."}
    if record.get("status") != "running":
        return {"error": f"That subagent run has already finished ({record.get('status')})."}
    run_id = str(record["run_id"])
    child_session_id = str(record.get("child_session_id") or run_id)
    if deliver_message(child_session_id, message):
        return {"status": "delivered", "delivery": "waiting_subagent", "runId": run_id,
                "sessionKey": child_session_id,
                "message": "The subagent was waiting for a message; it resumes with this one."}
    from tools.delegate_tool_registry import steer_subagent

    if steer_subagent(run_id, message):
        return {"status": "delivered", "delivery": "steered", "runId": run_id,
                "sessionKey": child_session_id,
                "message": "The message was queued into the running subagent; it lands at its "
                           "next iteration boundary."}
    return {"error": "The subagent finished before the message could be delivered."}


def _send_to_session(
    session_key: str, canonical: str, message: str, parent_agent: Any,
) -> dict[str, Any]:
    """Deliver as a new turn in a gateway session; receipt or error dict."""
    from gateway.run import _gateway_runner_ref

    runner = _gateway_runner_ref()
    if runner is None:
        return {"error": "No live gateway in this process; the session cannot be messaged from here."}
    entry = runner.session_store.lookup_by_session_id(canonical)
    if entry is None:
        return {"error": f"Session '{session_key}' is not live under this gateway."}
    from tools.subagent_run_registry import consume_session_wait

    was_waiting = False
    try:
        from tools.delegate_tool_registry import _resolve_session_lineage

        tip = _resolve_session_lineage(canonical, parent_agent)
    except Exception:  # health: allow BLE001 -- lineage is best-effort; the raw id still keys the claim lookup
        tip = canonical
    if consume_session_wait(tip) is not None or consume_session_wait(canonical) is not None:
        was_waiting = True
    try:
        from hermes_constants import get_hermes_home
        from model_tools import _run_async
        from tools.send_message_tool import _dispatch_on_gateway_loop

        delivered = _run_async(_dispatch_on_gateway_loop(
            runner,
            lambda: runner._dispatch_plugin_message_injection(
                session_key=entry.session_key, content=message,
                plugin_id="sessions_send", plugin_home=get_hermes_home()),
            "sessions_send dispatch failed",
        ))
    except Exception as exc:  # health: allow BLE001 -- a dispatch failure is a tool error, never a crash
        logger.debug("sessions_send: gateway dispatch failed", exc_info=True)
        return {"error": f"Gateway dispatch failed: {type(exc).__name__}"}
    if isinstance(delivered, dict) and delivered.get("error"):
        return {"error": str(delivered["error"])}
    if delivered is not True:
        return {"error": "The session refused the message (not admitted under this profile, "
                         "or its adapter is unavailable)."}
    return {"status": "delivered", "delivery": "session_turn",
            "session_id": canonical, "session_key": entry.session_key,
            "was_waiting": was_waiting,
            "message": ("The message starts a new turn in that session"
                        + ("; it was waiting for a message." if was_waiting else "."))}


def sessions_send(
    session_key: Any = None, message: Any = None,
    db: Any = None, current_session_id: Optional[str] = None, parent_agent: Any = None,
) -> str:
    """Message an existing session by its session id or subagent run id."""
    if not isinstance(session_key, str) or not session_key.strip():
        return tool_error("sessionKey must be a session id or subagent run id.", success=False)
    session_key = session_key.strip()
    if not isinstance(message, str) or not message.strip():
        return tool_error("message must be a non-empty string.", success=False)
    if len(message) > _MESSAGE_MAX_CHARS:
        return tool_error(f"message must be at most {_MESSAGE_MAX_CHARS} characters.", success=False)

    # Turn taint (PROVISIONAL, wave 5): a tainted turn cannot send messages to
    # another session — whether a live child run or another Hermes session.
    from agent.turn_taint import refuse_tainted_cross_chat_send

    refusal = refuse_tainted_cross_chat_send(parent_agent)
    if refusal is not None:
        return tool_error(refusal, success=False)

    child = _send_to_child(session_key, message, parent_agent)
    if child is not None:
        if "error" in child:
            return tool_error(child["error"], success=False)
        return _ok(**child)

    canonical, error = _resolve_canonical(session_key, db)
    if error is not None:
        return tool_error(error, success=False)
    if current_session_id and canonical == str(current_session_id):
        return tool_error("Cannot send a message to your own session; answer directly instead.",
                          success=False)
    receipt = _send_to_session(session_key, canonical, message, parent_agent)
    if "error" in receipt:
        return tool_error(receipt["error"], success=False)
    return _ok(**receipt)


SESSIONS_SEND_SCHEMA = {
    "name": "sessions_send",
    "description": (
        "Send a message to an existing session by its session id or subagent run id. A subagent "
        "waiting for a message resumes with it; a running subagent takes it at its next iteration "
        "boundary; any other session starts a new turn carrying it. The target must belong to this "
        "profile — another profile's session, your own session, or an unknown id is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sessionKey": {
                "type": "string",
                "description": "Session id or subagent run id to message.",
            },
            "message": {
                "type": "string",
                "description": "Message to deliver to that session.",
            },
        },
        "required": ["sessionKey", "message"],
    },
}


def check_sessions_send_requirements() -> bool:
    """No external requirement: a missing live target is a per-call error."""
    return True


registry.register(
    name="sessions_send",
    toolset="conversations",
    schema=SESSIONS_SEND_SCHEMA,
    handler=lambda args, **kw: sessions_send(
        session_key=args.get("sessionKey"), message=args.get("message"),
        db=kw.get("db"), current_session_id=kw.get("current_session_id"),
        parent_agent=kw.get("parent_agent")),
    check_fn=check_sessions_send_requirements,
    emoji="✉️")
