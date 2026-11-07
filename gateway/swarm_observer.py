"""Telegram observer for agent/swarm spawns (Phase 2, Builder A).

Notices spawns as they happen and surfaces ONE status card per agent/swarm in
the invoking Telegram chat/thread, updated through the existing
:class:`gateway.telegram_progress_scheduler.TelegramProgressScheduler`
(kind ``"swarm"``) so rapid progress ticks coalesce to at most one edit per
10-60 s per chat. No message is ever sent per tick.

Seams (all pre-existing push or poll surfaces; no new core hooks):

- ``subagent.*`` / ``moa.*`` relay events via :meth:`SwarmObserver.on_progress_event`
  (the turn's ``tool_progress_callback``; wired in by the run_turn_runner patch
  in ``~/workspace/swarm-telegram/followups.md``).
- ``type="async_delegation"`` completion-queue events via
  :meth:`SwarmObserver.on_async_delegation_event` (forwarded from the gateway's
  ``_async_delegation_watcher``).
- Kanban lifecycle hooks (``kanban_task_claimed`` / ``kanban_task_completed`` /
  ``kanban_task_blocked`` / ``on_kanban_worker_spawned`` /
  ``on_kanban_dispatch_tick``) via :func:`register_kanban_hooks`.
- Polling via :meth:`SwarmObserver.poll_once` over
  ``list_async_delegations()`` and ``list_active_subagents()`` (the only way to
  see subagent-lifecycle children today: they register in the delegate registry
  while running, but there is no push hook and no completion outcome).

Cards are Rich HTML so the Cancel button rides in-body as a ``<tg-button>``
pill (auto-detected HTML routes through ``InputRichMessageHTML``). Callback
data is ``sw:<12-hex>:<action>`` (token registry, 64-byte cap). Card text never
carries internal identifiers (no subagent/delegation/task ids, no token, no
module paths) and no emojis anywhere.

Anchor rule (copied from the deepresearch fix): in a threaded DM the card
replies to the topic root (``thread_id``); strict-topic metadata is set only
when a thread is present. A card anchored anywhere else cannot be edited.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from gateway.swarm_status import SwarmStatusBoard, SwarmStatusPublisher

logger = logging.getLogger(__name__)

#: Poll interval for the gateway watcher (seconds). The scheduler still gates
#: edits to its own 10-60 s spacing; polling faster would only re-render.
POLL_INTERVAL_SECONDS = 15.0
#: A running card unseen by any seam for this long is treated as finished
#: (its result arrives in the normal turn message).
VANISH_GRACE_SECONDS = 60.0
#: Callback tokens live one hour, matching the lifecycle registry retention.
TOKEN_TTL_SECONDS = 3600.0
#: Scheduler card kind for swarm cards.
CARD_KIND = "swarm"

_MAX_GOAL_CHARS = 140
_MAX_LABEL_CHARS = 80
_MAX_REASON_CHARS = 200
_MAX_SUMMARY_CHARS = 500

_KANBAN_HOOK_NAMES = (
    "kanban_task_claimed",
    "kanban_task_completed",
    "kanban_task_blocked",
    "on_kanban_worker_spawned",
    "on_kanban_dispatch_tick",
)

_KIND_TITLES = {
    "delegate": "Agent",
    "lifecycle": "Agent",
    "kanban": "Swarm",
    "moa": "Mixture of agents",
}

_STATE_TITLES = {
    "running": "running",
    "stalling": "stalled",
    "stalled": "stalled",
    "done": "finished",
    "failed": "failed",
    "interrupted": "stopped",
}

# Async-delegation record status -> card state (terminal only).
_TERMINAL_DELEGATION_STATUS = {
    "completed": "done",
    "succeeded": "done",
    "done": "done",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
    "canceled": "interrupted",
}

_TOKEN_RE = re.compile(r"sw:([a-f0-9]{12}):([a-z_]+)")

_SUBAGENT_LIFECYCLE_EVENTS = {"subagent.spawn_requested", "subagent.start", "subagent.complete"}
_SUBAGENT_ACTIVITY_EVENTS = {
    "subagent.progress", "subagent_progress", "subagent.thinking", "subagent.tool", "subagent.text",
}
_MOA_EVENTS = {"moa.progress", "moa.reference", "moa.phase", "moa.aggregating"}


def _failure_statuses() -> frozenset:
    try:
        from tools.delegate_tool_progress import SUBAGENT_FAILURE_STATUSES

        return SUBAGENT_FAILURE_STATUSES
    except Exception:
        return frozenset({"failed", "error", "timeout"})


def _clean_text(text: Any, limit: int = _MAX_REASON_CHARS) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned


def _clean_goal(goal: Any) -> str:
    cleaned = _clean_text(goal, _MAX_GOAL_CHARS)
    return cleaned or "Untitled task"


def _clean_label(label: Any) -> str:
    return _clean_text(label, _MAX_LABEL_CHARS) or "Worker"


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


@dataclass(frozen=True)
class SwarmRoute:
    """Where one card lives: Telegram chat/thread + the session that spawned it."""

    platform: str
    chat_id: str
    chat_type: str = "dm"
    thread_id: Optional[str] = None
    user_id: Optional[str] = None
    session_key: str = ""
    profile: Optional[str] = None

    @property
    def is_telegram(self) -> bool:
        return self.platform == "telegram"

    @classmethod
    def from_session_key(cls, session_key: Any) -> Optional[SwarmRoute]:
        """Telegram route for a gateway session key, else None (CLI / other platform)."""
        if not session_key or not isinstance(session_key, str):
            return None
        try:
            from gateway.run import _parse_session_key

            parsed = _parse_session_key(session_key)
        except Exception:
            logger.debug("Swarm observer could not parse session key", exc_info=True)
            return None
        if not parsed or parsed.get("platform") != "telegram":
            return None
        chat_id = str(parsed.get("chat_id") or "")
        if not chat_id:
            return None
        thread_id = parsed.get("thread_id")
        return cls(
            platform="telegram",
            chat_id=chat_id,
            chat_type=str(parsed.get("chat_type") or "dm"),
            thread_id=str(thread_id) if thread_id else None,
            session_key=session_key,
            profile=parsed.get("profile"),
        )


@dataclass
class SwarmChild:
    """One worker/reference inside a card. ``ref`` is the live subagent id used
    only for cancel; it is never rendered."""

    label: str
    state: str = "pending"  # pending | running | done | failed
    ref: Optional[str] = None
    reason: Optional[str] = None


@dataclass(eq=False)
class SwarmCard:
    """One observed unit and its Telegram card."""

    kind: str  # delegate | lifecycle | kanban | moa
    unit_id: str
    goal: str
    route: SwarmRoute
    state: str = "running"  # running | stalling | stalled | done | failed | interrupted
    children: list[SwarmChild] = field(default_factory=list)
    started_at: float = 0.0
    updated_at: float = 0.0
    last_seen_at: float = 0.0
    failure_reason: Optional[str] = None
    summary: Optional[str] = None
    token: str = ""
    token_created_at: float = 0.0
    card_message_id: Optional[str] = None
    terminal: bool = False
    dirty: bool = False


class SwarmObserver:
    """State, rendering, tokens and delivery for swarm/agent status cards.

    ``resolve_adapter`` maps a route to the Telegram adapter that answers it
    (gateway: ``runner._delivery_adapter_for`` over a rebuilt source).
    ``resolve_route_for_session_id`` maps a parent agent session id to its
    route (gateway: the session store's ``lookup_by_session_id``); without it,
    live sync children found by polling cannot be routed and are only observed
    through progress events.
    """

    def __init__(
        self,
        *,
        resolve_adapter: Optional[Callable[[SwarmRoute], Any]] = None,
        resolve_route_for_session_id: Optional[Callable[[str], Optional[SwarmRoute]]] = None,
        clock: Optional[Callable[[], float]] = None,
        status_publisher: Optional[SwarmStatusPublisher] = None,
    ) -> None:
        self._resolve_adapter = resolve_adapter
        self._resolve_route_for_session_id = resolve_route_for_session_id
        self._clock = clock or time.time
        self.cards: dict[tuple[str, str], SwarmCard] = {}
        self._tokens: dict[str, SwarmCard] = {}
        self._kanban_bindings: dict[str, dict[str, int]] = {}
        # One persistent status message per delegation; the per-agent card for
        # a boarded delegation is never sent (see refresh_card).
        self.status_publisher = status_publisher or SwarmStatusPublisher()

    # ── cards ────────────────────────────────────────────────────────────────

    def observe(
        self, *, kind: str, unit_id: str, goal: Any, route: SwarmRoute,
        children: Optional[list[SwarmChild]] = None,
    ) -> SwarmCard:
        """Get-or-create the card for one unit (one card per agent/swarm)."""
        key = (str(kind), str(unit_id))
        card = self.cards.get(key)
        if card is None:
            self._prune_tokens()
            token = secrets.token_hex(6)
            while token in self._tokens:
                token = secrets.token_hex(6)
            now = self._clock()
            card = SwarmCard(
                kind=str(kind), unit_id=str(unit_id), goal=_clean_goal(goal), route=route,
                children=list(children or []), started_at=now, updated_at=now,
                last_seen_at=now, token=token, token_created_at=now, dirty=True,
            )
            self.cards[key] = card
            self._tokens[token] = card
        else:
            cleaned = _clean_goal(goal)
            if cleaned != "Untitled task" and card.goal == "Untitled task":
                card.goal = cleaned
        return card

    def finish_unit(
        self, kind: str, unit_id: str, *, state: str = "done",
        summary: Optional[str] = None, failure_reason: Optional[str] = None,
    ) -> Optional[SwarmCard]:
        """Mark a card terminal (no-op when unknown or already terminal)."""
        card = self.cards.get((str(kind), str(unit_id)))
        if card is None or card.terminal:
            return None
        card.state = state
        card.terminal = True
        if summary is not None:
            card.summary = _clean_text(summary, _MAX_SUMMARY_CHARS)
        if failure_reason is not None:
            card.failure_reason = _clean_text(failure_reason, _MAX_REASON_CHARS)
        for child in card.children:
            if child.state in {"pending", "running"}:
                child.state = "done" if state == "done" else "failed"
        self._mark_dirty(card)
        return card

    def _mark_dirty(self, card: SwarmCard) -> None:
        card.dirty = True
        card.updated_at = self._clock()

    # ── progress events (delegate_task relay + MoA relay) ────────────────────

    def on_progress_event(
        self, event_type: str, tool_name: Optional[str] = None, preview: Optional[str] = None,
        args: Any = None, *, route: Optional[SwarmRoute] = None,
        run_key: Optional[str] = None, **kwargs: Any,
    ) -> Optional[SwarmCard]:
        """Consume one relay event. Unknown events and events without a route
        are ignored; activity ticks refresh liveness only."""
        event = str(event_type or "")
        if route is None or not isinstance(route, SwarmRoute):
            return None
        if event.startswith("subagent.") or event == "subagent_progress":
            return self._on_subagent_event(
                event, preview, tool_name=tool_name, route=route, **kwargs)
        if event.startswith("moa."):
            return self._on_moa_event(event, route=route, run_key=run_key, **kwargs)
        return None

    def _on_subagent_event(
        self, event: str, preview: Optional[str], *, tool_name: Optional[str] = None,
        route: SwarmRoute, **kwargs: Any,
    ) -> Optional[SwarmCard]:
        if event not in _SUBAGENT_LIFECYCLE_EVENTS | _SUBAGENT_ACTIVITY_EVENTS:
            return None
        unit_id = str(kwargs.get("delegation_id") or kwargs.get("subagent_id") or "")
        if not unit_id:
            return None
        goal = str(kwargs.get("goal") or preview or "")
        try:
            task_count = max(1, int(kwargs.get("task_count") or 1))
        except (TypeError, ValueError):
            task_count = 1
        try:
            task_index = int(kwargs.get("task_index") or 0)
        except (TypeError, ValueError):
            task_index = 0
        card = self.observe(
            kind="delegate", unit_id=unit_id, goal=goal, route=route,
            children=[SwarmChild(label=f"Worker {i + 1}") for i in range(task_count)],
        )
        while len(card.children) < task_count:
            card.children.append(SwarmChild(label=f"Worker {len(card.children) + 1}"))
        card.last_seen_at = self._clock()
        child = card.children[task_index] if 0 <= task_index < len(card.children) else None
        if child is None:
            return card
        if kwargs.get("subagent_id"):
            child.ref = str(kwargs["subagent_id"])
        if event == "subagent.spawn_requested":
            if goal:
                child.label = _clean_label(goal)
            self._mark_dirty(card)
        elif event == "subagent.start":
            child.state = "running"
            if goal:
                child.label = _clean_label(goal)
            self._mark_dirty(card)
        elif event == "subagent.complete":
            status = str(kwargs.get("status") or "")
            reason = _clean_text(
                kwargs.get("summary") or preview or kwargs.get("failure_reason") or "")
            if status in _failure_statuses():
                child.state = "failed"
                child.reason = reason or None
                card.failure_reason = reason or None
            else:
                child.state = "done"
            if all(c.state in {"done", "failed"} for c in card.children):
                failed = any(c.state == "failed" for c in card.children)
                self.finish_unit(
                    "delegate", unit_id, state="failed" if failed else "done",
                    failure_reason=card.failure_reason,
                    summary=str(kwargs.get("summary") or "") or None,
                )
            else:
                self._mark_dirty(card)
        else:  # progress / thinking / tool / text ticks: liveness only, no re-render
            card.updated_at = self._clock()
        self._mirror_subagent_event_to_board(
            card, event, child, tool_name=tool_name, preview=preview, **kwargs)
        return card

    # ── persistent status board (one message per delegation) ─────────────────

    def board_for(self, delegation_id: str) -> Optional[SwarmStatusBoard]:
        """The status board for a delegation, if its events created one."""
        return self.status_publisher.boards.get(str(delegation_id))

    def _status_board(self, card: SwarmCard) -> Optional[SwarmStatusBoard]:
        if card.kind != "delegate":
            return None
        return self.status_publisher.boards.get(card.unit_id)

    def _ensure_status_board(self, card: SwarmCard) -> SwarmStatusBoard:
        board = self._status_board(card)
        if board is None:
            board = self.status_publisher.board_for(
                card.unit_id, card.goal, card.route, started_at=card.started_at)
        board.sync_agents([(child.label, child.state) for child in card.children])
        return board

    def _mirror_subagent_event_to_board(
        self, card: SwarmCard, event: str, child: SwarmChild, *,
        tool_name: Optional[str] = None, preview: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Record one subagent event on the delegation's status board.

        The per-agent card keeps the same state in parallel; only the board
        is delivered for a boarded delegation (see refresh_card)."""
        board = self._ensure_status_board(card)
        try:
            task_index = int(kwargs.get("task_index") or 0)
        except (TypeError, ValueError):
            task_index = 0
        if event == "subagent.spawn_requested":
            parent = kwargs.get("parent_label") or kwargs.get("spawned_by")
            if parent:
                board.record_spawned(str(parent), child.label)
            board.record_started(child.label, index=task_index, state="pending")
        elif event == "subagent.start":
            board.record_started(child.label, index=task_index)
        elif event == "subagent.complete":
            status = str(kwargs.get("status") or "")
            reason = _clean_text(
                kwargs.get("summary") or preview or kwargs.get("failure_reason") or "")
            board.record_completed(
                child.label, "failed" if status in _failure_statuses() else "done",
                detail=reason or None, index=task_index)
            if card.terminal:
                board.finish(
                    card.state, summary=card.summary,
                    failure_reason=card.failure_reason)
        else:  # activity ticks become turn lines inside the agent's section
            text = _clean_text(preview or "")
            if tool_name and text:
                text = f"{tool_name}: {text}"
            elif tool_name:
                text = str(tool_name)
            if text:
                board.record_turn(child.label, text, index=task_index)
        self.status_publisher.note_update(board)

    def _on_moa_event(
        self, event: str, *, route: SwarmRoute, run_key: Optional[str] = None, **kwargs: Any,
    ) -> Optional[SwarmCard]:
        if event not in _MOA_EVENTS:
            return None
        unit_id = str(run_key or f"moa:{route.session_key or route.chat_id}")
        try:
            total = max(0, int(kwargs.get("moa_refs_total") or kwargs.get("moa_count") or 0))
        except (TypeError, ValueError):
            total = 0
        try:
            done = max(0, int(kwargs.get("moa_refs_done") or 0))
        except (TypeError, ValueError):
            done = 0
        card = self.observe(
            kind="moa", unit_id=unit_id, goal="Mixture of agents run", route=route,
            children=[SwarmChild(label=f"Reference {i + 1}") for i in range(total)],
        )
        while len(card.children) < total:
            card.children.append(SwarmChild(label=f"Reference {len(card.children) + 1}"))
        card.last_seen_at = self._clock()
        if event == "moa.reference":
            try:
                index = int(kwargs.get("moa_index") or 0)
            except (TypeError, ValueError):
                index = 0
            label = str(kwargs.get("label") or "")
            if 0 <= index < len(card.children):
                if label:
                    card.children[index].label = _clean_label(label)
                if card.children[index].state == "pending":
                    card.children[index].state = "running"
            self._mark_dirty(card)
        elif event == "moa.progress":
            for i, child in enumerate(card.children):
                if i < done:
                    child.state = "done"
                elif i == done and child.state == "pending":
                    child.state = "running"
            self._mark_dirty(card)
        else:  # moa.phase / moa.aggregating: references done, aggregation in flight
            for child in card.children:
                child.state = "done"
            self._mark_dirty(card)
        return card

    # ── async-delegation completion queue ────────────────────────────────────

    def on_async_delegation_event(self, evt: dict) -> list[SwarmCard]:
        """Consume one ``type="async_delegation"`` queue event. Only cards the
        observer already knows are updated (a completion for an unobserved unit
        already reaches the chat as the normal completion message); redelivery
        is a no-op."""
        if not isinstance(evt, dict) or evt.get("type") != "async_delegation":
            return []
        delegation_id = str(evt.get("delegation_id") or "")
        if not delegation_id:
            return []
        route = SwarmRoute.from_session_key(str(evt.get("session_key") or ""))
        if route is None:
            return []
        card = self.cards.get(("delegate", delegation_id))
        if card is None or card.terminal:
            return []
        if evt.get("task_failure_notice"):
            board = self._status_board(card)
            for entry in evt.get("results") or []:
                if not isinstance(entry, dict):
                    continue
                try:
                    index = int(entry.get("task_index") or 0)
                except (TypeError, ValueError):
                    index = 0
                if 0 <= index < len(card.children):
                    card.children[index].state = "failed"
                    reason = _clean_text(entry.get("error") or entry.get("summary") or "")
                    card.children[index].reason = reason or None
                    if reason:
                        card.failure_reason = reason
                    if board is not None:
                        board.record_completed(
                            card.children[index].label, "failed",
                            detail=reason or None, index=index)
            if board is not None:
                self.status_publisher.note_update(board)
            card.last_seen_at = self._clock()
            self._mark_dirty(card)
            return [card]
        status = str(evt.get("status") or "")
        terminal = _TERMINAL_DELEGATION_STATUS.get(status)
        if terminal is None:
            card.last_seen_at = self._clock()
            return []
        self._apply_batch_results(card, evt.get("results") or [])
        finished = self.finish_unit(
            "delegate", delegation_id, state=terminal,
            failure_reason=str(evt.get("error") or "") or None,
            summary=str(evt.get("summary") or "") or None,
        )
        if finished is not None:
            board = self._status_board(finished)
            if board is not None:
                board.finish(
                    terminal, summary=finished.summary,
                    failure_reason=finished.failure_reason)
                self.status_publisher.note_update(board)
        return [finished] if finished is not None else []

    @staticmethod
    def _apply_batch_results(card: SwarmCard, results: Any) -> None:
        for entry in results or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("task_index") or 0)
            except (TypeError, ValueError):
                index = 0
            if not 0 <= index < len(card.children):
                continue
            status = str(entry.get("status") or "")
            if status in _failure_statuses():
                card.children[index].state = "failed"
                reason = _clean_text(entry.get("error") or entry.get("summary") or "")
                card.children[index].reason = reason or None
            elif status:
                card.children[index].state = "done"

    # ── Kanban hooks ─────────────────────────────────────────────────────────

    def bind_kanban_swarm(
        self, root_task_id: str, route: SwarmRoute, *, goal: Any,
        worker_ids: Any, verifier_id: Optional[str] = None,
        synthesizer_id: Optional[str] = None, worker_labels: Optional[Any] = None,
    ) -> SwarmCard:
        """Bind a Kanban swarm root to its chat route (hook payloads carry no
        route, so without this binding Kanban events are ignored)."""
        root = str(root_task_id)
        children: list[SwarmChild] = []
        mapping: dict[str, int] = {}
        labels = list(worker_labels or [])
        for i, worker_id in enumerate(worker_ids or []):
            mapping[str(worker_id)] = len(children)
            label = labels[i] if i < len(labels) and labels[i] else f"Worker {i + 1}"
            children.append(SwarmChild(label=_clean_label(label)))
        if verifier_id:
            mapping[str(verifier_id)] = len(children)
            children.append(SwarmChild(label="Verify"))
        if synthesizer_id:
            mapping[str(synthesizer_id)] = len(children)
            children.append(SwarmChild(label="Synthesize"))
        self._kanban_bindings[root] = mapping
        return self.observe(kind="kanban", unit_id=root, goal=goal, route=route, children=children)

    def on_kanban_hook(self, hook_name: str, **payload: Any) -> Optional[SwarmCard]:
        """Consume one Kanban lifecycle hook. Worker PIDs and workspace paths
        are deliberately never rendered. Dispatch ticks are too noisy for a card."""
        name = str(hook_name or "")
        if name == "on_kanban_dispatch_tick":
            return None
        if name not in {
            "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
            "on_kanban_worker_spawned",
        }:
            return None
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return None
        for root, mapping in self._kanban_bindings.items():
            if task_id != root and task_id not in mapping:
                continue
            card = self.cards.get(("kanban", root))
            if card is None or card.terminal:
                return None
            if task_id == root:
                # The root's own completion only finishes the card once every
                # worker/verifier/synthesizer is terminal; the creation-time
                # root event arrives before any binding exists.
                if name == "kanban_task_completed" and all(
                    c.state in {"done", "failed"} for c in card.children
                ):
                    return self.finish_unit(
                        "kanban", root,
                        state="failed" if any(c.state == "failed" for c in card.children) else "done",
                    )
                return None
            child = card.children[mapping[task_id]]
            if name in {"kanban_task_claimed", "on_kanban_worker_spawned"}:
                child.state = "running"
            elif name == "kanban_task_completed":
                child.state = "done"
            elif name == "kanban_task_blocked":
                child.state = "failed"
                reason = _clean_text(payload.get("reason") or "")
                child.reason = reason or None
                if reason:
                    card.failure_reason = reason
            self._mark_dirty(card)
            if all(c.state in {"done", "failed"} for c in card.children):
                return self.finish_unit(
                    "kanban", root,
                    state="failed" if any(c.state == "failed" for c in card.children) else "done",
                    failure_reason=card.failure_reason,
                )
            return card
        return None

    def _finish_status_board(
        self, card: SwarmCard, state: str, *, summary: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Finish the delegation's board alongside its card, if it has one."""
        board = self._status_board(card)
        if board is not None and not board.terminal:
            board.finish(state, summary=summary, failure_reason=failure_reason)
            self.status_publisher.note_update(board)

    # ── polling ──────────────────────────────────────────────────────────────

    def poll_once(
        self, *, list_subagents: Optional[Callable[[], list]] = None,
        list_delegations: Optional[Callable[[], list]] = None,
    ) -> list[SwarmCard]:
        """One snapshot over the live registries. Returns cards created or
        changed. Never raises: a broken sampler must not kill the watcher."""
        now = self._clock()
        updated: list[SwarmCard] = []
        if list_delegations is None:
            try:
                from tools.async_delegation import list_async_delegations as list_delegations
            except Exception:
                list_delegations = list
        if list_subagents is None:
            try:
                from tools.delegate_tool_registry import list_active_subagents as list_subagents
            except Exception:
                list_subagents = list
        try:
            delegations = list(list_delegations() or [])
        except Exception:
            delegations = []
        try:
            subagents = list(list_subagents() or [])
        except Exception:
            subagents = []
        seen: set[tuple[str, str]] = set()
        for record in delegations:
            if not isinstance(record, dict):
                continue
            delegation_id = str(record.get("delegation_id") or "")
            if not delegation_id:
                continue
            route = SwarmRoute.from_session_key(str(record.get("session_key") or ""))
            if route is None:
                continue
            key = ("delegate", delegation_id)
            preexisting = key in self.cards
            goals = record.get("goals") or []
            if not goals and record.get("goal"):
                goals = [record.get("goal")]
            children = [
                SwarmChild(label=_clean_label(goal)) for goal in goals
            ] or [SwarmChild(label="Worker 1")]
            card = self.observe(
                kind="delegate", unit_id=delegation_id, goal=goals[0] if goals else "",
                route=route, children=children,
            )
            card.last_seen_at = now
            seen.add(key)
            terminal = _TERMINAL_DELEGATION_STATUS.get(str(record.get("status") or "running"))
            if terminal is not None:
                if not card.terminal:
                    self._apply_batch_results(card, record.get("results") or [])
                    finished = self.finish_unit(
                        "delegate", delegation_id, state=terminal,
                        failure_reason=str(record.get("error") or "") or None,
                        summary=str(record.get("summary") or "") or None,
                    )
                    if finished is not None:
                        self._finish_status_board(
                            finished, terminal, summary=finished.summary,
                            failure_reason=finished.failure_reason)
                        updated.append(finished)
                continue
            status = str(record.get("status") or "running")
            if status in {"stalling", "stalled"}:
                if card.state != status:
                    card.state = status
                    self._mark_dirty(card)
            elif card.state in {"stalling", "stalled"}:
                card.state = "running"
                self._mark_dirty(card)
            if not preexisting:
                for child in card.children:
                    child.state = "running"
                self._mark_dirty(card)
            if card.dirty and card not in updated:
                updated.append(card)
        for record in subagents:
            if not isinstance(record, dict):
                continue
            subagent_id = str(record.get("subagent_id") or "")
            if not subagent_id:
                continue
            delegation_id = str(record.get("delegation_id") or "")
            route: Optional[SwarmRoute] = None
            owner_session_id = str(record.get("owner_agent_session_id") or "")
            if owner_session_id and self._resolve_route_for_session_id is not None:
                try:
                    route = self._resolve_route_for_session_id(owner_session_id)
                except Exception:
                    route = None
            card: Optional[SwarmCard] = None
            if delegation_id:
                card = self.cards.get(("delegate", delegation_id))
                if card is not None:
                    route = route or card.route
                    seen.add(("delegate", delegation_id))
            if route is None:
                continue
            if card is None:
                card = self.observe(
                    kind="lifecycle", unit_id=subagent_id,
                    goal=str(record.get("goal") or ""), route=route,
                    children=[SwarmChild(
                        label=_clean_label(record.get("goal")), state="running", ref=subagent_id,
                    )],
                )
                try:
                    card.started_at = float(record.get("started_at") or now)
                except (TypeError, ValueError):
                    card.started_at = now
                seen.add(("lifecycle", subagent_id))
                if card not in updated:
                    updated.append(card)
            else:
                card.last_seen_at = now
                for child in card.children:
                    if child.ref == subagent_id:
                        child.state = "running"
                        break
                else:
                    for child in card.children:
                        if child.ref is None and child.label == _clean_label(record.get("goal")):
                            child.ref = subagent_id
                            child.state = "running"
                            break
        for key, card in list(self.cards.items()):
            if card.terminal or card.kind == "kanban" or key in seen:
                continue
            if now - card.last_seen_at > VANISH_GRACE_SECONDS:
                finished = self.finish_unit(card.kind, card.unit_id, state="done")
                if finished is not None:
                    self._finish_status_board(finished, "done")
                    updated.append(finished)
        return updated

    async def poll_tick(self) -> None:
        """One poll + delivery pass for the gateway watcher. Never raises."""
        try:
            self.poll_once()
        except Exception:
            logger.debug("Swarm observer poll failed", exc_info=True)
        await self.refresh_dirty()

    # ── rendering ────────────────────────────────────────────────────────────

    def render_card(self, card: SwarmCard) -> str:
        """Rich HTML card. No emojis, no internal identifiers, goal escaped."""
        kind_title = _KIND_TITLES.get(card.kind, "Agent")
        state_title = _STATE_TITLES.get(card.state, card.state)
        lines = [f"<h1>{kind_title} {state_title}</h1>", "<p><b>Task</b></p>",
                 f"<pre>{html.escape(card.goal)}</pre>"]
        done = sum(1 for child in card.children if child.state == "done")
        total = len(card.children)
        if total:
            noun = "References" if card.kind == "moa" else "Workers"
            lines.append(f"<p><b>{noun}: {done} of {total} done</b></p>")
            for child in card.children:
                label = html.escape(child.label)
                if child.state == "failed" and child.reason:
                    lines.append(f"<p>{label}: failed — {html.escape(child.reason)}</p>")
                else:
                    lines.append(f"<p>{label}: {child.state}</p>")
        now = self._clock()
        elapsed = _format_duration(now - card.started_at)
        started = time.strftime("%H:%M:%S", time.localtime(card.started_at))
        if card.state in {"running", "stalling", "stalled"}:
            lines.append(f"<p>Started {started} · running {elapsed}</p>")
            if card.state in {"stalling", "stalled"}:
                lines.append("<p>No progress for a while — still running.</p>")
        elif card.state == "done":
            lines.append(f"<p>Finished after {elapsed}</p>")
            if card.summary:
                lines.append(f"<p>Result: {html.escape(_clean_text(card.summary, 200))}</p>")
        elif card.state == "failed":
            lines.append(f"<p>Finished after {elapsed}</p>")
            if card.failure_reason:
                lines.append(f"<p>Failed: {html.escape(_clean_text(card.failure_reason, 200))}</p>")
        elif card.state == "interrupted":
            lines.append(f"<p>Stopped after {elapsed} — before finishing.</p>")
        if not card.terminal:
            button_row = self._button_row(card)
            if button_row:
                lines.append(button_row)
        return "\n".join(lines)

    def _button_row(self, card: SwarmCard) -> str:
        """In-body Cancel (callback) + Copy task (copy) pills; empty when the
        Telegram rich-button surface is unavailable."""
        try:
            from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row
        except Exception:
            return ""
        buttons = [
            rich_button("Cancel", kind="callback",
                        value=self.callback_data(card, "cancel"), style="danger"),
        ]
        if card.goal and card.goal != "Untitled task":
            buttons.append(rich_button("Copy task", kind="copy", value=card.goal))
        return rich_button_row(buttons)

    # ── callback tokens + cancel ─────────────────────────────────────────────

    def callback_data(self, card: SwarmCard, action: str) -> str:
        """``sw:<12-hex>:<action>`` — 24 bytes, inside the 64-byte cap."""
        return f"sw:{card.token}:{action}"

    def _prune_tokens(self) -> None:
        now = self._clock()
        for token, card in list(self._tokens.items()):
            if now - card.token_created_at > TOKEN_TTL_SECONDS:
                self._tokens.pop(token, None)

    def resolve_callback(
        self, data: Any, *, user_id: Any, chat_id: Any,
    ) -> Optional[tuple[str, SwarmCard]]:
        """Resolve callback data to ``(action, card)``; None when unknown,
        expired, or from the wrong chat/user. A card without a recorded user
        is chat-scoped only."""
        if not isinstance(data, str):
            return None
        match = _TOKEN_RE.fullmatch(data.strip())
        if not match:
            return None
        token, action = match.groups()
        if action != "cancel":
            return None
        self._prune_tokens()
        card = self._tokens.get(token)
        if card is None:
            return None
        if str(chat_id or "") != str(card.route.chat_id or ""):
            return None
        owner = card.route.user_id
        if owner and str(user_id or "") != str(owner):
            return None
        return (action, card)

    def request_cancel(self, card: SwarmCard) -> tuple[bool, str]:
        """Stop a running unit. Kanban has no user-cancel API and in-flight
        MoA references cannot be killed, so those report honestly."""
        if card.terminal:
            return (False, "This run already finished.")
        if card.kind == "kanban":
            return (False, "Kanban workers cannot be stopped from chat yet.")
        if card.kind == "moa":
            return (False, "Reference calls already running cannot be stopped.")
        stopped = 0
        try:
            from tools.delegate_tool_registry import interrupt_subagent
        except Exception:
            interrupt_subagent = None
        if interrupt_subagent is not None:
            for child in card.children:
                if child.state in {"pending", "running"} and child.ref:
                    try:
                        if interrupt_subagent(child.ref):
                            stopped += 1
                    except Exception:
                        logger.debug("Swarm observer cancel of a child failed", exc_info=True)
        try:
            from tools.async_delegation import interrupt_for_session
        except Exception:
            interrupt_for_session = None
        if interrupt_for_session is not None and card.route.session_key:
            try:
                stopped += int(interrupt_for_session(
                    session_key=card.route.session_key, reason="swarm_observer_cancel") or 0)
            except Exception:
                logger.debug("Swarm observer cancel of a delegation failed", exc_info=True)
        if stopped:
            return (True, "Stop requested — the run stops at its next step.")
        return (False, "Nothing running to stop — it may have just finished.")

    # ── delivery (scheduler) ─────────────────────────────────────────────────

    def card_key(self, card: SwarmCard) -> str:
        return (
            f"{card.route.chat_id}:{card.route.session_key or 'route'}"
            f":swarm:{card.kind}:{card.unit_id}"
        )

    @staticmethod
    def _route_metadata(route: SwarmRoute) -> dict:
        """Card metadata: progress card, strict-topic only when threaded.
        No ``rich_format`` key — ``auto`` lets the in-body button row route
        the card to HTML."""
        metadata: dict[str, Any] = {"telegram_progress_card": True}
        if route.thread_id:
            metadata["thread_id"] = str(route.thread_id)
            metadata["telegram_strict_thread"] = True
            metadata["telegram_send_in_topic"] = True
        return metadata

    @staticmethod
    def _anchor(route: SwarmRoute) -> Optional[str]:
        """Threaded chats anchor to the topic root (the only verifiable anchor
        for later edits); unthreaded chats carry no anchor."""
        return str(route.thread_id) if route.thread_id else None

    def _adapter_for(self, card: SwarmCard) -> Any:
        if self._resolve_adapter is None:
            return None
        return self._adapter_for_route(card.route)

    def _adapter_for_route(self, route: SwarmRoute) -> Any:
        if self._resolve_adapter is None:
            return None
        try:
            return self._resolve_adapter(route)
        except Exception:
            logger.debug("Swarm observer adapter resolution failed", exc_info=True)
            return None

    async def _scheduler_begin(
        self, key: str, text: str, route: SwarmRoute, *,
        on_started: Optional[Callable[[str], Any]] = None,
    ) -> Optional[str]:
        """Register a card with the per-chat scheduler; the message id when
        the first send succeeds (None on FloodWait / no adapter)."""
        adapter = self._adapter_for_route(route)
        if adapter is None:
            return None
        try:
            from gateway.telegram_progress_scheduler import scheduler_for

            scheduler = scheduler_for(adapter, route.chat_id)
        except RuntimeError:
            return None
        metadata = self._route_metadata(route)
        anchor = self._anchor(route)
        chat_id = route.chat_id

        async def _send_start(content: str) -> Any:
            try:
                return await adapter.send(chat_id, content, reply_to=anchor, metadata=metadata)
            except Exception as exc:
                return SimpleNamespace(success=False, error=f"{type(exc).__name__}: {exc}")

        async def _edit(message_id: str, content: str) -> Any:
            try:
                return await adapter.edit_message(chat_id, message_id, content, metadata=metadata)
            except Exception as exc:
                return SimpleNamespace(success=False, error=f"{type(exc).__name__}: {exc}")

        try:
            message_id = await scheduler.begin(
                key, CARD_KIND, text,
                send_start=_send_start, edit=_edit, on_started=on_started,
            )
        except Exception:
            logger.debug("Swarm observer card begin failed", exc_info=True)
            return None
        return str(message_id) if message_id else None

    async def _scheduler_update(
        self, key: str, text: str, route: SwarmRoute, *, terminal: bool = False,
    ) -> bool:
        """Hand the latest text to the per-chat scheduler (it coalesces and
        spaces the actual edit). False when nothing could be delivered."""
        adapter = self._adapter_for_route(route)
        if adapter is None:
            return False
        try:
            from gateway.telegram_progress_scheduler import scheduler_for

            scheduler = scheduler_for(adapter, route.chat_id)
        except RuntimeError:
            return False
        scheduler.update(key, text, terminal=terminal)
        return True

    async def ensure_card(self, card: SwarmCard) -> Optional[str]:
        """Register the card with the per-chat scheduler and return its message
        id when the first send succeeds (None on FloodWait / no adapter). A
        boarded delegation delivers its status board instead of the card."""
        board = self._status_board(card)
        if board is not None:
            return await self._ensure_board(card, board)
        if card.card_message_id:
            return card.card_message_id

        async def _on_started(message_id: str) -> None:
            card.card_message_id = str(message_id)

        message_id = await self._scheduler_begin(
            self.card_key(card), self.render_card(card), card.route,
            on_started=_on_started,
        )
        if message_id:
            card.card_message_id = str(message_id)
        return card.card_message_id

    async def refresh_card(self, card: SwarmCard, *, terminal: Optional[bool] = None) -> bool:
        """Push the latest rendered card through the scheduler. Returns False
        when nothing could be delivered (no adapter, no message yet). A
        boarded delegation refreshes its status board instead."""
        board = self._status_board(card)
        if board is not None:
            return await self._refresh_board(card, board)
        if card.card_message_id is None:
            return await self.ensure_card(card) is not None
        return await self._scheduler_update(
            self.card_key(card), self.render_card(card), card.route,
            terminal=bool(card.terminal if terminal is None else terminal),
        )

    async def refresh_dirty(self) -> None:
        """Deliver every card whose state changed since the last delivery.
        Boarded delegations deliver their status board instead of the card."""
        for card in list(self.cards.values()):
            if not card.dirty or self._status_board(card) is not None:
                continue
            try:
                delivered = await self.refresh_card(card)
            except Exception:
                logger.debug("Swarm observer card refresh failed", exc_info=True)
                delivered = False
            if delivered:
                card.dirty = False
        await self._refresh_due_boards()

    # ── status-board delivery ────────────────────────────────────────────────

    def status_board_key(self, board: SwarmStatusBoard) -> str:
        """Scheduler key for one delegation's status message: one message id
        per delegation, reused across every update."""
        return (
            f"{board.route.chat_id}:{board.route.session_key or 'route'}"
            f":swarm:status:{board.delegation_id}"
        )

    def _render_board(self, card: SwarmCard, board: SwarmStatusBoard) -> str:
        button_row = None if board.terminal else self._button_row(card)
        return board.render(self._clock(), button_row=button_row)

    async def _ensure_board(
        self, card: SwarmCard, board: SwarmStatusBoard,
    ) -> Optional[str]:
        """First send of the status message; the id is reused by every edit."""
        if board.message_id:
            return board.message_id

        async def _on_started(message_id: str) -> None:
            board.message_id = str(message_id)
            self.status_publisher.mark_flushed(board, message_id=str(message_id))

        message_id = await self._scheduler_begin(
            self.status_board_key(board), self._render_board(card, board),
            card.route, on_started=_on_started,
        )
        if message_id:
            board.message_id = str(message_id)
            self.status_publisher.mark_flushed(board, message_id=str(message_id))
        return board.message_id

    async def _refresh_board(self, card: SwarmCard, board: SwarmStatusBoard) -> bool:
        """Edit the status message in place, unless the flood gate says the
        update is still inside its coalescing window (it stays pending)."""
        if board.message_id is None:
            return await self._ensure_board(card, board) is not None
        if not self.status_publisher.is_due(board):
            return True
        delivered = await self._scheduler_update(
            self.status_board_key(board), self._render_board(card, board),
            card.route, terminal=board.terminal,
        )
        if delivered:
            self.status_publisher.mark_flushed(board)
        return delivered

    async def _refresh_due_boards(self) -> None:
        """Flush every board whose coalescing window has elapsed."""
        for board in self.status_publisher.due_boards():
            card = self.cards.get(("delegate", board.delegation_id))
            if card is None:
                continue
            try:
                delivered = await self._refresh_board(card, board)
            except Exception:
                logger.debug("Swarm observer status-board refresh failed", exc_info=True)
                delivered = False
            if delivered:
                card.dirty = False


# ── Kanban hook subscription ─────────────────────────────────────────────────

def _kanban_callback(observer: SwarmObserver, hook_name: str) -> Callable[..., Any]:
    def _callback(**kwargs: Any) -> Any:
        return observer.on_kanban_hook(hook_name, **kwargs)

    _callback._swarm_observer = observer  # type: ignore[attr-defined]
    return _callback


def register_kanban_hooks(
    observer: SwarmObserver, manager: Any = None,
) -> list[str]:
    """Subscribe the observer to the Kanban lifecycle hooks on the plugin
    manager's hook table (the same table ``invoke_hook`` reads). Idempotent
    across reloads: a callback already present for this observer is not added
    twice. Returns the hook names newly subscribed."""
    if manager is None:
        try:
            from hermes_cli.plugins import get_plugin_manager

            manager = get_plugin_manager()
        except Exception:
            logger.debug(
                "Swarm observer: plugin manager unavailable; Kanban hooks not registered",
                exc_info=True,
            )
            return []
    hooks = getattr(manager, "_hooks", None)
    if not isinstance(hooks, dict):
        return []
    registered: list[str] = []
    for name in _KANBAN_HOOK_NAMES:
        callbacks = hooks.setdefault(name, [])
        if any(getattr(cb, "_swarm_observer", None) is observer for cb in callbacks):
            continue
        callbacks.append(_kanban_callback(observer, name))
        registered.append(name)
    return registered


# ── gateway wiring ───────────────────────────────────────────────────────────

#: The observer of the running gateway process (one process, one runner).
#: The adapter-side ``sw:`` callback handler (Builder B's swarm_ui) reaches the
#: observer through here instead of inventing a second registry.
_ACTIVE_OBSERVER: Optional[SwarmObserver] = None


def get_active_observer() -> Optional[SwarmObserver]:
    """The observer registered by :func:`observer_for_runner`, if any."""
    return _ACTIVE_OBSERVER

def _gateway_resolve_adapter(runner: Any, route: SwarmRoute) -> Any:
    """The adapter that answers a route: rebuild a source and use the runner's
    delivery resolver (never ``runner.adapters[platform]``)."""
    try:
        from gateway.session import Platform, SessionSource
    except Exception:
        return None
    try:
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id=route.chat_id, chat_type=route.chat_type,
            thread_id=route.thread_id, user_id=route.user_id, profile=route.profile,
        )
        return runner._delivery_adapter_for(source)
    except Exception:
        logger.debug("Swarm observer delivery-adapter resolution failed", exc_info=True)
        return None


def _gateway_resolve_route_for_session_id(runner: Any, session_id: str) -> Optional[SwarmRoute]:
    """Route for a parent agent session id via the session store's origin
    (the delivery routing recorded when the session started)."""
    try:
        store = getattr(runner, "_sessions", None)
        lookup = getattr(store, "lookup_by_session_id", None)
        if not callable(lookup):
            return None
        entry = lookup(session_id)
        origin = getattr(entry, "origin", None) if entry is not None else None
        if origin is None or getattr(origin, "platform", None) is None:
            return None
        if str(getattr(origin.platform, "value", origin.platform)) != "telegram":
            return None
        return SwarmRoute(
            platform="telegram", chat_id=str(getattr(origin, "chat_id", "") or ""),
            chat_type=str(getattr(origin, "chat_type", "dm") or "dm"),
            thread_id=str(getattr(origin, "thread_id", "") or "") or None,
            user_id=str(getattr(origin, "user_id", "") or "") or None,
            session_key=str(getattr(entry, "session_key", "") or ""),
            profile=getattr(origin, "profile", None),
        )
    except Exception:
        logger.debug("Swarm observer session-route resolution failed", exc_info=True)
        return None


def observer_for_runner(runner: Any) -> SwarmObserver:
    """Get-or-create the runner's observer (one per gateway process) and make
    sure its Kanban hooks are subscribed."""
    global _ACTIVE_OBSERVER
    observer = getattr(runner, "_swarm_observer", None)
    if observer is None:
        observer = SwarmObserver(
            resolve_adapter=lambda route: _gateway_resolve_adapter(runner, route),
            resolve_route_for_session_id=(
                lambda session_id: _gateway_resolve_route_for_session_id(runner, session_id)
            ),
        )
        runner._swarm_observer = observer
    _ACTIVE_OBSERVER = observer
    register_kanban_hooks(observer)
    return observer
