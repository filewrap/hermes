import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn import GatewayTurnMixin
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from gateway.deep_research_sessions import create_session, load_session


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
        "searches": 0, "result_urls": set(), "sites": set(), "pages_read": set(), "read_sites": set(),
    }
    return runner


def test_deep_research_prompt_uses_configured_turns_and_correct_private_path_semantics(tmp_path):
    runner = _runner(tmp_path)
    prompt = runner._combined_ephemeral_prompt()
    assert "150 agent turns" in prompt
    assert "Approval prompts are disabled for this session's scoped work" in prompt
    assert "never perform consequential external actions" in prompt
    assert "credible primary sources and authoritative publications" in prompt
    assert "cross-check material claims against independent evidence" in prompt
    assert "nearby numeric citations" in prompt
    assert "do not force a consensus" in prompt
    assert "Use the real Hermes agent loop" in prompt
    assert "Do not stop after an initial model turn, a plan, one search, or the first file write" in prompt
    assert "For file tools" in prompt
    assert "use only session-relative names" in prompt
    assert "never pass `/workspace/...`" in prompt
    assert "The isolated shell alone sees this session directory mounted at `/workspace`" in prompt
    assert "readable cited report to `report.md`" in prompt
    assert "actual `todo_list` snapshot" in prompt
    assert "never invent UI tasks, statuses, phases, or percentages" in prompt


def test_deep_research_progress_card_contains_only_actual_state_and_native_markdown_table(tmp_path):
    runner = _runner(tmp_path)
    card = runner._search_progress_text()
    assert card.startswith("## Deep research\n**Status: Running**")
    assert "waiting for its first tool result" in card
    assert "## Agent tasks" in card
    assert "No task list has been returned by the agent yet" in card
    assert "| Measured activity | Count |" in card
    assert "| Successful web searches | 0 |" in card
    assert "Overall" not in card
    assert "To-do" not in card
    assert "Agent plan" not in card
    assert "☐" not in card and "☑" not in card
    assert "```" not in card


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_worker_failure_finalization_pauses_research_with_safe_error(tmp_path, cancelled):
    runner = _runner(tmp_path)
    runner._search_progress_scheduler = object()
    runner._search_progress_key = "research-key"
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()

    async def no_card(**_kwargs):
        pass

    runner._request_search_progress = no_card
    error = RuntimeError("secret-bearing provider error")
    error.failure_reason = "provider_unavailable"
    if cancelled:
        error = asyncio.CancelledError()
    gateway = object.__new__(GatewayTurnMixin)

    async def await_worker(*_args):
        raise error

    gateway._run_agent_await_turn_worker = await_worker
    with pytest.raises(type(error)) as caught:
        await gateway._run_agent_await_worker_with_progress(None, None, None, None, runner)
    assert caught.value is error

    session = load_session(tmp_path, "12345", runner._ctx.deep_research["session_id"])
    assert session["status"] == ("interrupted" if cancelled else "paused")
    assert session["failure_class"] == ("interrupted" if cancelled else "provider_unavailable")
    assert "secret-bearing" not in session["last_error"]
    assert runner._search_progress_terminal


def test_provider_unavailable_failure_copy_is_safe_and_actionable():
    failure_class, message = TurnRunner._deep_research_failure({"failure_reason": "provider_unavailable"})
    assert failure_class == "provider_unavailable"
    assert "resume this session" in message


@pytest.mark.asyncio
async def test_research_progress_is_pinned_to_the_created_topic(tmp_path):
    runner = _runner(tmp_path)
    runner._ctx.event_message_id = None
    runner._ctx._status_thread_metadata = {"thread_id": "1"}
    sent = []

    class Adapter:
        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            sent.append((chat_id, content, reply_to, metadata))
            return SimpleNamespace(success=True, message_id="900")

    adapter = Adapter()
    runner._runner = SimpleNamespace(
        _delivery_adapter_for=lambda _source: adapter,
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    await runner.start_search_progress()

    assert sent[0][3]["thread_id"] == "77"
    assert sent[0][3]["telegram_send_in_topic"] is True
    assert "## Deep research" in sent[0][1]
    assert "| Measured activity | Count |" in sent[0][1]
    assert "```" not in sent[0][1]
    assert "To-do" not in sent[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,expected_anchor", [("dm", "77"), ("group", "88")])
async def test_deep_research_first_card_anchors_to_topic_root_in_threaded_dm(tmp_path, chat_type, expected_anchor):
    """In a threaded DM the card must reply to the topic root, not the welcome message.

    The strict-topic edit gate only verifies a DM card that replies directly to
    the topic root (verified_topic_reply path 2); a welcome-anchored card can
    never be edited, so the scheduler disables it and the card freezes. In a
    group forum the echo carries reply_to_top_id, so the latest-reply anchor
    stays valid there.
    """
    runner = _runner(tmp_path)
    runner._ctx.source.chat_type = chat_type
    runner._ctx.deep_research["latest_reply_message_id"] = "88"
    runner._ctx.deep_research["topic_welcome_message_id"] = "89"
    sent = []

    class Adapter:
        async def send(self, chat_id, content, *, reply_to=None, metadata=None):
            sent.append((chat_id, content, reply_to, metadata))
            return SimpleNamespace(success=True, message_id="900")

    adapter = Adapter()
    runner._runner = SimpleNamespace(
        _delivery_adapter_for=lambda _source: adapter,
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )
    await runner.start_search_progress()

    assert sent
    assert sent[0][2] == expected_anchor
    assert sent[0][3]["thread_id"] == "77"
    assert sent[0][3]["telegram_send_in_topic"] is True


@pytest.mark.asyncio
async def test_deep_research_first_contact_skips_unrelated_sethome_notice():
    class Store:
        async def has_any_sessions(self):
            return True

    gateway = object.__new__(GatewayTurnMixin)
    gateway.async_session_store = Store()
    notices = []

    async def deliver(_source, content):
        notices.append(content)

    gateway._deliver_platform_notice = deliver
    # Real MessageEvents carry ``internal`` (False for a human first contact);
    # canary's _hmwa_first_contact_notes skips internal/plugin turns up front.
    event = SimpleNamespace(internal=False, metadata={"deep_research": {"session_id": "abc123def456"}})
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="42")

    await gateway._hmwa_first_contact_notes(event, source, [], [])
    assert notices == []


@pytest.mark.asyncio
async def test_worker_exception_finalizes_progress_and_preserves_original_even_if_finalizer_fails():
    gateway = object.__new__(GatewayTurnMixin)
    original = RuntimeError("worker failed")
    async def await_worker(*_args):
        raise original
    gateway._run_agent_await_turn_worker = await_worker
    calls = []
    async def finish(**kwargs):
        calls.append(kwargs)
        raise ValueError("finalizer failed")
    runner = SimpleNamespace(search_progress_enabled=True, _search_progress_terminal=False,
                             finish_search_progress=finish)
    with pytest.raises(RuntimeError) as caught:
        await gateway._run_agent_await_worker_with_progress(None, None, None, None, runner)
    assert caught.value is original
    assert calls == [{"success": False, "result": {"failure_reason": "worker_failure", "interrupted": False}}]


@pytest.mark.asyncio
async def test_worker_cancellation_finalizes_as_interrupted():
    gateway = object.__new__(GatewayTurnMixin)
    async def await_worker(*_args):
        raise asyncio.CancelledError
    gateway._run_agent_await_turn_worker = await_worker
    calls = []
    async def finish(**kwargs):
        calls.append(kwargs)
    runner = SimpleNamespace(search_progress_enabled=True, _search_progress_terminal=False,
                             finish_search_progress=finish)
    with pytest.raises(asyncio.CancelledError):
        await gateway._run_agent_await_worker_with_progress(None, None, None, None, runner)
    assert calls == [{"success": False, "result": {"failure_reason": "interrupted", "interrupted": True}}]


@pytest.mark.asyncio
async def test_worker_exception_does_not_duplicate_terminal_finalization():
    gateway = object.__new__(GatewayTurnMixin)
    async def await_worker(*_args):
        raise RuntimeError("worker failed")
    gateway._run_agent_await_turn_worker = await_worker
    calls = []
    async def finish(**kwargs):
        calls.append(kwargs)
    runner = SimpleNamespace(search_progress_enabled=True, _search_progress_terminal=True,
                             finish_search_progress=finish)
    with pytest.raises(RuntimeError):
        await gateway._run_agent_await_worker_with_progress(None, None, None, None, runner)
    assert calls == []


@pytest.mark.asyncio
async def test_generic_success_without_read_sources_is_paused_as_resumable(tmp_path):
    runner = _runner(tmp_path)
    record = load_session(tmp_path, "12345", runner._ctx.deep_research["session_id"])
    session_dir = tmp_path / "deep_research_sessions" / "12345" / record["session_id"]
    (session_dir / "report.md").write_text("A generic answer with no sources.", encoding="utf-8")
    runner._search_progress_scheduler = object()
    runner._search_progress_key = "research-key"
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()

    async def no_progress(**_kwargs):
        pass

    runner._request_search_progress = no_progress
    await runner.finish_search_progress(success=True, result={"completed": True})

    session = load_session(tmp_path, "12345", record["session_id"])
    assert session["status"] == "paused"
    assert session["failure_class"] == "insufficient_research_evidence"
    assert "successfully read source pages" in session["last_error"]


def test_research_prompt_requires_sequential_verified_tasks_and_safe_path_recovery(tmp_path):
    prompt = _runner(tmp_path)._combined_ephemeral_prompt()
    assert "mark a task in_progress BEFORE" in prompt
    assert "Complete at most one task per update" in prompt
    assert "only the delivery callback can confirm" in prompt
    assert "not estimated by you" in prompt
    assert "not a loss of all research capability" in prompt


def test_completed_research_tasks_show_measured_duration_not_card_age(tmp_path):
    runner = _runner(tmp_path)
    parsed = runner._progress_todo_snapshot_from_result({"revision": 3, "todos": [
        {"id": "a", "content": "Read sources", "status": "completed", "elapsed_seconds": 73.2},
        {"id": "b", "content": "Legacy task", "status": "completed"},
        {"id": "c", "content": "Invalid timing", "status": "completed", "elapsed_seconds": float('nan')},
    ]})
    runner._search_progress_todos = parsed[0]
    card = runner._search_progress_text()
    assert "Read sources (elapsed: 1m 13s)" in card
    assert "Legacy task (elapsed: not recorded)" in card
    assert "Invalid timing (elapsed: not recorded)" in card
    assert "not successful final delivery" in card


@pytest.mark.asyncio
async def test_coalesced_progress_retains_each_observed_task_transition(tmp_path):
    import threading
    runner = _runner(tmp_path)
    runner._search_metrics_lock = threading.Lock()
    runner._search_progress_todos = None
    runner._search_progress_todo_revision = None
    scheduled = []
    runner._schedule = lambda coro, _message: scheduled.append(asyncio.create_task(coro))
    for revision, status in enumerate(["in_progress", "completed"], start=1):
        runner._search_progress_event("tool.completed", "todo_list", {"result": {
            "revision": revision, "todos": [{"id": "read", "content": "Read independent sources", "status": status,
                                             **({"elapsed_seconds": 12.5} if status == "completed" else {})}],
        }})
    await asyncio.gather(*scheduled)
    card = runner._search_progress_text()
    assert "Recent task changes" in card
    assert "Read independent sources — in progress" in card
    assert "Read independent sources — completed; elapsed: 12.5s" in card
    assert "Observed during this run" in card
    assert "chat edits may be combined" in card
    # A stale callback must not erase measured transitions or current state.
    runner._search_progress_event("tool.completed", "todo_list", {"result": {
        "revision": 1, "todos": [{"id": "read", "content": "Read independent sources", "status": "in_progress"}],
    }})
    assert runner._search_progress_todos[0]["status"] == "completed"
    assert len(runner._search_task_transitions) == 2


def _valid_artifacts(tmp_path, runner):
    from gateway.deep_research_sessions import resolve_session_dir, save_checkpoint
    sid = runner._ctx.deep_research["session_id"]
    directory = resolve_session_dir(tmp_path, "12345", sid)
    urls = ["https://agency.example/report", "https://university.example/paper"]
    (directory / "research_notes.md").write_text("Read source findings\n" + "\n".join(urls), encoding="utf-8")
    (directory / "report.md").write_text(
        "Evidence supports findings [1] and [2].\n\n## References\n"
        f"[1] Agency — {urls[0]}\n[2] University — {urls[1]}\n", encoding="utf-8")
    save_checkpoint(tmp_path, "12345", sid, research_evidence={"read_urls": urls})
    return sid


@pytest.mark.asyncio
async def test_rate_limited_final_answer_keeps_verified_report_but_never_completes(tmp_path):
    from gateway.deep_research_sessions import load_checkpoint
    runner = _runner(tmp_path)
    sid = _valid_artifacts(tmp_path, runner)
    runner._search_progress_scheduler = object()
    runner._search_progress_key = "key"
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    cards = []
    async def capture(**kwargs):
        cards.append(kwargs)
    runner._request_search_progress = capture
    await runner.finish_search_progress(success=False, result={"failed": True, "failure_reason": "rate_limit"})
    session = load_session(tmp_path, "12345", sid)
    checkpoint = load_checkpoint(tmp_path, "12345", sid)
    assert session["status"] == "paused"
    assert session["artifact_status"] == checkpoint["artifact_status"] == "ready"
    assert session["final_response_status"] == checkpoint["final_response_status"] == "failed"
    assert cards[-1]["state"] == "paused"
    assert "Verified report saved privately" in cards[-1]["activity"]
    assert "Final response: failed" in cards[-1]["activity"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["pending", "delivered", "failed"])
async def test_complete_card_requires_confirmed_report_delivery(tmp_path, delivery):
    from gateway.deep_research_sessions import update_session
    runner = _runner(tmp_path)
    sid = _valid_artifacts(tmp_path, runner)
    runner._ctx.session_key = "session"
    runner._search_progress_scheduler = object()
    runner._search_progress_key = "key"
    callbacks = []
    adapter = SimpleNamespace(register_post_delivery_callback=lambda _key, callback, **_kw: callbacks.append(callback))
    runner._runner._delivery_adapter_for = lambda _source: adapter
    cards = []
    async def capture(**kwargs):
        cards.append(kwargs)
    runner._request_search_progress = capture
    await runner.finish_search_progress(success=True, result={"completed": True})
    assert cards == []  # Never announce completion before delivery callbacks.
    update_session(tmp_path, "12345", sid, status="completed", final_response_status=delivery)
    await callbacks[0]()
    assert cards[-1]["state"] == ("complete" if delivery == "delivered" else "paused")


@pytest.mark.asyncio
async def test_progress_ui_unavailable_does_not_suppress_failure_persistence(tmp_path):
    runner = _runner(tmp_path)
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    assert runner._search_progress_scheduler is None
    await runner.finish_search_progress(success=False, result={"failed": True, "failure_reason": "rate_limit"})
    session = load_session(tmp_path, "12345", runner._ctx.deep_research["session_id"])
    assert session["status"] == "paused"
    assert session["final_response_status"] == "failed"


@pytest.mark.asyncio
async def test_only_successful_non_todo_tool_work_satisfies_lifecycle_gate(tmp_path):
    import threading
    runner = _runner(tmp_path)
    runner._search_metrics_lock = threading.Lock()
    work = []
    runner._ctx.agent_holder = [SimpleNamespace(_todo_store=SimpleNamespace(note_work_result=lambda **kw: work.append(kw)))]
    scheduled = []
    runner._schedule = lambda coro, _message: scheduled.append(asyncio.create_task(coro))
    runner._search_progress_event("tool.completed", "read_file", {"result": {"content": "report"}})
    runner._search_progress_event("tool.completed", "read_file", {"is_error": True})
    runner._search_progress_event("tool.completed", "read_file", {"result": {"success": False}})
    runner._search_progress_event("tool.completed", "read_file", {"result": {"error": "blocked"}})
    runner._search_progress_event("tool.completed", "todo_list", {"result": {"todos": [], "revision": 1}})
    await asyncio.gather(*scheduled)
    assert work == [{"success": True}, {"success": False}, {"success": False}, {"success": False}]


@pytest.mark.parametrize("topic_id", ["77", None])
def test_all_research_interim_metadata_is_strictly_topic_bound(tmp_path, topic_id):
    old = _runner(tmp_path)
    ctx = old._ctx
    ctx.source.thread_id = topic_id
    ctx._status_thread_metadata = {"thread_id": "1", "direct_messages_topic_id": "1"}
    ctx.event_metadata = {"unrelated": True}
    TurnRunner(old._runner, ctx)
    assert ctx._status_thread_metadata["thread_id"] == (topic_id or "")
    assert ctx._status_thread_metadata["telegram_strict_thread"] is True
    assert ctx._status_thread_metadata["deep_research_content"] is True
    assert ctx._status_thread_metadata["deep_research_session_id"] == ctx.deep_research["session_id"]


@pytest.mark.asyncio
async def test_verified_report_with_unfinished_tasks_is_not_session_completion(tmp_path):
    runner = _runner(tmp_path)
    sid = _valid_artifacts(tmp_path, runner)
    runner._search_progress_todos = [{"id": "verify", "content": "Verify report", "status": "in_progress"}]
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    await runner.finish_search_progress(success=True, result={"completed": True})
    session = load_session(tmp_path, "12345", sid)
    assert session["status"] == "paused"
    assert session["artifact_status"] == "ready"
    assert session["failure_class"] == "unfinished_research_tasks"
    assert session["final_response_status"] == "failed"


@pytest.mark.asyncio
async def test_failed_turn_checkpoints_latest_active_task_runtime(tmp_path):
    from tools.todo_tool import TodoStore
    from gateway.deep_research_sessions import load_checkpoint
    runner = _runner(tmp_path)
    store = TodoStore(strict_lifecycle=True)
    store.write([{"id": "read", "content": "Read sources", "status": "in_progress"}])
    store.note_work_result(success=True)
    runner._ctx.agent_holder = [SimpleNamespace(_todo_store=store)]
    runner._runner._delivery_adapter_for = lambda _source: SimpleNamespace()
    await asyncio.sleep(0.01)
    await runner.finish_search_progress(success=False, result={"failed": True, "failure_reason": "rate_limit"})
    todo = load_checkpoint(tmp_path, "12345", runner._ctx.deep_research["session_id"])["todos"][0]
    assert todo["elapsed_seconds"] >= 0.01
    assert "started_at" in todo
    assert todo["work_observed"] is True
    assert todo["status"] == "in_progress"


def test_fresh_deep_research_seeds_canonical_task_plan_without_overwriting(tmp_path):
    from tools.todo_tool import TodoStore

    runner = _runner(tmp_path)
    agent = SimpleNamespace(_todo_store=TodoStore())
    assert runner._prepare_research_todo_resume(agent, []) is None
    seeded = agent._todo_store.snapshot()["todos"]
    assert [task["id"] for task in seeded] == ["dr-scope", "dr-search", "dr-read-verify", "dr-report", "dr-verify"]
    assert all(task["status"] == "pending" for task in seeded)

    custom = TodoStore()
    custom.restore([{"id": "custom-1", "content": "Existing plan task", "status": "in_progress"}], revision=3)
    agent2 = SimpleNamespace(_todo_store=custom)
    assert runner._prepare_research_todo_resume(agent2, []) is None
    kept = agent2._todo_store.snapshot()["todos"]
    assert [task["id"] for task in kept] == ["custom-1"]
    assert kept[0]["status"] == "in_progress"
