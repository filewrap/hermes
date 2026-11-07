"""Behaviour contracts for the persistent swarm status message
(gateway/swarm_status.py, wired into gateway/swarm_observer.py).

One message per delegation, edited in place: a compact live summary on top,
one real ``<details>`` section per subagent/turn below it, flood-gated edits.
No live Telegram is involved: delivery is exercised against a fake adapter
and the real scheduler; rendering is asserted on the message content.
"""

import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.swarm_observer import SwarmChild, SwarmObserver, SwarmRoute
from gateway.swarm_status import (
    COALESCE_WINDOW_SECONDS,
    MIN_EDIT_INTERVAL_SECONDS,
    SwarmStatusBoard,
    SwarmStatusPublisher,
)

TELEGRAM_DM_KEY = "agent:main:telegram:dm:123:456"


def _dm_route():
    return SwarmRoute(
        platform="telegram", chat_id="123", chat_type="dm", thread_id="456",
        user_id="7", session_key=TELEGRAM_DM_KEY,
    )


def _spawn(observer, route=None, delegation_id="del-1", task_count=2, goal="Research the topic"):
    return observer.on_progress_event(
        "subagent.spawn_requested", preview=goal, route=route or _dm_route(),
        delegation_id=delegation_id, task_index=0, task_count=task_count, goal=goal,
    )


class FakeAdapter:
    """Minimal adapter surface the observer uses (send / edit_message)."""

    def __init__(self):
        self.sends = []
        self.edits = []
        self.sent = asyncio.Event()
        self.edited = asyncio.Event()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append((str(chat_id), content, reply_to, dict(metadata or {})))
        self.sent.set()
        return SimpleNamespace(success=True, message_id="77")

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        self.edits.append((str(chat_id), str(message_id), content, dict(metadata or {})))
        self.edited.set()
        return SimpleNamespace(success=True, message_id=str(message_id))


# ── one board per delegation, collapsible sections per agent ──────────────────

def test_one_board_per_delegation_with_a_details_section_per_agent():
    observer = SwarmObserver()
    route = _dm_route()
    _spawn(observer, route=route, task_count=2)
    observer.on_progress_event(
        "subagent.spawn_requested", route=route, delegation_id="del-1",
        task_index=1, task_count=2, goal="Write it up",
    )
    observer.on_progress_event(
        "subagent.start", route=route, delegation_id="del-1",
        task_index=0, task_count=2, goal="Research the topic",
    )
    board = observer.board_for("del-1")
    assert board is not None
    assert observer.board_for("del-1") is board  # same delegation reuses its board
    assert observer.board_for("del-2") is None
    text = board.render(1_000_100.0)
    assert "1 of 2 done" not in text  # nothing done yet
    assert "0 of 2 done" in text
    assert "<details><summary>Research the topic" in text
    assert "<details><summary>Write it up" in text
    assert "Research the topic" in text and "Write it up" in text
    assert "del-1" not in text  # no internal identifiers in chat
    assert not any(ord(char) >= 0x1F300 for char in text)  # no emojis


def test_turn_progress_lands_inside_the_agents_own_section():
    observer = SwarmObserver()
    route = _dm_route()
    _spawn(observer, route=route, task_count=1)
    observer.on_progress_event(
        "subagent.tool", "web_search", preview="Searching the archive",
        route=route, delegation_id="del-1", task_index=0, task_count=1,
        goal="Research the topic",
    )
    board = observer.board_for("del-1")
    text = board.render(1_000_100.0)
    section = text.split("<details>")[1]
    assert "Searching the archive" in section
    assert "web_search" in section


def test_new_agent_spawned_by_a_worker_is_recorded_on_the_parent():
    board = SwarmStatusBoard(delegation_id="del-1", goal="Research", route=_dm_route())
    board.record_started("Research the topic")
    board.record_spawned("Research the topic", "Fetch the sources")
    text = board.render(1_000_100.0)
    assert "Fetch the sources" in text
    assert "Spawned: Fetch the sources" in text


def test_completion_closes_the_agents_section_and_updates_the_summary():
    observer = SwarmObserver()
    route = _dm_route()
    _spawn(observer, route=route, task_count=2)
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=2, status="succeeded", summary="Found three sources.",
    )
    board = observer.board_for("del-1")
    text = board.render(1_000_100.0)
    assert "1 of 2 done" in text
    assert "Found three sources." in text
    assert "Research the topic — done" in text


# ── flood gate: coalescing, not a sleep ───────────────────────────────────────

def test_rapid_updates_coalesce_to_a_single_pending_edit():
    now = [1_000_000.0]
    publisher = SwarmStatusPublisher(clock=lambda: now[0])
    board = publisher.board_for("del-1", "Research", _dm_route())
    for _ in range(5):  # five updates inside one window
        board.record_turn("Research the topic", "still going")
        publisher.note_update(board)
    assert publisher.due_boards() == [board]  # one board, one pending edit
    publisher.mark_flushed(board, message_id="77")
    for _ in range(5):
        board.record_turn("Research the topic", "still going")
        publisher.note_update(board)
    assert publisher.due_boards() == []  # inside the window: nothing due
    now[0] += MIN_EDIT_INTERVAL_SECONDS
    assert publisher.due_boards() == [board]  # window elapsed: one edit
    assert COALESCE_WINDOW_SECONDS == MIN_EDIT_INTERVAL_SECONDS


def test_terminal_board_bypasses_the_flood_gate():
    now = [1_000_000.0]
    publisher = SwarmStatusPublisher(clock=lambda: now[0])
    board = publisher.board_for("del-1", "Research", _dm_route())
    publisher.mark_flushed(board, message_id="77")
    board.finish("done", summary="All done.")
    publisher.note_update(board)
    assert publisher.due_boards() == [board]  # finished now, not after the window


# ── delivery: one message id, edited in place ─────────────────────────────────

@pytest.mark.asyncio
async def test_board_replaces_the_per_agent_card_one_message_edited_in_place():
    from gateway.telegram_progress_scheduler import scheduler_for

    now = [1_000_000.0]
    adapter = FakeAdapter()
    observer = SwarmObserver(
        resolve_adapter=lambda route: adapter,
        status_publisher=SwarmStatusPublisher(clock=lambda: now[0]),
    )
    route = _dm_route()
    _spawn(observer, route=route, task_count=2)
    await observer.poll_tick()
    await asyncio.wait_for(adapter.sent.wait(), timeout=5)
    assert len(adapter.sends) == 1  # one status message, no per-agent card
    assert "<details>" in adapter.sends[0][1]
    assert adapter.sends[0][2] == "456"  # anchored to the topic root
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=2, status="succeeded", summary="Found three sources.",
    )
    now[0] += MIN_EDIT_INTERVAL_SECONDS + 1
    scheduler = scheduler_for(adapter, "123")
    scheduler._last_intermediate_edit = time.monotonic() - 100
    await observer.refresh_dirty()
    await asyncio.wait_for(adapter.edited.wait(), timeout=5)
    assert len(adapter.sends) == 1  # still one message: edited, not re-sent
    assert len(adapter.edits) == 1
    assert adapter.edits[0][1] == "77"  # the same message id
    assert "1 of 2 done" in adapter.edits[0][2]
    assert "Found three sources." in adapter.edits[0][2]


@pytest.mark.asyncio
async def test_finished_board_drops_cancel_and_keeps_the_summary():
    adapter = FakeAdapter()
    observer = SwarmObserver(resolve_adapter=lambda route: adapter)
    route = _dm_route()
    _spawn(observer, route=route, task_count=1)
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=1, status="succeeded", summary="Three sources found.",
    )
    board = observer.board_for("del-1")
    assert board.terminal is True
    await observer.poll_tick()
    await asyncio.wait_for(adapter.sent.wait(), timeout=5)
    assert len(adapter.sends) == 1
    assert "Three sources found." in adapter.sends[0][1]
    assert "Cancel" not in adapter.sends[0][1]


def test_board_cancel_button_reuses_the_cards_token_and_ttl():
    observer = SwarmObserver()
    route = _dm_route()
    card = _spawn(observer, route=route, task_count=1)
    board = observer.board_for("del-1")
    text = board.render(1_000_100.0, button_row=observer._button_row(card))
    assert text.count(card.token) == 1
    assert f'data="sw:{card.token}:cancel"' in text
    action, found = observer.resolve_callback(
        observer.callback_data(card, "cancel"), user_id="7", chat_id="123")
    assert action == "cancel"
    assert found is card


# ── scope: only delegate units get a board ────────────────────────────────────

def test_moa_and_kanban_keep_their_cards_and_get_no_board():
    observer = SwarmObserver()
    route = _dm_route()
    observer.on_progress_event(
        "moa.progress", route=route, run_key="moa:turn-1",
        moa_refs_done=1, moa_refs_total=2,
    )
    observer.bind_kanban_swarm(
        "root-1", route, goal="Build the report", worker_ids=["task-w1"],
    )
    assert observer.board_for("moa:turn-1") is None
    assert observer.board_for("root-1") is None
    assert len(observer.cards) == 2


def test_board_render_escapes_user_text():
    board = SwarmStatusBoard(delegation_id="del-1", goal="Fix <b>bold</b>", route=_dm_route())
    board.record_turn("Research <script>", "note <b>bold</b> & stuff")
    text = board.render(1_000_100.0)
    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
