"""Whether THIS turn runs in autonomous mode.

One :class:`~contextvars.ContextVar`, owned by the runner: the runner sets
it around each turn of an autonomous session and clears it after. Every
behaviour that must know "is THIS turn autonomous" reads
:func:`is_autonomous_turn` — the representation the ``suggest_task`` seam's
docstring asks for. It carries no prompt bytes: autonomous mode adds zero
bytes to the system prompt.

The done channel rides the same turn: :func:`request_autonomous_end` /
:func:`consume_autonomous_end` arm and pop the agent's end-of-run request
(one attribute on the agent, same shape as the F3 yield in
``tools/subagent_run_registry.py``). The loop clears a stale one at turn
start, so a host that never consumes it cannot end a later turn.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

AUTONOMOUS_TURN: ContextVar[bool] = ContextVar("autonomous_turn", default=False)

#: Actions the done channel can declare.
AUTONOMOUS_END_ACTIONS = frozenset({"done", "needs_input"})


def set_autonomous_turn(enabled: bool) -> None:
    """Mark the current turn autonomous (or clear it back to ordinary)."""
    AUTONOMOUS_TURN.set(bool(enabled))


def is_autonomous_turn() -> bool:
    """True when the current turn runs in autonomous mode."""
    return bool(AUTONOMOUS_TURN.get())


def request_autonomous_end(agent: Any, action: str, acknowledgment: str) -> None:
    """Arm the deterministic autonomous turn end: the tool round ends this
    turn with the acknowledgment as the final response once the current
    tool batch finishes. The flag is per-turn state; the loop clears a
    stale one at turn start, so a host that never consumes it cannot end a
    later turn. The run record itself was already transitioned by the
    caller — this only carries the turn end."""
    agent._autonomous_done_requested = {
        "action": action, "acknowledgment": acknowledgment,
    }


def consume_autonomous_end(agent: Any) -> Optional[dict[str, str]]:
    """Pop the armed autonomous end request (None when none was armed this
    turn). An armed request with an unknown action is dropped, never
    honoured."""
    pending = getattr(agent, "_autonomous_done_requested", None)
    agent._autonomous_done_requested = None
    if not isinstance(pending, dict):
        return None
    action = pending.get("action")
    if action not in AUTONOMOUS_END_ACTIONS:
        return None
    acknowledgment = pending.get("acknowledgment")
    return {
        "action": str(action),
        "acknowledgment": str(acknowledgment) if isinstance(acknowledgment, str) else "",
    }
