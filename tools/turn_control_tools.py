#!/usr/bin/env python3
"""F3 turn-control tools — ``sessions_yield`` + ``agents_wait``.

Thin agent tools over the run registry (``tools/subagent_run_registry.py``)
and the existing async-delegation completion path:

- ``sessions_yield`` claims this turn's pending background children and ends
  the turn deterministically: the loop stops after the current tool batch
  with the acknowledgment as the final response, and each child's completion
  re-enters as a new turn through the async-delegation completion event it
  already produces. The yield's private ``message`` rides that event so the
  resumed turn carries the context. A second yield for the same children
  reuses the claim and ends the turn normally instead of re-arming it.
- ``agents_wait`` joins runs in-turn without polling: it parks on the
  registry condition until a run settles, the timeout lapses, or the turn is
  interrupted. ``awaitResults`` joins every run with no deadline (the runs
  still settle through their own child timeouts; a wedged unit is
  force-finalized as ``stalled``).

``waitFor: "message"`` is resumable via ``sessions_send`` (F4): a delegate
child parks inside this call until its requester delivers a message (a
child has no turn-resume machinery — ``run_conversation`` is one-shot), and
a main agent ends the turn waiting, resuming as a new turn when the
message is delivered to its session.
"""

import json
import logging
import math
import time
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_NO_PENDING_MESSAGE = (
    "No pending child completion is owned by this turn. This call did not pause the turn or schedule a "
    "continuation. Continue unfinished work; return its final result when complete. For background tools, "
    "follow their result's continuation instructions."
)


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _pending_children_text(children: list[dict[str, Any]]) -> str:
    parts = []
    for child in children:
        label = child.get("goal") or child.get("run_id") or ""
        parts.append(f"{label} ({child.get('run_id')})" if label and child.get("run_id") else str(child.get("run_id") or ""))
    return "; ".join(p for p in parts if p)


def _ack_for(count: int) -> str:
    noun = "subagent" if count == 1 else "subagents"
    return f"Waiting for {count} {noun} to finish — I'll continue when their results arrive."


def _yield_wait_for_message(
    message: Optional[str], acknowledgment: Optional[str], parent_agent: Any,
) -> str:
    """Yield until the requester sends this session a message.

    A delegate child parks inside this call: its ``run_conversation`` is
    one-shot, so ending the turn would end the child — the wait lives in
    the tool call and the delivered message is its result, after which the
    turn continues. A main agent ends the turn waiting (acknowledgment as
    the final response) and resumes as a new turn when the message is
    delivered to its session. A second yield for an already-waiting session
    reuses the claim and ends the turn normally.
    """
    from agent.delegation_context import is_delegated_child_context

    if is_delegated_child_context():
        from tools.subagent_run_registry import await_message_wait

        session_id = str(getattr(parent_agent, "session_id", "") or "")
        last_touch = [0.0]

        def _touch() -> None:
            now = time.monotonic()
            if now - last_touch[0] < 10.0:
                return
            last_touch[0] = now
            touch = getattr(parent_agent, "_touch_activity", None)
            if callable(touch):
                touch("waiting for a message from the requester")

        status, delivered = await_message_wait(parent_agent, session_id, on_slice=_touch)
        if status == "delivered":
            return _ok(status="message_received", message=delivered)
        return tool_error(
            "The wait for a message was interrupted; no message arrived. Continue with what "
            "you have, or end your turn — do not wait again for the same message.")

    from tools.subagent_run_registry import _session_key, claim_session_wait, request_yield

    session_id = _session_key(parent_agent)
    outcome, _claim = claim_session_wait(
        session_id, message=message, acknowledgment=acknowledgment)
    if outcome == "reused":
        return _ok(
            status="already_pending",
            message=(
                "This session is already waiting for a message from its requester; the "
                "message will arrive as a new turn. This turn owns no new wait, so no "
                "yield is needed: end this turn normally."
            ),
        )
    ack = (acknowledgment.strip() if isinstance(acknowledgment, str) and acknowledgment.strip()
           else "Waiting for a message — I'll continue when it arrives.")
    request_yield(parent_agent, ack, [], message=message)
    return _ok(status="yielded_waiting", acknowledgment=ack)


def sessions_yield(
    message: Optional[str] = None, acknowledgment: Optional[str] = None,
    wait_for: Optional[str] = None, parent_agent: Any = None,
) -> str:
    """End this turn to await pending child completions (not a final result).

    Claims the pending background children owned by this session, arms the
    deterministic turn end, and attaches the private ``message`` to each
    child's async unit so the resumed turn carries it. Returns
    ``nothing_pending`` when the turn owns no pending completion (the turn
    is untouched) and ``already_pending`` when an earlier turn already
    yielded for these children (end the turn normally).
    """
    if parent_agent is None:
        return tool_error("sessions_yield requires a live agent turn.")
    if message is not None and not isinstance(message, str):
        return tool_error("message must be a string when provided.")
    if acknowledgment is not None and not isinstance(acknowledgment, str):
        return tool_error("acknowledgment must be a string when provided.")
    if wait_for is not None and wait_for != "message":
        return tool_error('waitFor must be "message" when provided.')
    if wait_for == "message":
        return _yield_wait_for_message(message, acknowledgment, parent_agent)

    from tools.subagent_run_registry import (
        claim_yield, pending_runs_for_session, request_yield,
    )

    pending = pending_runs_for_session(parent_agent)
    if not pending:
        return _ok(status="nothing_pending", message=_NO_PENDING_MESSAGE)
    run_ids = [entry["run_id"] for entry in pending]
    session_id = str(getattr(parent_agent, "session_id", "") or "")
    outcome, _claim = claim_yield(
        session_id, run_ids, message=message, acknowledgment=acknowledgment)
    if outcome == "reused":
        return _ok(
            status="already_pending",
            message=(
                "An earlier turn of this session already yielded for "
                f"{len(run_ids)} child session(s) whose completion is still pending: "
                f"{_pending_children_text(pending)}. Their completion will arrive in this session as a "
                "later turn; do not re-spawn, re-send, or poll to wake them. This turn owns no new "
                "claim, so no yield is needed: end this turn normally."
            ),
            pendingChildren=[
                {"runId": entry["run_id"], "label": entry.get("goal"), "state": "running"}
                for entry in pending
            ],
        )
    ack = acknowledgment.strip() if isinstance(acknowledgment, str) and acknowledgment.strip() else _ack_for(len(run_ids))
    if message:
        # Best-effort: the unit may have finalized between the claim and
        # this attach; the results still arrive, only the context line is lost.
        try:
            from tools.async_delegation import attach_yield_context
            unit_ids = {entry.get("unit_id") or entry.get("delegation_id") for entry in pending}
            for unit_id in sorted(u for u in unit_ids if u):
                attach_yield_context(str(unit_id), message)
        except Exception:  # health: allow BLE001 -- a missed attach never fails the yield
            logger.debug("sessions_yield: could not attach continuation message", exc_info=True)
    request_yield(parent_agent, ack, run_ids, message=message)
    return _ok(
        status="yielded", acknowledgment=ack, runIds=run_ids,
        pendingChildren=[
            {"runId": entry["run_id"], "label": entry.get("goal"), "state": "running"}
            for entry in pending
        ],
    )


def agents_wait(
    ids: Any = None, timeout_seconds: Any = None, await_results: Any = None,
    parent_agent: Any = None,
) -> str:
    """Join subagent runs in-turn, without polling.

    Returns settled completions plus the still-pending runIds (and per-id
    ``not_found`` / ``not_owner`` errors). Without ``awaitResults`` the call
    returns when any run completes, when nothing is pending, or at the
    timeout (default 30s). With ``awaitResults`` it joins every run until
    settlement or cancellation — mutually exclusive with ``timeoutSeconds``.
    Results collected here still arrive as the unit's completion message;
    treat a repeated delegation_id there as the same result.
    """
    from tools.subagent_run_registry import MAX_WAIT_IDS, await_runs

    if parent_agent is None:
        return tool_error("agents_wait requires a live agent turn.")
    if await_results is not None and not isinstance(await_results, bool):
        return tool_error("agents_wait awaitResults must be a boolean.")
    if await_results and timeout_seconds is not None:
        return tool_error("agents_wait with awaitResults=true cannot also specify timeoutSeconds.")
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        return tool_error("agents_wait ids must be an array of run id strings.")
    if len(ids) > MAX_WAIT_IDS:
        return tool_error(f"agents_wait supports at most {MAX_WAIT_IDS} ids.")
    run_ids = [i.strip() for i in dict.fromkeys(ids) if i.strip()]
    if not run_ids:
        return tool_error("agents_wait requires at least one non-empty run id.")
    timeout: Optional[float] = None
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            return tool_error("agents_wait timeoutSeconds must be a number.")
        if not math.isfinite(float(timeout_seconds)) or float(timeout_seconds) < 0:
            return tool_error("agents_wait timeoutSeconds must be a finite number >= 0.")
        timeout = float(timeout_seconds)
    if not await_results and timeout is None:
        timeout = 30.0
    state = await_runs(
        parent_agent, run_ids, timeout_seconds=timeout, await_results=bool(await_results))
    result: dict[str, Any] = {"completed": state["completed"], "pending": state["pending"]}
    if state["errors"]:
        result["errors"] = state["errors"]
    if not state["completed"] and not state["pending"] and state["errors"]:
        result["success"] = False
    return _ok(**result)


SESSIONS_YIELD_SCHEMA = {
    "name": "sessions_yield",
    "description": (
        "End this turn to await pending child completions, not submit a final result. Return completed "
        "work normally. Does not message other sessions. Omit waitFor when yielding for your own "
        "children; their completions resume this session. Set waitFor:\"message\" only when a subagent "
        "has no pending children and waits for its requester to send it a continuation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "waitFor": {
                "type": "string",
                "description": (
                    "Only for a subagent with no pending children of its own that waits for its "
                    "requester to send it a continuation. Omit when yielding for your own children."
                ),
            },
            "message": {
                "type": "string",
                "description": "Private context for the resumed turn; not sent to the user.",
            },
            "acknowledgment": {
                "type": "string",
                "description": (
                    "Optional brief waiting status shown to the user as your message; put answers in "
                    "normal assistant text."
                ),
            },
        },
        "required": [],
    },
}

AGENTS_WAIT_SCHEMA = {
    "name": "agents_wait",
    "description": (
        "Wait for subagent runs to settle and collect their results, without polling. Returns settled "
        "completions and the still-pending run ids; a run you do not own, or that does not exist, is "
        "reported per id instead of failing the call. Use run ids from the delegate_task dispatch "
        "response or action='list'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Run ids (subagent ids) to wait for, at most 1000.",
            },
            "timeoutSeconds": {
                "type": "number",
                "description": (
                    "Seconds to wait before returning whatever has settled (default 30). "
                    "Mutually exclusive with awaitResults."
                ),
            },
            "awaitResults": {
                "type": "boolean",
                "description": (
                    "Join all listed runs until settlement or cancellation, without polling. "
                    "Mutually exclusive with timeoutSeconds."
                ),
            },
        },
        "required": ["ids"],
    },
}


def check_turn_control_requirements() -> bool:
    """Turn control has no external requirements — always available."""
    return True


registry.register(
    name="sessions_yield",
    toolset="turn_control",
    schema=SESSIONS_YIELD_SCHEMA,
    handler=lambda args, **kw: sessions_yield(
        message=args.get("message"), acknowledgment=args.get("acknowledgment"),
        wait_for=args.get("waitFor"), parent_agent=kw.get("parent_agent")),
    check_fn=check_turn_control_requirements,
    emoji="⏸️")

registry.register(
    name="agents_wait",
    toolset="turn_control",
    schema=AGENTS_WAIT_SCHEMA,
    handler=lambda args, **kw: agents_wait(
        ids=args.get("ids"), timeout_seconds=args.get("timeoutSeconds"),
        await_results=args.get("awaitResults"), parent_agent=kw.get("parent_agent")),
    check_fn=check_turn_control_requirements,
    emoji="⏳")
