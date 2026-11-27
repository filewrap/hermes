#!/usr/bin/env python3
"""``autonomous_done`` — the agent-facing done channel for autonomous mode.

The agent calls this, in an autonomous turn only, to end the run: either
the goal is done, or the run waits for something only the owner can
supply. It wires straight into the run store
(``gateway/autonomous_state.py``): ``done`` records the run done, and the
turn end arms here is finished by the runner (live flag cleared, pinned
message unpinned + deleted, master setting left on); ``needs_input``
records the run waiting, and the owner's next message resumes it via
``resume_run`` (wired in ``gateway/autonomous_turn.begin``).

Hard gate, fail-closed: outside an autonomous turn — no live flag, no
running record for this session, a record that already ended, or an agent
that cannot prove it is the run's session — the tool refuses and changes
nothing. The tool is never eager: it lives in the ``autonomous`` toolset
(never in ``_HERMES_CORE_TOOLS``) and is named in the default defer list,
so ordinary turns never see it at all — no prompt bytes, no bridge
listing entry, no load path. Delegated children never get it either (see
``tools/delegate_tool_toolsets.py``).
"""

import json
import logging
from typing import Any, Optional

from agent.autonomous_context import is_autonomous_turn, request_autonomous_end
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


def _ok(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ack_for(action: str, summary: Optional[str]) -> str:
    """The final response the turn ends with: the completion / wait notice
    the owner sees. The summary is the agent's own words — what was done,
    or what it needs."""
    detail = summary.strip() if isinstance(summary, str) and summary.strip() else ""
    if action == "done":
        return f"Done — {detail}" if detail else "Done."
    return f"Waiting for your input: {detail}" if detail else "Waiting for your input."


def autonomous_done(
    action: Optional[str] = None, summary: Optional[str] = None,
    parent_agent: Any = None,
) -> str:
    """End the autonomous run: declare the goal done, or pause for owner input.

    Arms the deterministic turn end (the acknowledgment becomes the final
    response) and transitions the run record. Refuses — changing nothing —
    outside an autonomous turn, when the run already ended, or when the
    calling agent is not the run's session.
    """
    if parent_agent is None:
        return tool_error("autonomous_done requires a live agent turn.")
    if action not in ("done", "needs_input"):
        return tool_error('autonomous_done action must be "done" or "needs_input".')
    if summary is not None and not isinstance(summary, str):
        return tool_error("autonomous_done summary must be a string when provided.")
    if not is_autonomous_turn():
        return tool_error(
            "autonomous_done is only available inside an autonomous turn. "
            "Continue the work, or end the turn normally.")
    try:
        # Looked up at call time, like autonomous_state.autonomous_mode_enabled,
        # so tests can patch the hermes_constants seam.
        from hermes_constants import get_routing_process_hermes_home

        from gateway import autonomous_state
    except Exception:
        logger.debug("autonomous_done could not read the run store", exc_info=True)
        return tool_error("autonomous_done could not read the run store; try again.")

    home = get_routing_process_hermes_home()
    try:
        state = autonomous_state.load_state(home)
        run_id = state.get("current_run_id")
        if not bool(state.get("active")) or not isinstance(run_id, str) or not run_id:
            return tool_error(
                "No autonomous run is live. End the turn normally.")
        record = autonomous_state.load_run(home, run_id)
        if record is None:
            return tool_error(
                "The live autonomous run has no record. End the turn normally.")
        if str(record.get("state") or "") != "running":
            return tool_error(
                "The autonomous run already ended or is waiting. End the turn normally.")
        # The run belongs to one session; an agent that cannot prove it is
        # that session (a delegated child, a bare agent) never ends it.
        agent_key = getattr(parent_agent, "gateway_session_key", None)
        record_key = record.get("session_key")
        if not isinstance(agent_key, str) or not agent_key:
            return tool_error(
                "This agent is not the autonomous run's session. End the turn normally.")
        if record_key is not None and record_key != agent_key:
            return tool_error(
                "This agent is not the autonomous run's session. End the turn normally.")
        if action == "done":
            autonomous_state.mark_done(home, run_id)
        else:
            autonomous_state.mark_needs_input(home, run_id)
    except ValueError as exc:
        # Invalid transition (a race with /stop or disable): the run ended
        # under the agent; the turn ends normally, nothing is re-armed.
        logger.debug("autonomous_done refused: %s", exc)
        return tool_error(f"The autonomous run already ended: {exc}")
    except Exception:
        logger.debug("autonomous_done store failure", exc_info=True)
        return tool_error("autonomous_done could not update the run store; try again.")

    acknowledgment = _ack_for(action, summary)
    request_autonomous_end(parent_agent, action, acknowledgment)
    return _ok(status=action, acknowledgment=acknowledgment)


AUTONOMOUS_DONE_SCHEMA = {
    "name": "autonomous_done",
    "description": (
        "End the autonomous run when the goal is done, or pause for owner input. The turn ends "
        "with the acknowledgment as the final response. Use only inside an autonomous turn, when "
        "the goal is complete or you cannot continue without the owner."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["done", "needs_input"],
                "description": (
                    "\"done\" when the goal is complete; \"needs_input\" when you cannot continue "
                    "without something only the owner can supply. The run pauses and the owner's "
                    "next message resumes it."
                ),
            },
            "summary": {
                "type": "string",
                "description": (
                    "For done: what was accomplished. For needs_input: what you need from the owner. "
                    "Shown to the owner as the completion or wait notice."
                ),
            },
        },
        "required": ["action"],
    },
}


def check_autonomous_done_requirements() -> bool:
    """The tool is always reachable — availability is the session's
    toolset, never this probe (see tools/AGENTS.md). The handler gates
    the call itself."""
    return True


registry.register(
    name="autonomous_done",
    toolset="autonomous",
    schema=AUTONOMOUS_DONE_SCHEMA,
    handler=lambda args, **kw: autonomous_done(
        action=args.get("action"), summary=args.get("summary"),
        parent_agent=kw.get("parent_agent")),
    check_fn=check_autonomous_done_requirements,
    emoji="✅")
