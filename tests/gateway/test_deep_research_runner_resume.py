import asyncio
import threading
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.deep_research_sessions import create_session
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tools.todo_tool import TodoLifecycleError, TodoStore
from datetime import UTC


def _runner(home):
    record = create_session(home, "12345", "Attock research", chat_id="123", chat_type="dm")
    context = TurnContext(
        source=SimpleNamespace(platform=Platform.TELEGRAM, chat_id="123", thread_id="77", user_id="12345"),
        message="Conduct deep research on Attock history",
        context_prompt="",
        event_message_id="99",
        _status_thread_metadata={"thread_id": "77"},
        _run_still_current=lambda: True,
        deep_research={"session_id": record["session_id"], "max_turns": 150},
    )

    class Gateway:
        def _get_system_prompt_for_channel(self, *args, **kwargs):
            return ""

        def _resolve_profile_home_for_source(self, _source):
            return home

    runner = object.__new__(TurnRunner)
    runner._ctx = context
    runner._runner = Gateway()
    runner._search_task_requested = False
    runner._deep_research_requested = True
    runner._task_progress_name = "Deep research"
    runner._search_progress_message_id = None
    runner._search_progress_scheduler = None
    runner._search_progress_key = None
    runner._search_progress_last_submitted_text = None
    runner._search_progress_terminal = False
    runner._search_progress_enabled = True
    runner._search_progress_requested = 0
    runner._search_progress_rendered = 0
    runner._search_progress_todos = None
    runner._search_progress_todo_revision = None
    runner._search_progress_rendered_todos = None
    runner._search_progress_activity = "The agent turn started; waiting for its first tool result."
    runner._search_progress_last_edit = 0
    runner._search_progress_update_task = None
    runner._search_progress_status = "running"
    runner._search_progress_rendered_status = None
    runner._search_progress_rendered_stats = None
    runner._search_progress_rendered_activity = None
    runner._search_progress_result = None
    runner._search_started_at = None
    runner._search_metrics = {
        "searches": 0,
        "result_urls": set(),
        "sites": set(),
        "pages_read": set(),
        "read_sites": set(),
    }
    runner._search_metrics_lock = threading.Lock()
    runner._deep_research_tool_history = []
    runner._deep_research_checkpoint_restored = False
    runner._deep_research_source_update_seen = set()
    runner._deep_research_source_update_count = 0
    runner._deep_research_source_update_futures = []
    return runner


def _discard_scheduled(coro, *_args, **_kwargs):
    """Avoid starting progress/checkpoint background work in unit tests."""
    coro.close()


@pytest.mark.parametrize(
    "tool_result",
    [
        "Terminal execution skipped: TTL expired before the command started.",
        "The terminal command timed out after its execution time limit.",
        {"status": "expired", "output": "execution window elapsed"},
        {"exit_code": 17, "stdout": "partial output", "stderr": "command failed"},
    ],
    ids=["skipped-ttl-expiry", "timeout", "structured-expiry", "nonzero-exit-code"],
)
def test_tool_result_success_rejects_terminal_expiry_timeout_and_nonzero_exit(tool_result):
    assert not TurnRunner._tool_result_success("terminal", {"result": tool_result, "is_error": False})


def test_successful_terminal_output_is_valid_strict_todo_work(tmp_path):
    runner = _runner(tmp_path)
    runner._schedule = _discard_scheduled
    store = TodoStore(strict_lifecycle=True)
    store.write([{"id": "research", "content": "Verify primary sources", "status": "in_progress"}])
    runner._ctx.agent_holder = [SimpleNamespace(_todo_store=store)]

    runner._search_progress_event(
        "tool.completed", "terminal", {"result": {"exit_code": 0, "stdout": "verification complete"}, "is_error": False}
    )

    store.write([{"id": "research", "content": "Verify primary sources", "status": "completed"}])
    assert store.read()[0]["status"] == "completed"


def test_skipped_terminal_then_model_completion_claim_cannot_complete_strict_todo(tmp_path):
    runner = _runner(tmp_path)
    runner._schedule = _discard_scheduled
    store = TodoStore(strict_lifecycle=True)
    store.write([{"id": "research", "content": "Verify primary sources", "status": "in_progress"}])
    runner._ctx.agent_holder = [SimpleNamespace(_todo_store=store)]

    runner._search_progress_event(
        "tool.completed", "terminal", {"result": "Terminal tool skipped: TTL expired", "is_error": False}
    )
    # A model-authored todo_list completion is only a claim; it is not verified work.
    runner._search_progress_event(
        "tool.completed",
        "todo_list",
        {"result": {"todos": [{"id": "research", "content": "Verify primary sources", "status": "completed"}], "revision": 2}},
    )

    assert store.read()[0]["status"] == "in_progress"
    with pytest.raises(TodoLifecycleError):
        store.write([{"id": "research", "content": "Verify primary sources", "status": "completed"}])
    assert store.read()[0]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_resume_restores_checkpoint_before_card_reuses_saved_message_and_counts_new_search_once(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    runner._ctx.deep_research["resumed"] = True
    result_urls = [f"https://source-{index:02d}.example.org/article" for index in range(91)]
    read_urls = [f"https://read-{index:02d}.example.org/page" for index in range(15)]
    saved_todo = {
        "id": "verify",
        "content": "Verify primary sources",
        "status": "in_progress",
        "started_at": "2025-03-04T12:00:00+00:00",
        "elapsed_seconds": 41.25,
    }
    runner._ctx.deep_research["checkpoint"] = {
        "research_evidence": {"searches": 21, "result_urls": result_urls, "read_urls": read_urls,
                              "read_sources": [{"url": read_urls[0], "host": "read-00.example.org",
                                                "title": "Previously read primary source"}]},
        "todos": [saved_todo],
        "todo_revision": 7,
        "phase": "verification",
        "progress_message_id": "saved-card-42",
    }

    card_observation = {}

    class Adapter:
        def __init__(self):
            self.edits = []
            self.sends = []

        async def edit_message(self, chat_id, message_id, content, *, metadata=None):
            self.edits.append((chat_id, message_id, content, metadata))
            return SimpleNamespace(success=True, message_id=message_id)

        async def send(self, *args, **kwargs):
            self.sends.append((args, kwargs))
            return SimpleNamespace(success=True, message_id="unexpected-new-card")

    adapter = Adapter()
    runner._runner._delivery_adapter_for = lambda _source: adapter

    class Scheduler:
        async def begin(self, _key, _kind, text, *, send_start, edit, on_started):
            # begin() is the first card registration point; restoration must already be complete.
            card_observation["stats"] = runner._search_stats_snapshot()
            card_observation["todos"] = runner._search_progress_todos
            card_observation["activity"] = runner._search_progress_activity
            card_observation["message_id"] = runner._search_progress_message_id
            card_observation["text"] = text
            message = await send_start(text)
            return message.message_id

    import gateway.telegram_progress_scheduler as progress_scheduler

    monkeypatch.setattr(progress_scheduler, "scheduler_for", lambda _adapter, _chat_id: Scheduler())
    await runner.start_search_progress()

    assert card_observation["stats"] == (21, 91, 91, 15, 15)
    assert card_observation["todos"] == [saved_todo]
    assert card_observation["todos"][0]["status"] == "in_progress"
    assert card_observation["todos"][0]["elapsed_seconds"] == 41.25
    # Neutral resume activity (38007aac80): never name a task as current or
    # restate restored counters as fresh activity before new tool output.
    assert card_observation["activity"] == (
        "Resumed saved session (last saved phase: verification). "
        "Waiting for the first new verified tool output in this run."
    )
    assert card_observation["message_id"] == "saved-card-42"
    assert "21" in card_observation["text"]
    assert adapter.edits and adapter.edits[0][1] == "saved-card-42"
    assert adapter.sends == []
    assert runner._search_progress_message_id == "saved-card-42"
    assert runner._research_evidence_snapshot()["read_sources"] == [
        {"url": read_urls[0], "host": "read-00.example.org", "title": "Previously read primary source"}]

    runner._schedule = _discard_scheduled
    runner._search_progress_event(
        "tool.completed",
        "web_search",
        {"result": {"web": [{"url": "https://new.example.org/new-result"}], "success": True}, "is_error": False},
    )
    assert runner._search_stats_snapshot()[0] == 22


@pytest.mark.asyncio
async def test_resume_with_all_tasks_completed_explains_prior_failure_and_waits_for_new_evidence(tmp_path):
    runner = _runner(tmp_path)
    runner._ctx.deep_research["resumed"] = True
    runner._ctx.deep_research["checkpoint"] = {
        "research_evidence": {"searches": 3, "read_urls": ["https://source.example/read"]},
        "todos": [{"id": "done", "content": "Read primary source", "status": "completed"}],
        "todo_revision": 4,
        "phase": "verification",
        "last_error": "prior report verification did not finish",
    }

    await runner._restore_deep_research_checkpoint()

    activity = runner._search_progress_activity
    # Neutral resume activity (38007aac80): the prior failure and the restored
    # counters are not restated as fresh activity before new tool output.
    assert activity == (
        "Resumed saved session (last saved phase: verification). "
        "Waiting for the first new verified tool output in this run."
    )
    assert runner._search_progress_todos == [{"id": "done", "content": "Read primary source", "status": "completed"}]
    from plugins.platforms.telegram.deep_research_ui import TelegramDeepResearchUI
    prompt = TelegramDeepResearchUI._resume_prompt(
        {"session_id": runner._ctx.deep_research["session_id"], "query": "Original question"},
        runner._ctx.deep_research["checkpoint"],
    )
    assert "concrete repair and re-verification tasks" in prompt
    assert "do not reset completed tasks" in prompt
    assert "prior report verification did not finish" in prompt


@pytest.mark.asyncio
async def test_current_tool_activity_names_actual_task_operation_result_and_measured_source(tmp_path):
    runner = _runner(tmp_path)
    runner._search_progress_todos = [{
        "id": "verify", "content": "Verify the municipal archive", "status": "in_progress",
    }]
    class Scheduler:
        def update(self, *_args, **_kwargs):
            return None

    runner._search_progress_scheduler = Scheduler()
    runner._search_progress_key = "research"
    scheduled = []
    runner._schedule = lambda coro, *_args, **_kwargs: scheduled.append(coro)

    runner._search_progress_event("tool.started", "web_extract", {})
    await scheduled.pop(0)
    assert "Verify the municipal archive" in runner._search_progress_activity
    assert "`web_extract`: reading source pages" in runner._search_progress_activity

    runner._search_progress_event("tool.completed", "web_extract", {
        "result": {"results": [{
            "url": "https://archive.example.org/records/1",
            "title": "Municipal archive record",
            "content": "Do not expose this arbitrary raw source text.",
        }]},
    })
    await scheduled.pop(0)
    for coro in scheduled:
        coro.close()
    assert "Verify the municipal archive" in runner._search_progress_activity
    assert "read 1 source page(s)" in runner._search_progress_activity
    assert "Municipal archive record" in runner._search_progress_activity
    assert "https://archive.example.org/records/1" in runner._search_progress_activity
    assert "arbitrary raw source text" not in runner._search_progress_activity


@pytest.mark.asyncio
async def test_source_receipt_is_bounded_and_strictly_replies_to_research_topic(tmp_path):
    runner = _runner(tmp_path)
    runner.bind_deep_research_routing()
    # Even a stale reply anchor is not used: the receipt is a direct topic reply.
    runner._ctx.deep_research["latest_reply_message_id"] = "999"

    class Adapter:
        def __init__(self):
            self.calls = []

        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            self.calls.append((chat_id, content, reply_to, metadata))
            return SimpleNamespace(success=True, message_id="101")

    adapter = Adapter()
    runner._runner._delivery_adapter_for = lambda _source: adapter
    await runner._send_deep_research_source_update([
        {"title": "Official archive", "url": "https://archive.example.org/item/1"},
        {"title": "Another page", "url": "https://archive.example.org/item/2"},
    ])

    assert len(adapter.calls) == 1
    chat_id, content, reply_to, metadata = adapter.calls[0]
    assert chat_id == "123"
    assert "Official archive — https://archive.example.org/item/1" in content
    assert "Another page — https://archive.example.org/item/2" in content
    assert reply_to == "77"
    assert metadata["telegram_strict_thread"] is True
    assert metadata["deep_research_content"] is True
    assert metadata["thread_id"] == "77"
    assert metadata["telegram_task_reply_chain"] is False
    assert metadata["telegram_task_event_metadata"] is runner._ctx.event_metadata
    assert metadata["deep_research_user_id"] == "12345"
    assert metadata["deep_research_session_id"] == runner._ctx.deep_research["session_id"]


@pytest.mark.asyncio
async def test_source_receipt_caps_attempts_and_records_delivery_failure(tmp_path):
    runner = _runner(tmp_path)
    runner.bind_deep_research_routing()

    class Adapter:
        calls = 0

        async def send(self, *_args, **_kwargs):
            self.calls += 1
            await asyncio.sleep(0)
            return SimpleNamespace(success=False, error="send refused")

    adapter = Adapter()
    runner._runner._delivery_adapter_for = lambda _source: adapter
    await asyncio.gather(*(
        runner._send_deep_research_source_update([
            {"title": f"Read page {index}", "url": f"https://example.org/source/{index}"},
        ])
        for index in range(9)
    ))

    assert adapter.calls == 6
    assert runner._ctx.event_metadata["telegram_task_delivery_failure"] == "send refused"


@pytest.mark.asyncio
async def test_finish_waits_for_pending_source_receipt_before_terminal_state(tmp_path):
    from concurrent.futures import Future

    runner = _runner(tmp_path)
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    runner._search_progress_scheduler = SimpleNamespace(update=lambda *_args, **_kwargs: None)
    runner._search_progress_key = "research"
    pending_receipt = Future()
    runner._deep_research_source_update_futures = [pending_receipt]

    finish = asyncio.create_task(runner.finish_search_progress(
        success=False, result={"failed": True, "failure_reason": "rate_limit"},
    ))
    await asyncio.sleep(0)
    assert runner._search_progress_terminal is False
    assert not finish.done()

    pending_receipt.set_result(SimpleNamespace(success=True, message_id="receipt-1"))
    await finish
    assert runner._search_progress_terminal is True


def test_tool_history_records_failure_and_redacts_credentials(tmp_path):
    runner = _runner(tmp_path)
    kwargs = {
        "is_error": False,
        "result": {
            "exit_code": 9,
            "message": "failed",
            "api_key": "api-secret-123",
            "access_token": "access-secret-456",
            "authorization": "Bearer auth-secret-789",
        },
    }

    runner._record_deep_research_tool_history("terminal", kwargs)

    assert len(runner._deep_research_tool_history) == 1
    entry = runner._deep_research_tool_history[0]
    assert entry["success"] is False
    assert "api-secret-123" not in entry["output_excerpt"]
    assert "access-secret-456" not in entry["output_excerpt"]
    assert "auth-secret-789" not in entry["output_excerpt"]
    assert "[redacted]" in entry["output_excerpt"]


def test_tool_history_retains_more_than_64_entries(tmp_path):
    runner = _runner(tmp_path)

    for index in range(70):
        runner._record_deep_research_tool_history(
            "web_search", {"result": {"success": True, "query_index": index}, "is_error": False}
        )

    assert len(runner._deep_research_tool_history) == 70
    assert runner._deep_research_tool_history[0]["output_excerpt"].find("query_index") >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_initial_checkpoint_does_not_invent_a_plan_or_a_resume(tmp_path, resumed):
    from gateway.deep_research_sessions import update_checkpoint_metadata

    runner = _runner(tmp_path)
    research = runner._ctx.deep_research
    research["resumed"] = resumed
    # Fresh startup creates this routing-only checkpoint before entering the gateway.
    update_checkpoint_metadata(tmp_path, "12345", research["session_id"], latest_reply_message_id="88")
    await runner._restore_deep_research_checkpoint()
    card = runner._search_progress_text()
    assert ("Resumed saved session (last saved phase: starting)" in card) is resumed
    if not resumed:
        assert "Starting deep research" in card
        assert "No task list has been returned" in card
        assert "current task list is empty" not in card
        assert "| Agent tasks | 0 |" not in card
    assert runner._search_stats_snapshot() == (0, 0, 0, 0, 0)

    runner._schedule = _discard_scheduled
    todos = [{"id": str(i), "content": f"Verify source {i}", "status": "pending"} for i in range(6)]
    runner._search_progress_event("tool.completed", "todo_list", {"result": {"todos": todos, "revision": 1}})
    runner._search_progress_event("tool.completed", "web_search", {
        "result": {"success": True, "web": [{"url": "https://source.example/article"}]},
    })
    await runner._restore_deep_research_checkpoint()
    card = runner._search_progress_text()
    assert "| Agent tasks | 6 |" in card
    assert "| Tasks pending | 6 |" in card
    assert runner._search_stats_snapshot() == (1, 1, 1, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_id", ["77", None])
@pytest.mark.parametrize("anchor_kind", ["latest", "welcome", "topic"])
async def test_production_wiring_preserves_research_routing_after_constructor(tmp_path, topic_id, anchor_kind):
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from telethon import types
    from gateway.config import PlatformConfig
    from gateway.run import GatewayRunner
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    old = _runner(tmp_path)
    ctx = old._ctx
    ctx.source.thread_id = topic_id
    ctx.source.chat_type = "dm"
    ctx.event_message_id = "99"
    ctx._status_thread_metadata = {"thread_id": "1", "direct_messages_topic_id": "1"}
    expected_anchor = {"latest": "88", "welcome": "89", "topic": topic_id}[anchor_kind]
    if anchor_kind == "latest":
        ctx.deep_research["latest_reply_message_id"] = "88"
    if anchor_kind in {"latest", "welcome"}:
        ctx.deep_research["topic_welcome_message_id"] = "89"
    ctx.event_metadata = {"telegram_task_reply_to_message_id": "99"}
    runner = TurnRunner(old._runner, ctx)
    gateway = object.__new__(GatewayRunner)
    gateway.hooks = SimpleNamespace()
    gateway._delivery_adapter_for = lambda _source: SimpleNamespace()
    # Keep the real generic metadata helpers AND binding stage, not just __init__.
    metadata = gateway._run_agent_bind_turn_wiring(ctx, runner, ctx.source, ctx.event_message_id, False)
    assert metadata is ctx._status_thread_metadata
    for route in (metadata, ctx._progress_metadata):
        assert route["telegram_strict_thread"] is True
        assert route["deep_research_content"] is True
        assert route["thread_id"] == (topic_id or "")
        assert route["telegram_reply_to_message_id"] == expected_anchor
        assert route["deep_research_session_id"] == ctx.deep_research["session_id"]
        assert route["deep_research_user_id"] == ctx.source.user_id
        assert route["telegram_task_event_metadata"] is ctx.event_metadata
    assert ctx._progress_reply_to == expected_anchor

    adapter = TelethonAdapter(PlatformConfig())
    requests = []
    now = datetime.now(UTC)

    class Client:
        async def get_messages(self, peer, *, ids):
            # Both the old trigger and even a corrupt saved anchor belong elsewhere.
            return types.Message(
                id=ids, peer_id=types.PeerUser(123), message="Parent topic", date=now,
                reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=1),
            )

        async def __call__(self, request):
            requests.append(request)
            return types.UpdateShort(types.UpdateMessageID(id=900 + len(requests), random_id=request.random_id), date=now)

    adapter._client = Client()
    adapter._input_peer_for = AsyncMock(return_value=types.InputPeerUser(123, 0))
    for route in (metadata, ctx._progress_metadata):
        live_anchor = ctx.deep_research.get("latest_reply_message_id") or expected_anchor
        if live_anchor:
            assert adapter._reply_target("99", route) == int(live_anchor)
        result = await adapter.send(ctx.source.chat_id, "Research progress", reply_to="99", metadata=route)
        assert result.success is bool(topic_id)
        if topic_id:
            assert requests[-1].peer.user_id == int(ctx.source.chat_id)
            assert requests[-1].reply_to.reply_to_msg_id == int(topic_id)
            # Root-addressed topic sends keep top_msg_id: without it the
            # message lands outside the topic and can never be edited in place.
            assert requests[-1].reply_to.top_msg_id == int(topic_id)
        else:
            assert requests == []  # No topic must fail closed, never send to the parent.
