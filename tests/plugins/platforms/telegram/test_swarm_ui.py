"""Telegram swarm UI: cards, token callbacks, cancel/retry controls.

Behaviour contracts only: callback data fits the 64-byte limit, cards render
live state without emoji, cancel stops the one delegation it names (and
refuses when the session has several), and Kanban cancel/retry block and
unblock real worker cards.
"""

import asyncio
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from telethon import types

from plugins.platforms.telegram import swarm_ui
from plugins.platforms.telegram.swarm_ui import TelegramSwarmUI


@dataclass(frozen=True)
class Source:
    chat_id: str = "42"
    user_id: str = "12345"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: str = "telegram"


class CallbackEvent:
    def __init__(self, source, message_id):
        self.source = source
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text or "", kwargs))


class FakeSettings:
    def __init__(self, home):
        self.home = home

    def _home(self, source):
        return self.home


class FakeAdapter:
    def __init__(self, home):
        self._client = object()
        self.home = home
        self.requests = []
        self.messages = []

    def _settings_ui(self):
        return FakeSettings(self.home)

    def _peer_for(self, chat_id):
        return types.PeerUser(int(chat_id))

    def _sent_message_id(self, response):
        return str(response.id)

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=100 + len(self.requests))

    async def _source(self, event):
        return event.source

    def _authorized(self, source):
        return True

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return SimpleNamespace(success=True, message_id="501")


def _entry(**overrides):
    base = {
        "entry_id": "live:delegation:d1",
        "kind": "delegation",
        "source_id": "d1",
        "status": "running",
        "goal": "Summarize the release notes",
        "workers": [{"label": "worker 1", "status": "running", "detail": "read_file"}],
        "session_key": "agent:main:test:dm:1",
        "board": None,
        "durable_session_id": None,
        "result_summary": None,
        "created_at": "",
        "updated_at": "",
    }
    base.update(overrides)
    return base


def _assert_no_emoji(text):
    assert not any(0x1F000 <= ord(char) for char in text)
    assert "⚠" not in text and "🔀" not in text and "💭" not in text


def test_callback_data_fits_64_bytes():
    token = "a" * 12
    for action in ("view", "cancel", "retry", "result", "back", "refresh"):
        data = swarm_ui.callback_data(token, action)
        assert len(data.encode("utf-8")) <= 64
        assert swarm_ui.CALLBACK_RE.fullmatch(data)


def test_list_text_renders_entries_without_emoji():
    text = swarm_ui.list_text([_entry(), _entry(
        entry_id="live:lifecycle:s2", kind="lifecycle", source_id="s2",
        status="failed", goal="Review the patch",
        workers=[{"label": "worker", "status": "failed", "detail": "timeout"}],
    )])
    assert "Summarize the release notes" in text
    assert "Review the patch" in text
    assert "running" in text and "failed" in text
    _assert_no_emoji(text)


def test_detail_text_renders_workers_and_result_note():
    text = swarm_ui.detail_text(_entry(result_summary="Done: 3 pages summarized."))
    assert "Summarize the release notes" in text
    assert "worker 1" in text
    assert "Done: 3 pages summarized." in text
    _assert_no_emoji(text)


def test_help_text_names_unavailable_controls():
    text = swarm_ui.help_text()
    assert "/swarm status" in text
    assert "live log" in text.lower()
    assert "steer" in text.lower()
    _assert_no_emoji(text)


@pytest.mark.asyncio
async def test_handle_command_no_args_sends_help(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    adapter = FakeAdapter(str(home))
    ui = TelegramSwarmUI(adapter)
    await ui.handle_command(SimpleNamespace(message_id="7"), Source(), "")
    assert adapter.requests
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_handle_callback_unknown_token_answers_expired(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    adapter = FakeAdapter(str(home))
    ui = TelegramSwarmUI(adapter)
    source = Source()
    event = CallbackEvent(source, "101")
    await ui.handle_callback(event, "sw:0123456789ab:view")
    assert event.answers
    assert "expired" in event.answers[0][0].lower() or "not available" in event.answers[0][0].lower()


@pytest.fixture(autouse=True)
def _clean_delegations():
    from tools import async_delegation as ad
    from tools.process_registry import process_registry
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def test_cancel_delegation_stops_the_only_active_one():
    from tools import async_delegation as ad
    gate = threading.Event()
    stopped = threading.Event()

    def blocked_runner():
        gate.wait()
        return {}

    res = ad.dispatch_async_delegation(
        goal="cancel me", context=None, toolsets=None, role="leaf",
        model="m", session_key="agent:main:test:dm:1", max_async_children=1,
        runner=blocked_runner, interrupt_fn=stopped.set,
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"
    try:
        ok, _message = swarm_ui.cancel_swarm_entry(_entry(
            source_id=res["delegation_id"], session_key="agent:main:test:dm:1"))
        assert ok is True
        assert stopped.wait(5.0), "interrupt was not delivered to the delegation"
    finally:
        gate.set()


def test_cancel_delegation_refuses_when_session_has_several():
    from tools import async_delegation as ad
    gates = [threading.Event(), threading.Event()]

    def _runner(idx):
        def run():
            gates[idx].wait()
            return {}
        return run

    results = [
        ad.dispatch_async_delegation(
            goal=f"job {idx}", context=None, toolsets=None, role="leaf",
            model="m", session_key="agent:main:test:dm:1", max_async_children=2,
            runner=_runner(idx), interrupt_fn=gates[idx].set,
            progress_fn=lambda: ((0, None), False),
        )
        for idx in range(2)
    ]
    assert all(res["status"] == "dispatched" for res in results)
    try:
        ok, message = swarm_ui.cancel_swarm_entry(_entry(
            source_id=results[0]["delegation_id"], session_key="agent:main:test:dm:1"))
        assert ok is False
        assert "/stop" in message
    finally:
        for gate in gates:
            gate.set()


def test_cancel_unknown_lifecycle_child_refuses():
    ok, _message = swarm_ui.cancel_swarm_entry(_entry(
        kind="lifecycle", source_id="no-such-child"))
    assert ok is False


def test_kanban_cancel_blocks_workers_and_retry_unblocks(tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn, goal="Map the market",
            workers=[
                SwarmWorkerSpec(profile="a", title="Scan A", body="a"),
                SwarmWorkerSpec(profile="b", title="Scan B", body="b"),
            ],
            verifier_assignee="reviewer", synthesizer_assignee="writer",
        )
        task_ids = list(created.worker_ids) + [created.verifier_id, created.synthesizer_id]
        blocked = swarm_ui.cancel_kanban_cards(conn, task_ids)
        assert blocked == 2
        assert all(kb.get_task(conn, tid).status == "blocked" for tid in created.worker_ids)
        unblocked = swarm_ui.retry_kanban_cards(conn, task_ids)
        assert unblocked == 2
        assert all(kb.get_task(conn, tid).status == "ready" for tid in created.worker_ids)
    finally:
        conn.close()


def test_read_kanban_swarm_state_reports_workers_and_blackboard(tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_swarm import (
        SwarmWorkerSpec, create_swarm, latest_blackboard, post_blackboard_update,
    )
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn, goal="Map the market",
            workers=[SwarmWorkerSpec(profile="a", title="Scan A", body="a")],
            verifier_assignee="reviewer", synthesizer_assignee="writer",
        )
        post_blackboard_update(conn, created.root_id, author="a", key="results", value={"a": "done"})
        state = swarm_ui.read_kanban_swarm_state(conn, created.root_id)
        assert state["root_id"] == created.root_id
        assert len(state["workers"]) == 1
        assert state["workers"][0]["status"] == "ready"
        assert state["verifier"]["status"] == "todo"
        assert state["synthesizer"]["status"] == "todo"
        assert latest_blackboard(conn, created.root_id).get("results") == {"a": "done"}
        assert state["blackboard"].get("results") == {"a": "done"}
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_status_card_uses_scheduler_and_persists_message_id(tmp_path):
    from gateway import swarm_sessions as ss
    home = tmp_path / "home"
    home.mkdir()
    record = ss.create_session(home, "12345", kind="delegation", source_id="d1",
                               goal="card work", chat_id="42", chat_type="dm")
    adapter = FakeAdapter(str(home))
    ui = TelegramSwarmUI(adapter)
    source = Source()
    message_id = await ui.begin_card(source, record, session_key="agent:main:test:dm:1")
    assert message_id is not None
    stored = ss.load_session(home, "12345", record["session_id"])
    assert stored["progress_message_id"] == str(message_id)
    await ui.update_card(source, record, "Card work: still running", session_key="agent:main:test:dm:1")
    await ui.update_card(source, record, "Card work: done", session_key="agent:main:test:dm:1", terminal=True)
    assert adapter.requests or adapter.messages


@pytest.mark.asyncio
async def test_handle_callback_falls_back_to_observer_token(monkeypatch, tmp_path):
    """An observer-issued Cancel token (never in the UI's own registry) is
    resolved via the active observer instead of being rejected as expired."""
    import gateway.swarm_observer as swarm_observer_mod
    from gateway.swarm_observer import SwarmObserver, SwarmRoute

    observer = SwarmObserver()
    monkeypatch.setattr(swarm_observer_mod, "_ACTIVE_OBSERVER", observer)
    route = SwarmRoute(
        platform="telegram", chat_id="42", chat_type="dm", user_id="12345",
        session_key="agent:main:telegram:dm:42:12345",
    )
    card = observer.on_progress_event(
        "subagent.spawn_requested", preview="Summarize the release notes", route=route,
        delegation_id="d1", task_index=0, task_count=1, goal="Summarize the release notes",
    )
    assert card is not None
    home = tmp_path / "home"
    home.mkdir()
    adapter = FakeAdapter(str(home))
    ui = TelegramSwarmUI(adapter)
    source = Source()
    event = CallbackEvent(source, "501")
    await ui.handle_callback(event, f"sw:{card.token}:cancel")
    assert event.answers
    note = event.answers[0][0].lower()
    assert "not available" not in note
    assert "expired" not in note
