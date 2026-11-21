"""Per-turn taint state (external-agent shape: ``turn-taint-state.ts``).

A turn is tainted once any tool outcome carries network-sourced content — a web
search/extract result, a browser page, an MCP tool result. Taint is sticky for
the turn and resets at the next turn's start. Hermes has no ``presentationOnly``
tool-result concept, so every network-sourced result taints.

The one consumer that ports without invention is memory-write provenance: a write on a
tainted turn is stamped ``origin_class: "untrusted"``, else ``"agent"``
(``turn_taint_origin_class`` below — the taint half of the full resolver in
``agent/memory_model.py::resolve_memory_origin_class``, which adds the ``system`` class
for non-interactive write origins). The external agent's other consumers do not port: Hermes has
no owner-only model tools (admission is gateway-level, and inventing an owner-tool
denial would be a new limit), no decision prefilter, and no transcript consumer.

The two refusals below are the enforcement half (wave 5). They are PROVISIONAL —
proposed and approved provisionally 2026-11-11, and the user will change them later;
build as proposed, do not extend them. A tainted turn:

- cannot approve things (``refuse_tainted_auto_approve`` — the two places the agent
  itself grants approval: ``suggest_task``'s autonomous auto-approve and the
  delegated child's auto-approve callback; the owner approving on a card is the
  owner acting, not the turn, and is untouched),
- cannot send messages to other chats (``refuse_tainted_cross_chat_send`` for tools
  that always send elsewhere; ``refuse_tainted_telegram_send`` for ``telegram_client``
  actions, where a numeric send to the current chat stays allowed and every other
  recipient is refused).

Enforcement lives in the tool handlers, at the point of effect, where the action,
the target and the current chat are all in scope. Handlers receive the agent as
``parent_agent`` (threaded through ``model_tools.handle_function_call`` from the
turn-loop dispatchers); a handler called with no agent (out-of-turn callers: MCP
server, code kernel, eval harnesses) sees no turn and no taint, so the helpers
fail open on ``agent=None``. A refused call returns a tool error and never runs;
a refusal never itself taints the turn.

State lives on the agent instance (``agent._turn_taint``), lazily created —
following the existing per-turn agent-attribute pattern (``_turn_user_intervened``)
and avoiding ContextVar thread-propagation issues in the concurrent executor.
"""
from __future__ import annotations

from typing import Any, Optional

# Registry entries for network-sourced tools carry this value.
NETWORK_RESULT_CONTENT_SOURCE = "network"

# Memory-write provenance classes (the external agent's resolveOriginClass values).
ORIGIN_CLASS_AGENT = "agent"
ORIGIN_CLASS_UNTRUSTED = "untrusted"


class TurnTaintState:
    """Sticky per-turn boolean: True once a network-sourced tool outcome is observed."""

    def __init__(self) -> None:
        self._tainted = False

    def observe(self, result_content_source: Optional[str], *, presentation_only: bool = False) -> None:
        """Mark the turn tainted when an outcome carries network content.

        ``presentation_only`` results (rendered for the user, never fed back into
        the turn) do not taint — Hermes has no such results today, so the flag
        exists for the external-agent-shaped contract only."""
        if result_content_source == NETWORK_RESULT_CONTENT_SOURCE and not presentation_only:
            self._tainted = True

    def is_tainted(self) -> bool:
        return self._tainted

    def reset(self) -> None:
        self._tainted = False


def get_turn_taint_state(agent: Any) -> TurnTaintState:
    """The agent's per-turn taint state, created lazily."""
    state = getattr(agent, "_turn_taint", None)
    if not isinstance(state, TurnTaintState):
        state = TurnTaintState()
        agent._turn_taint = state
    return state


def reset_turn_taint(agent: Any) -> None:
    get_turn_taint_state(agent).reset()


def observe_tool_outcome(agent: Any, tool_name: str, *, presentation_only: bool = False) -> bool:
    """Observe one tool outcome by name: taints when the tool's registry entry
    declares network-sourced results. Returns True when the turn is now tainted."""
    from tools.registry import registry
    entry = registry.get_entry(tool_name)
    source = entry.result_content_source if entry is not None else None
    state = get_turn_taint_state(agent)
    state.observe(source, presentation_only=presentation_only)
    return state.is_tainted()


def is_turn_tainted(agent: Any) -> bool:
    return get_turn_taint_state(agent).is_tainted()


def turn_taint_origin_class(agent: Any) -> str:
    """Memory-write provenance class for this turn (the external agent's resolveOriginClass)."""
    return ORIGIN_CLASS_UNTRUSTED if is_turn_tainted(agent) else ORIGIN_CLASS_AGENT


# ── Provisional refusals (wave 5) ──────────────────────────────────────────
# PROVISIONAL (approved provisionally 2026-11-11; the user will change these).
# Exactly these two refusals, at the handlers named below — do not extend them
# with new limits, guards, or alternative routes.


def _refusal(agent: Any, reason: str) -> Optional[str]:
    """*reason* when *agent*'s turn is tainted, else None.

    ``agent=None`` (an out-of-turn caller with no turn to taint) fails open.
    """
    if agent is None or not is_turn_tainted(agent):
        return None
    return reason


def refuse_tainted_cross_chat_send(agent: Any) -> Optional[str]:
    """Refusal for a tool that always sends to another chat/session.

    Covers ``conversations_turn`` (another conversation), ``sessions_send``
    (another session), and ``message_agent`` (a teammate DM). A tainted turn
    can still answer the user in the current chat.
    """
    return _refusal(
        agent,
        "This turn is tainted by network-sourced content, so it cannot send "
        "messages to another chat or session. Answer the user here instead, "
        "or start a fresh turn.",
    )


def refuse_tainted_telegram_send(
    agent: Any, *, action: str, params: Optional[dict] = None, current_chat_id: str = "",
) -> Optional[str]:
    """Refusal for a ``telegram_client`` send on a tainted turn.

    ``send_message`` stays allowed only to the current chat, named by numeric
    id — an @username recipient can never be proven to be this chat, so it is
    refused fail-closed. ``cross_chat_reply`` is always refused: its whole
    purpose is replying in another chat, and same-chat replies stay available
    through ``quote_reply``. Every other action (reads, same-chat sends,
    reactions, pins) is untouched.
    """
    if agent is None or not is_turn_tainted(agent):
        return None
    action = str(action or "").strip()
    if action == "cross_chat_reply":
        return (
            "This turn is tainted by network-sourced content, so it cannot reply "
            "in another chat. Use quote_reply for a reply in this chat, or start "
            "a fresh turn."
        )
    if action == "send_message":
        recipient = (params or {}).get("recipient")
        current = str(current_chat_id or "").strip()
        if recipient is not None and current and str(recipient).strip() == current:
            return None
        return (
            "This turn is tainted by network-sourced content, so it cannot send "
            "a message to another chat. Sending to this chat by numeric id stays "
            "allowed; anything else needs a fresh turn."
        )
    return None


def refuse_tainted_auto_approve(agent: Any) -> Optional[str]:
    """Refusal for the agent granting an approval on a tainted turn.

    Covers ``suggest_task``'s autonomous auto-approve (the suggestion stays
    pending for the owner instead). The owner's own approval on a card is the
    owner acting, not the turn, and is untouched.
    """
    return _refusal(
        agent,
        "This turn is tainted by network-sourced content, so it cannot "
        "auto-approve. The suggestion stays pending for the owner to approve.",
    )
