"""One persistent Telegram status message per swarm/delegation.

A :class:`SwarmStatusBoard` is the single message for one delegation: a compact
live summary on top (workers done/total, current activity) and one open,
formatted section per subagent below it, holding that agent's turns, spawned
children and outcome. Sections render open, never collapsed: the card must
show what each agent actually did, not hide it behind a dropdown. The state
rides on the observer's coloured badge inline right of "Task", never as prose here.

A :class:`SwarmStatusPublisher` owns the boards and the flood gate. Edits are
coalesced, never slept on: rapid updates inside one
:data:`COALESCE_WINDOW_SECONDS` window batch into a single pending edit, and
a board is only handed to the scheduler once :data:`MIN_EDIT_INTERVAL_SECONDS`
have passed since its last edit (matching the scheduler's minimum spacing
for one active card). Terminal boards bypass the gate so a finish is never
held back. The scheduler itself remains the chat-level serializer on top.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from gateway.swarm_observer import SwarmRoute

#: Minimum seconds between two edits of the same status message. Matches the
#: per-chat scheduler's minimum spacing for a single active card, so the
#: board-level gate and the scheduler never fight each other.
MIN_EDIT_INTERVAL_SECONDS = 10.0
#: Rapid updates arriving inside this window batch into one edit.
COALESCE_WINDOW_SECONDS = MIN_EDIT_INTERVAL_SECONDS
#: Most recent turn lines kept per agent; older lines drop, the summary stays.
MAX_TURN_LINES_PER_AGENT = 6
#: Rendered-message budget; oldest turn lines drop first when it is exceeded.
MAX_BOARD_CHARS = 8000

_MAX_LABEL_CHARS = 80
_MAX_TURN_CHARS = 200
_MAX_DETAIL_CHARS = 500

def _clean_text(text: Any, limit: int = _MAX_TURN_CHARS) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned


@dataclass
class SwarmAgentSection:
    """One subagent inside a board: its turns, spawned children, outcome."""

    label: str
    state: str = "pending"  # pending | running | done | failed
    turns: list[str] = field(default_factory=list)
    spawned: list[str] = field(default_factory=list)
    detail: Optional[str] = None


@dataclass
class SwarmStatusBoard:
    """The single persistent message for one delegation.

    ``delegation_id`` keys the board and is never rendered; ``route`` is the
    observer's :class:`SwarmRoute` (duck-typed here to avoid an import cycle).
    ``message_id`` is the Telegram message being edited in place.
    """

    delegation_id: str
    goal: str
    route: SwarmRoute
    started_at: float = 0.0
    state: str = "running"
    terminal: bool = False
    summary: Optional[str] = None
    failure_reason: Optional[str] = None
    message_id: Optional[str] = None
    agents: list[SwarmAgentSection] = field(default_factory=list)
    current_label: Optional[str] = None
    current_text: Optional[str] = None

    # ── recording ────────────────────────────────────────────────────────────

    def _ensure_agent(self, label: str, *, index: Optional[int] = None) -> SwarmAgentSection:
        if index is not None and 0 <= index < len(self.agents):
            section = self.agents[index]
            cleaned = _clean_text(label, _MAX_LABEL_CHARS)
            if cleaned:
                section.label = cleaned
            return section
        cleaned = _clean_text(label, _MAX_LABEL_CHARS) or "Worker"
        for section in self.agents:
            if section.label == cleaned:
                return section
        section = SwarmAgentSection(label=cleaned)
        self.agents.append(section)
        return section

    def sync_agents(self, agents: list[tuple[str, str]]) -> None:
        """Seed/refresh sections from the card's children (label, state pairs)
        so the board knows every worker up front, not only after its first
        event. Turn lines and spawned children are never touched."""
        for index, (label, state) in enumerate(agents):
            section = self._ensure_agent(label, index=index)
            section.state = state

    def record_started(
        self, label: str, *, index: Optional[int] = None, state: str = "running",
    ) -> SwarmAgentSection:
        section = self._ensure_agent(label, index=index)
        section.state = state
        self.current_label = section.label
        return section

    def record_turn(
        self, label: str, text: str, *, index: Optional[int] = None,
    ) -> SwarmAgentSection:
        section = self._ensure_agent(label, index=index)
        cleaned = _clean_text(text)
        if cleaned:
            section.turns.append(cleaned)
            del section.turns[: max(0, len(section.turns) - MAX_TURN_LINES_PER_AGENT)]
            self.current_label = section.label
            self.current_text = cleaned
        return section

    def record_spawned(self, parent_label: str, child_label: str) -> SwarmAgentSection:
        parent = self._ensure_agent(parent_label)
        child = _clean_text(child_label, _MAX_LABEL_CHARS)
        if child and child not in parent.spawned:
            parent.spawned.append(child)
        self._ensure_agent(child_label)
        return parent

    def record_completed(
        self, label: str, state: str, *, detail: Optional[str] = None,
        index: Optional[int] = None,
    ) -> SwarmAgentSection:
        section = self._ensure_agent(label, index=index)
        section.state = state
        cleaned = _clean_text(detail, _MAX_DETAIL_CHARS) if detail else ""
        section.detail = cleaned or None
        return section

    def finish(
        self, state: str, *, summary: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        self.state = state
        self.terminal = True
        if summary is not None:
            self.summary = _clean_text(summary, _MAX_DETAIL_CHARS)
        if failure_reason is not None:
            self.failure_reason = _clean_text(failure_reason, _MAX_DETAIL_CHARS)

    # ── rendering ────────────────────────────────────────────────────────────

    def render(self, now: float, *, button_row: Optional[str] = None,
               status_badge: Optional[str] = None) -> str:
        """Rich HTML: compact summary on top, one open section per agent.

        The state badge rides inline right of "Task" (handed in by the
        observer); the heading here is the kind only, never the state as
        words. A failed board always names what failed — never silent,
        never blank.
        """
        sections = self._render_sections()
        while len("\n".join(sections)) > MAX_BOARD_CHARS and self._drop_oldest_turn():
            sections = self._render_sections()
        task_line = "<p><b>Task</b>"
        if status_badge:
            task_line += f" • {status_badge}"
        task_line += "</p>"
        lines = [
            "<h1>Swarm</h1>",
            task_line,
            f"<pre>{html.escape(self.goal)}</pre>",
        ]
        done = sum(1 for agent in self.agents if agent.state == "done")
        total = len(self.agents)
        if total:
            lines.append(f"<p><b>Workers: {done} of {total} done</b></p>")
        if self.state in {"running", "stalling", "stalled"} and self.current_label:
            current = f"<p>Now: {html.escape(self.current_label)}"
            if self.current_text:
                current += f" — {html.escape(self.current_text)}"
            lines.append(current + "</p>")
        lines.extend(sections)
        if self.terminal:
            if self.summary:
                lines.append(f"<p>Result: {html.escape(self.summary)}</p>")
            elif self.state == "failed":
                lines.append(f"<p>{html.escape(self._failure_line())}</p>")
            elif self.failure_reason:
                lines.append(f"<p>Failed: {html.escape(self.failure_reason)}</p>")
        if button_row:
            lines.append(button_row)
        return "\n".join(lines)

    def _failure_line(self) -> str:
        """What exactly failed — never silent. The board's reason wins; else
        the failed agents, each named with its detail; else an honest 'no
        reason was reported' (the failure itself is still stated)."""
        if self.failure_reason:
            return f"Failed: {_clean_text(self.failure_reason, _MAX_DETAIL_CHARS)}"
        failed = [agent for agent in self.agents if agent.state == "failed"]
        reasons = [f"{agent.label}: {agent.detail}" for agent in failed if agent.detail]
        if reasons:
            return "Failed: " + "; ".join(reasons)
        if failed:
            labels = ", ".join(agent.label for agent in failed)
            return f"Failed: {labels} failed (no reason was reported)."
        return "Failed: the run failed (no reason was reported)."

    def _render_sections(self) -> list[str]:
        """One open section per agent: heading, spawned children, turns,
        outcome. No dropdown — what each agent did stays visible."""
        lines: list[str] = []
        for agent in self.agents:
            lines.append(
                f"<p><b>{html.escape(agent.label)} — {agent.state}</b></p>")
            for child in agent.spawned:
                lines.append(f"<p>Spawned: {html.escape(child)}</p>")
            for turn in agent.turns:
                lines.append(f"<p>{html.escape(turn)}</p>")
            if agent.detail:
                noun = "Failed" if agent.state == "failed" else "Result"
                lines.append(f"<p>{noun}: {html.escape(agent.detail)}</p>")
        return lines

    def _drop_oldest_turn(self) -> bool:
        """Drop one turn line from the agent holding the most; False when empty."""
        holder = max(self.agents, key=lambda agent: len(agent.turns), default=None)
        if holder is None or not holder.turns:
            return False
        holder.turns.pop(0)
        return True


class SwarmStatusPublisher:
    """Board registry plus the flood gate. No sleeps: ``due_boards`` answers
    which pending boards may be handed to the scheduler right now."""

    def __init__(self, *, clock: Optional[Any] = None) -> None:
        self._clock = clock or time.monotonic
        self.boards: dict[str, SwarmStatusBoard] = {}
        self._pending: set[str] = set()
        self._last_edit: dict[str, float] = {}

    def board_for(
        self, delegation_id: str, goal: Any, route: SwarmRoute, *,
        started_at: float = 0.0,
    ) -> SwarmStatusBoard:
        key = str(delegation_id)
        board = self.boards.get(key)
        if board is None:
            board = SwarmStatusBoard(
                delegation_id=key, goal=_clean_text(goal, 140) or "Untitled task",
                route=route, started_at=started_at,
            )
            self.boards[key] = board
        return board

    def note_update(self, board: SwarmStatusBoard) -> None:
        self._pending.add(board.delegation_id)

    def is_due(self, board: SwarmStatusBoard) -> bool:
        if board.delegation_id not in self._pending:
            return False
        if board.message_id is None or board.terminal:
            return True
        last = self._last_edit.get(board.delegation_id)
        if last is None:
            return True
        return self._clock() - last >= MIN_EDIT_INTERVAL_SECONDS

    def due_boards(self) -> list[SwarmStatusBoard]:
        return [self.boards[key] for key in list(self._pending)
                if key in self.boards and self.is_due(self.boards[key])]

    def mark_flushed(
        self, board: SwarmStatusBoard, *, message_id: Optional[str] = None,
    ) -> None:
        if message_id is not None:
            board.message_id = str(message_id)
        self._last_edit[board.delegation_id] = self._clock()
        self._pending.discard(board.delegation_id)
