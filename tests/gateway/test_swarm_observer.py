"""Behaviour contracts for the Telegram swarm observer (gateway/swarm_observer.py).

The observer notices agent/swarm spawns as they happen and surfaces one status
card per agent/swarm in the invoking Telegram chat/thread, driven by the
existing TelegramProgressScheduler (never a message per tick).

No live Telegram is involved: delivery is exercised against a fake adapter and
the real scheduler; routing is exercised against real session-key parsing.
"""

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.swarm_observer import (
    POLL_INTERVAL_SECONDS,
    TOKEN_TTL_SECONDS,
    SwarmChild,
    SwarmObserver,
    SwarmRoute,
    register_kanban_hooks,
)
from gateway.turn_context import TurnContext

TELEGRAM_DM_KEY = "agent:main:telegram:dm:123:456"
TELEGRAM_GROUP_KEY = "agent:main:telegram:group:-1001"
OTHER_PLATFORM_KEY = "agent:main:slack:dm:C123"


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


# ── routing ──────────────────────────────────────────────────────────────────

def test_route_from_session_key_parses_telegram_dm_thread():
    route = SwarmRoute.from_session_key(TELEGRAM_DM_KEY)
    assert route is not None
    assert route.platform == "telegram"
    assert route.chat_id == "123"
    assert route.thread_id == "456"
    assert route.chat_type == "dm"
    assert route.session_key == TELEGRAM_DM_KEY


def test_route_from_session_key_group_has_no_thread():
    route = SwarmRoute.from_session_key(TELEGRAM_GROUP_KEY)
    assert route is not None
    assert route.chat_id == "-1001"
    assert route.thread_id is None


def test_route_from_session_key_rejects_other_platforms_and_junk():
    assert SwarmRoute.from_session_key(OTHER_PLATFORM_KEY) is None
    assert SwarmRoute.from_session_key("") is None
    assert SwarmRoute.from_session_key("not-a-key") is None
    assert SwarmRoute.from_session_key(None) is None


# ── progress events (delegate_task relay) ─────────────────────────────────────

def test_spawn_requested_creates_one_running_card_per_delegation():
    observer = SwarmObserver()
    card = _spawn(observer)
    assert card is not None
    assert card.state == "running"
    assert card.kind == "delegate"
    assert len(card.children) == 2
    assert card.children[0].label == "Research the topic"
    assert card.children[0].state == "pending"
    # The same delegation reuses its card; a second delegation gets its own.
    again = _spawn(observer)
    assert again is card
    other = _spawn(observer, delegation_id="del-2")
    assert other is not card
    assert len(observer.cards) == 2


def test_start_marks_child_running_and_complete_marks_it_done():
    observer = SwarmObserver()
    route = _dm_route()
    card = _spawn(observer, route=route)
    observer.on_progress_event(
        "subagent.start", route=route, delegation_id="del-1",
        task_index=0, task_count=2, goal="Research the topic",
    )
    assert card.children[0].state == "running"
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=2, status="succeeded", summary="Found three sources.",
    )
    assert card.children[0].state == "done"
    assert card.state == "running"  # sibling still pending
    assert "1 of 2 done" in observer.render_card(card)


def test_last_child_completing_finishes_the_card():
    observer = SwarmObserver()
    route = _dm_route()
    card = _spawn(observer, route=route, task_count=1)
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=1, status="succeeded", summary="Done.",
    )
    assert card.terminal is True
    assert card.state == "done"
    text = observer.render_card(card)
    assert "finished" in text.lower()
    assert "Cancel" not in text


def test_failed_child_marks_card_failed_and_keeps_the_reason():
    observer = SwarmObserver()
    route = _dm_route()
    card = _spawn(observer, route=route, task_count=1)
    observer.on_progress_event(
        "subagent.complete", route=route, delegation_id="del-1",
        task_index=0, task_count=1, status="failed",
        summary="The child ran out of time.", failure_reason="timeout",
    )
    assert card.children[0].state == "failed"
    assert card.state == "failed"
    assert card.terminal is True
    text = observer.render_card(card)
    assert "failed" in text.lower()
    assert "ran out of time" in text


def test_progress_ticks_do_not_create_cards_or_change_state():
    observer = SwarmObserver()
    route = _dm_route()
    assert observer.on_progress_event("tool.started", "web_search", route=route) is None
    assert observer.on_progress_event(
        "subagent.tool", "read_file", route=route, delegation_id="del-9",
        task_index=0, task_count=1, goal="Read the notes",
    ) is not None  # tool ticks still refresh activity on a known card
    assert len(observer.cards) == 1
    assert observer.cards[("delegate", "del-9")].state == "running"


def test_progress_event_without_route_is_ignored():
    observer = SwarmObserver()
    assert observer.on_progress_event(
        "subagent.spawn_requested", delegation_id="del-1", task_index=0, task_count=1,
    ) is None
    assert observer.cards == {}


# ── MoA relay events ─────────────────────────────────────────────────────────

def test_moa_progress_builds_one_reference_card_per_run():
    observer = SwarmObserver()
    route = _dm_route()
    card = observer.on_progress_event(
        "moa.progress", route=route, run_key="moa:turn-1",
        moa_refs_done=1, moa_refs_total=3, label="reference 1",
    )
    assert card is not None
    assert card.kind == "moa"
    assert len(card.children) == 3
    assert sum(1 for child in card.children if child.state == "done") == 1
    card2 = observer.on_progress_event(
        "moa.aggregating", route=route, run_key="moa:turn-1", moa_ref_count=3,
    )
    assert card2 is card
    assert all(child.state == "done" for child in card.children)
    assert card.state == "running"  # aggregation still in flight; turn end finishes it


def test_moa_without_run_key_keys_off_the_session():
    observer = SwarmObserver()
    card = observer.on_progress_event(
        "moa.progress", route=_dm_route(), moa_refs_done=0, moa_refs_total=2,
    )
    assert card is not None
    assert card.unit_id == f"moa:{TELEGRAM_DM_KEY}"


# ── async-delegation completion queue ─────────────────────────────────────────

def _completion_evt(delegation_id="del-1", status="completed", **extra):
    evt = {
        "type": "async_delegation", "delegation_id": delegation_id,
        "session_key": TELEGRAM_DM_KEY, "status": status,
        "goal": "Research the topic", "summary": "Three sources found.",
        "dispatched_at": 1000.0, "completed_at": 1060.0,
    }
    evt.update(extra)
    return evt


def test_async_completion_finalizes_a_known_card():
    observer = SwarmObserver()
    _spawn(observer, task_count=1)
    affected = observer.on_async_delegation_event(_completion_evt())
    assert len(affected) == 1
    card = affected[0]
    assert card.terminal is True
    assert card.state == "done"
    assert card.summary == "Three sources found."
    assert "Three sources found." in observer.render_card(card)


def test_async_completion_for_unknown_delegation_creates_no_card():
    observer = SwarmObserver()
    assert observer.on_async_delegation_event(_completion_evt()) == []
    assert observer.cards == {}


def test_async_completion_redelivery_is_idempotent():
    observer = SwarmObserver()
    _spawn(observer, task_count=1)
    assert len(observer.on_async_delegation_event(_completion_evt())) == 1
    assert observer.on_async_delegation_event(_completion_evt()) == []


def test_async_failure_event_marks_card_failed_with_error():
    observer = SwarmObserver()
    _spawn(observer, task_count=1)
    affected = observer.on_async_delegation_event(
        _completion_evt(status="failed", error="Provider refused the request."),
    )
    assert affected[0].state == "failed"
    assert "Provider refused the request." in observer.render_card(affected[0])


def test_async_task_failure_notice_fails_one_child_and_keeps_running():
    observer = SwarmObserver()
    _spawn(observer, task_count=3)
    evt = _completion_evt(
        status="running", task_failure_notice=True, is_batch=True, n_tasks=3,
        results=[{"task_index": 1, "status": "failed", "goal": "Draft the summary",
                  "error": "Timed out."}],
    )
    affected = observer.on_async_delegation_event(evt)
    assert len(affected) == 1
    card = affected[0]
    assert card.state == "running"
    assert card.terminal is False
    assert card.children[1].state == "failed"
    assert card.children[0].state == "pending"
    assert "Timed out." in observer.render_card(card)


def test_async_event_with_wrong_type_or_no_route_is_ignored():
    observer = SwarmObserver()
    assert observer.on_async_delegation_event({"type": "watch_match"}) == []
    assert observer.on_async_delegation_event(
        _completion_evt(session_key="")) == []
    assert observer.on_async_delegation_event(
        _completion_evt(session_key=OTHER_PLATFORM_KEY)) == []


# ── polling (lifecycle + async delegations) ───────────────────────────────────

def test_poll_discovers_running_async_delegation_by_session_key():
    observer = SwarmObserver()
    updated = observer.poll_once(list_subagents=list, list_delegations=lambda: [
        {"delegation_id": "del-async", "session_key": TELEGRAM_DM_KEY,
         "status": "running", "goal": "Background research",
         "goals": ["Background research", "Write it up"]},
    ])
    assert len(updated) == 1
    card = updated[0]
    assert card.kind == "delegate"
    assert card.unit_id == "del-async"
    assert len(card.children) == 2
    assert card.state == "running"


def test_poll_skips_cli_and_other_platform_delegations():
    observer = SwarmObserver()
    updated = observer.poll_once(list_subagents=list, list_delegations=lambda: [
        {"delegation_id": "del-cli", "session_key": "", "status": "running", "goal": "CLI job"},
        {"delegation_id": "del-slack", "session_key": OTHER_PLATFORM_KEY,
         "status": "running", "goal": "Slack job"},
    ])
    assert updated == []
    assert observer.cards == {}


def test_poll_marks_vanished_unit_finished_after_the_grace_period():
    now = [1_000_000.0]
    observer = SwarmObserver(clock=lambda: now[0])
    card = _spawn(observer, task_count=1)
    observer.poll_once(list_subagents=list, list_delegations=list)
    assert card.terminal is False  # inside the grace period: still running
    now[0] += POLL_INTERVAL_SECONDS * 10
    updated = observer.poll_once(list_subagents=list, list_delegations=list)
    assert card in updated
    assert card.terminal is True
    assert card.state == "done"
    assert "finished" in observer.render_card(card).lower()


def test_poll_routes_live_subagent_through_the_session_resolver():
    observer = SwarmObserver(
        resolve_route_for_session_id=lambda sid: _dm_route() if sid == "parent-1" else None,
    )
    updated = observer.poll_once(
        list_subagents=lambda: [
            {"subagent_id": "sub-1", "goal": "Lifecycle child", "status": "running",
             "owner_agent_session_id": "parent-1", "tool_count": 3},
            {"subagent_id": "sub-2", "goal": "Orphan child", "status": "running",
             "owner_agent_session_id": "parent-unknown", "tool_count": 0},
        ],
        list_delegations=list,
    )
    assert len(updated) == 1
    assert updated[0].kind == "lifecycle"
    assert updated[0].unit_id == "sub-1"
    assert updated[0].children[0].label == "Lifecycle child"


def test_poll_maps_stalled_async_status_onto_the_card():
    observer = SwarmObserver()
    observer.poll_once(list_subagents=list, list_delegations=lambda: [
        {"delegation_id": "del-stall", "session_key": TELEGRAM_DM_KEY,
         "status": "stalled", "goal": "Slow job", "seconds_since_progress": 300.0},
    ])
    card = observer.cards[("delegate", "del-stall")]
    assert card.state == "stalled"
    assert "stalled" in observer.render_card(card).lower()


# ── Kanban hooks ─────────────────────────────────────────────────────────────

def _bind(observer, route=None):
    return observer.bind_kanban_swarm(
        "root-1", route or _dm_route(), goal="Build the report",
        worker_ids=["task-w1", "task-w2"], verifier_id="task-v", synthesizer_id="task-s",
    )


def test_kanban_claimed_and_completed_update_the_bound_card():
    observer = SwarmObserver()
    card = _bind(observer)
    assert [child.state for child in card.children] == ["pending"] * 4
    observer.on_kanban_hook("kanban_task_claimed", task_id="task-w1", board="main",
                            assignee="worker", run_id=1, profile_name="default")
    assert card.children[0].state == "running"
    observer.on_kanban_hook("kanban_task_completed", task_id="task-w1", board="main",
                            assignee="worker", run_id=1, profile_name="default",
                            summary="Research done.")
    assert card.children[0].state == "done"
    assert card.state == "running"


def test_kanban_blocked_marks_worker_failed_with_reason():
    observer = SwarmObserver()
    card = _bind(observer)
    observer.on_kanban_hook("kanban_task_blocked", task_id="task-w2", board="main",
                            assignee="worker", run_id=2, profile_name="default",
                            reason="Waiting on the source file.")
    assert card.children[1].state == "failed"
    assert "Waiting on the source file." in observer.render_card(card)


def test_kanban_worker_spawned_never_leaks_pid_or_paths_into_chat():
    observer = SwarmObserver()
    card = _bind(observer)
    observer.on_kanban_hook("on_kanban_worker_spawned", task_id="task-w1", board="main",
                            assignee="worker", run_id=1, profile_name="default",
                            worker_pid=4242, workspace_path="/home/user/secret-work")
    assert card.children[0].state == "running"
    text = observer.render_card(card)
    assert "4242" not in text
    assert "secret-work" not in text


def test_kanban_all_children_done_finishes_the_swarm_card():
    observer = SwarmObserver()
    card = _bind(observer)
    for task_id in ("task-w1", "task-w2", "task-v", "task-s"):
        observer.on_kanban_hook("kanban_task_completed", task_id=task_id, board="main",
                                assignee="worker", run_id=1, profile_name="default")
    assert card.terminal is True
    assert card.state == "done"


def test_kanban_hook_without_a_binding_is_ignored():
    observer = SwarmObserver()
    assert observer.on_kanban_hook(
        "kanban_task_claimed", task_id="task-unknown", board="main",
        assignee="worker", run_id=1, profile_name="default") is None
    assert observer.cards == {}


def test_kanban_dispatch_tick_is_too_noisy_for_a_card():
    observer = SwarmObserver()
    card = _bind(observer)
    assert observer.on_kanban_hook(
        "on_kanban_dispatch_tick", board="main", profile_name="default",
        dry_run=False, outcome="idle", result=None) is None
    assert card.children[0].state == "pending"


def test_register_kanban_hooks_subscribes_once_and_drives_the_observer():
    class FakeManager:
        def __init__(self):
            self._hooks = {}

    manager = FakeManager()
    observer = SwarmObserver()
    names = register_kanban_hooks(observer, manager=manager)
    assert set(names) == {
        "kanban_task_claimed", "kanban_task_completed", "kanban_task_blocked",
        "on_kanban_worker_spawned", "on_kanban_dispatch_tick",
    }
    assert register_kanban_hooks(observer, manager=manager) == []
    _bind(observer)
    for callback in manager._hooks["kanban_task_claimed"]:
        callback(task_id="task-w1", board="main", assignee="w", run_id=1, profile_name="default")
    card = observer.cards[("kanban", "root-1")]
    assert card.children[0].state == "running"


# ── rendering: no emojis, no internal identifiers, buttons ──────────────────

def test_rendered_card_has_no_emoji_and_no_internal_identifiers():
    observer = SwarmObserver()
    card = _spawn(observer, task_count=2)
    text = observer.render_card(card)
    assert not any(ord(char) >= 0x1F300 for char in text)
    assert "del-1" not in text
    # The token is opaque callback data, never visible prose: it appears exactly
    # once, inside the Cancel button's data attribute (the deepresearch `dr:`
    # precedent), and nowhere else.
    assert text.count(card.token) == 1
    assert f'data="sw:{card.token}:cancel"' in text
    assert "Research the topic" in text
    assert "Cancel" in text
    assert "sw:" in text  # callback data rides in the in-body button
    assert len(observer.callback_data(card, "cancel").encode("utf-8")) <= 64


def test_rendered_card_escapes_user_text():
    observer = SwarmObserver()
    card = _spawn(observer, goal="Fix <b>bold</b> & </pre> injection")
    text = observer.render_card(card)
    assert "<b>bold</b>" not in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text


# ── callback tokens ──────────────────────────────────────────────────────────

def test_callback_token_roundtrip_and_ownership_checks():
    observer = SwarmObserver()
    card = _spawn(observer)
    data = observer.callback_data(card, "cancel")
    assert re.fullmatch(r"sw:[a-f0-9]{12}:cancel", data)
    action, found = observer.resolve_callback(data, user_id="7", chat_id="123")
    assert action == "cancel"
    assert found is card
    assert observer.resolve_callback(data, user_id="8", chat_id="123") is None
    assert observer.resolve_callback(data, user_id="7", chat_id="999") is None
    assert observer.resolve_callback("sw:000000000000:cancel", user_id="7", chat_id="123") is None
    assert observer.resolve_callback("dr:abcdef012345:cancel", user_id="7", chat_id="123") is None


def test_callback_token_expires():
    now = [1_000_000.0]
    observer = SwarmObserver(clock=lambda: now[0])
    card = _spawn(observer)
    data = observer.callback_data(card, "cancel")
    now[0] += TOKEN_TTL_SECONDS + 1
    assert observer.resolve_callback(data, user_id="7", chat_id="123") is None


# ── cancel ───────────────────────────────────────────────────────────────────

def test_cancel_delegate_interrupts_live_children(monkeypatch):
    stopped = []
    monkeypatch.setattr(
        "tools.delegate_tool_registry.interrupt_subagent",
        lambda subagent_id: stopped.append(subagent_id) or True,
    )
    monkeypatch.setattr(
        "tools.async_delegation.interrupt_for_session", lambda **kwargs: 0,
    )
    observer = SwarmObserver()
    route = _dm_route()
    card = _spawn(observer, route=route, task_count=1)
    observer.on_progress_event(
        "subagent.start", route=route, delegation_id="del-1",
        task_index=0, task_count=1, subagent_id="sub-1", goal="Research the topic",
    )
    ok, note = observer.request_cancel(card)
    assert ok is True
    assert stopped == ["sub-1"]
    assert "stop" in note.lower()


def test_cancel_kanban_and_moa_report_no_cancel_path():
    observer = SwarmObserver()
    kanban = _bind(observer)
    ok, note = observer.request_cancel(kanban)
    assert ok is False
    assert "cannot" in note.lower() or "no" in note.lower()
    moa = observer.on_progress_event(
        "moa.progress", route=_dm_route(), run_key="moa:turn-1",
        moa_refs_done=0, moa_refs_total=2,
    )
    ok, note = observer.request_cancel(moa)
    assert ok is False


def test_cancel_finished_card_is_a_noop():
    observer = SwarmObserver()
    card = _spawn(observer, task_count=1)
    observer.finish_unit("delegate", "del-1", state="done")
    ok, _note = observer.request_cancel(card)
    assert ok is False


# ── delivery through the scheduler ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_delivery_begin_anchors_to_the_topic_root():
    adapter = FakeAdapter()
    observer = SwarmObserver(resolve_adapter=lambda route: adapter)
    card = observer.observe(
        kind="delegate", unit_id="del-1", goal="Research the topic", route=_dm_route(),
        children=[SwarmChild(label="Research the topic", state="running")],
    )
    message_id = await observer.ensure_card(card)
    assert message_id == "77"
    assert len(adapter.sends) == 1
    chat_id, _content, reply_to, metadata = adapter.sends[0]
    assert chat_id == "123"
    assert reply_to == "456"  # threaded DM: anchor is the topic root
    assert metadata["telegram_progress_card"] is True
    assert metadata["telegram_strict_thread"] is True
    assert metadata["telegram_send_in_topic"] is True
    assert card.card_message_id == "77"


@pytest.mark.asyncio
async def test_delivery_update_edits_through_the_scheduler_not_a_new_message():
    from gateway.telegram_progress_scheduler import scheduler_for

    adapter = FakeAdapter()
    observer = SwarmObserver(resolve_adapter=lambda route: adapter)
    card = observer.observe(
        kind="delegate", unit_id="del-1", goal="Research the topic", route=_dm_route(),
        children=[SwarmChild(label="Research the topic", state="running")],
    )
    await observer.ensure_card(card)
    card.children[0].state = "done"
    card.dirty = True
    scheduler = scheduler_for(adapter, "123")
    scheduler._last_intermediate_edit = time.monotonic() - 100
    await observer.refresh_dirty()
    await asyncio.wait_for(adapter.edited.wait(), timeout=5)
    assert len(adapter.sends) == 1  # no second card
    assert len(adapter.edits) == 1
    assert adapter.edits[0][1] == "77"
    assert "done" in adapter.edits[0][2]


@pytest.mark.asyncio
async def test_delivery_without_an_adapter_keeps_the_card_pending():
    observer = SwarmObserver(resolve_adapter=lambda route: None)
    card = observer.observe(
        kind="delegate", unit_id="del-1", goal="Research the topic", route=_dm_route(),
    )
    assert await observer.ensure_card(card) is None
    assert card.card_message_id is None


@pytest.mark.asyncio
async def test_poll_tick_refreshes_dirty_cards():
    adapter = FakeAdapter()
    observer = SwarmObserver(resolve_adapter=lambda route: adapter)
    observer.poll_once(list_subagents=list, list_delegations=lambda: [
        {"delegation_id": "del-async", "session_key": TELEGRAM_DM_KEY,
         "status": "running", "goal": "Background research"},
    ])
    await observer.poll_tick()
    assert len(adapter.sends) == 1
    assert "Background research" in adapter.sends[0][1]


# ── gateway wiring ───────────────────────────────────────────────────────────

def test_swarm_observer_watcher_is_registered_for_gateway_startup():
    from gateway.run_startup import GatewayStartupMixin

    assert "_swarm_observer_watcher" in GatewayStartupMixin._POST_RECONNECT_WATCHERS


def test_observer_for_runner_registers_the_active_observer(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.swarm_observer import get_active_observer, observer_for_runner

    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _StubPluginManager(),
    )
    assert observer_for_runner(runner) is get_active_observer()
    assert observer_for_runner(runner) is get_active_observer()


class _StubPluginManager:
    def __init__(self):
        self._hooks = {}


def _bare_runner_with_observer(monkeypatch, adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running = True
    observer = SwarmObserver(resolve_adapter=lambda route: adapter)
    runner._swarm_observer = observer
    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_manager", lambda: _StubPluginManager(),
    )
    return runner, observer


@pytest.mark.asyncio
async def test_notify_swarm_observer_finalizes_card_for_delivered_group(monkeypatch):
    adapter = FakeAdapter()
    runner, observer = _bare_runner_with_observer(monkeypatch, adapter)
    card = _spawn(observer, task_count=1)
    runner._notify_swarm_observer([_completion_evt()])
    assert card.terminal is True
    assert card.state == "done"
    # The terminal card is delivered as a fresh send (no card existed yet).
    await asyncio.wait_for(adapter.sent.wait(), timeout=5)
    assert len(adapter.sends) == 1
    assert "Three sources found." in adapter.sends[0][1]


@pytest.mark.asyncio
async def test_swarm_observer_watcher_polls_and_delivers_dirty_cards(monkeypatch):
    adapter = FakeAdapter()
    runner, observer = _bare_runner_with_observer(monkeypatch, adapter)
    observer.observe(
        kind="delegate", unit_id="del-1", goal="Research the topic", route=_dm_route(),
        children=[SwarmChild(label="Research the topic", state="running")],
    )
    watcher = asyncio.create_task(runner._swarm_observer_watcher(interval=0.05))
    try:
        await asyncio.wait_for(adapter.sent.wait(), timeout=5)
        assert len(adapter.sends) == 1
    finally:
        runner._running = False
        await asyncio.wait_for(watcher, timeout=5)


# ── TurnRunner wiring (A1) ───────────────────────────────────────────────────

def test_progress_callback_feeds_subagent_events_to_observer(monkeypatch):
    """In-turn subagent events reach the swarm observer, even when the activity
    trail would return early. Without the progress_callback hook the observer
    learns about in-turn children only via the 15 s poll, and MoA not at all."""
    import gateway.swarm_observer as swarm_observer_mod

    observer = SwarmObserver()
    monkeypatch.setattr(swarm_observer_mod, "_ACTIVE_OBSERVER", None)
    gateway = SimpleNamespace()
    gateway._swarm_observer = observer
    context = TurnContext(
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="123", thread_id="77", user_id="7"),
        message="Do research",
        context_prompt="",
        event_message_id="99",
        session_key="agent:main:telegram:dm:123:7",
        run_generation=0,
        telegram_activity_trail_enabled=True,
    )
    runner = object.__new__(TurnRunner)
    runner._ctx = context
    runner._runner = gateway
    runner.progress_callback(
        "subagent.start", tool_name="delegate_task", preview="Research the topic",
        args={}, delegation_id="del-1", task_index=0, task_count=2, goal="Research the topic",
    )
    assert len(observer.cards) == 1
    card = observer.cards[("delegate", "del-1")]
    assert card.children[0].state == "running"
    assert card.children[0].label == "Research the topic"
