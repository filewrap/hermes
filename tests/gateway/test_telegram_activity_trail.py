from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.telegram_activity import tool_activity_event, tool_activity_message, turn_activity_event
from gateway.turn_context import TurnContext


def _runner(*, chat_type="dm", user_id="123", progress_grouping="separate"):
    ctx = TurnContext(
        source=SimpleNamespace(
            platform=Platform.TELEGRAM, chat_type=chat_type, user_id=user_id, chat_id=user_id,
        ),
        progress_queue=queue.Queue(), telegram_activity_trail_enabled=True,
        progress_grouping=progress_grouping, _run_still_current=lambda: True,
    )
    return TurnRunner(SimpleNamespace(adapters={}), ctx), ctx


def test_activity_events_expose_only_safe_tool_and_file_facts():
    start = tool_activity_event(
        "call-1", "read_file", "running",
        {"path": "/home/private/project/notes.txt", "offset": 12, "limit": 4, "token": "do-not-show"},
    )
    assert start == {
        "kind": "telegram_activity", "event": "tool", "call_id": "call-1",
        "tool": "Read File", "status": "running", "detail": "notes.txt · lines 12–15",
    }
    assert "private/project" not in str(start) and "do-not-show" not in str(start)
    assert "notes.txt" in tool_activity_message(start)
    assert "lines 12–15" in tool_activity_message(start)

    write = tool_activity_event(
        "call-2", "write_file", "running", {"path": "/tmp/out.py", "content": "one\ntwo\n"},
    )
    assert write["detail"] == "out.py · 2 lines"
    assert "one" not in tool_activity_message(write) and "two" not in tool_activity_message(write)
    patch = tool_activity_event(
        "call-3", "patch", "running",
        {"path": "/tmp/change.py", "old_string": "before", "new_string": "after\nand more"},
    )
    assert patch["detail"] == "change.py · 3 changed lines"

    terminal = tool_activity_event(
        "call-4", "terminal", "running", {"command": "cat /private/key", "api_key": "secret"},
    )
    assert tool_activity_message(terminal) == "<p>⏳ Running command</p>"
    assert "private" not in str(terminal) and "secret" not in str(terminal)
    assert tool_activity_event("call-5", "terminal\nsecret", "running")["tool"] == "Tool"
    assert tool_activity_event("call-6", "terminal", "unknown") is None
    assert turn_activity_event("turn-complete") == {
        "kind": "telegram_activity", "event": "turn", "status": "turn-complete",
    }


@pytest.mark.asyncio
async def test_activity_queue_sends_standalone_messages_and_deletes_on_matching_completion():
    runner, ctx = _runner(progress_grouping="separate")
    sent = []
    deleted = []

    class Adapter:
        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(success=True, message_id=f"message-{len(sent)}")

        async def delete_message(self, chat_id, message_id):
            deleted.append((chat_id, str(message_id)))
            return True

        async def edit_message(self, **_kwargs):
            raise AssertionError("activity must not edit a shared card")

    adapter = Adapter()
    runner.telegram_activity_tool_start_callback(
        "read-1", "read_file", {"path": "/home/private/plan.md", "offset": 2, "limit": 3},
    )
    runner.telegram_activity_tool_complete_callback(
        "read-1", "read_file", {"path": "/home/private/plan.md"}, {"content": "private output"},
    )
    runner.telegram_activity_tool_start_callback(
        "shell-1", "terminal", {"command": "cat /private/key"},
    )
    runner.finish_activity_trail({"completed": True, "failed": False})

    runner._runner._delivery_adapter_for = lambda _source: adapter
    await runner.send_progress_messages()

    assert [item["content"] for item in sent] == [
        "<p>⏳ Reading <code>plan.md</code> (lines 2–4)</p>",
        "<p>⏳ Running command</p>",
    ]
    assert all(item["metadata"]["rich_format"] == "html" for item in sent)
    assert all("/home/private" not in str(item) and "cat /private/key" not in str(item) for item in sent)
    assert deleted == [("123", "message-1"), ("123", "message-2")]
    assert runner._telegram_activity_message_ids == {}
    assert ctx.progress_queue.empty()


@pytest.mark.asyncio
async def test_unfinished_activity_message_is_deleted_at_turn_interruption():
    runner, _ctx = _runner()
    sent = []
    deleted = []

    class Adapter:
        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(success=True, message_id="temporary-1")

        async def delete_message(self, chat_id, message_id):
            deleted.append((chat_id, message_id))
            return True

    runner.telegram_activity_tool_start_callback("call-1", "web_search", {"query": "private"})
    runner.finish_activity_trail({"interrupted": True})
    await runner._consume_telegram_activity_queue(Adapter())

    assert len(sent) == 1
    assert deleted == [("123", "temporary-1")]
    assert "private" not in sent[0]["content"]


def test_opted_in_callbacks_queue_only_sanitized_lifecycle_facts():
    runner, ctx = _runner()
    counters = {"model": 0, "tool": 0}
    secret_args = {"command": "cat /secret/file", "token": "do-not-show"}
    runner.telegram_activity_tool_start_callback("call-1", "terminal", secret_args)
    counters["tool"] += 1
    result = {"exit_code": 0, "output": "private result"}
    runner.telegram_activity_tool_complete_callback("call-1", "terminal", secret_args, result)
    runner.finish_activity_trail({"completed": True, "failed": False})

    events = [ctx.progress_queue.get_nowait() for _ in range(ctx.progress_queue.qsize())]
    assert [event["status"] for event in events] == ["running", "succeeded", "turn-complete"]
    assert all("args" not in event and "command" not in event and "token" not in event for event in events)
    assert counters == {"model": 0, "tool": 1}


def test_group_chat_isolation_and_terminal_failure_or_interrupt_are_honest():
    runner, ctx = _runner(chat_type="group")
    runner.telegram_activity_tool_start_callback("call-1", "web_search", {})
    assert ctx.progress_queue.empty()

    dm_runner, dm_ctx = _runner()
    dm_runner.telegram_activity_tool_start_callback("call-1", "web_search", {})
    dm_runner.finish_activity_trail({"failed": True})
    dm_runner.telegram_activity_tool_start_callback("call-2", "read_file", {})
    failed_events = [dm_ctx.progress_queue.get_nowait() for _ in range(dm_ctx.progress_queue.qsize())]
    assert failed_events[-1] == turn_activity_event("turn-failed")
    assert len(failed_events) == 2

    interrupted_runner, interrupted_ctx = _runner()
    interrupted_runner.telegram_activity_tool_start_callback("call-1", "web_search", {})
    interrupted_runner.finish_activity_trail({"interrupted": True})
    interrupted_events = [
        interrupted_ctx.progress_queue.get_nowait() for _ in range(interrupted_ctx.progress_queue.qsize())
    ]
    assert interrupted_events[-1] == turn_activity_event("turn-interrupted")


def test_unfinished_tool_result_is_not_in_activity_event():
    runner, ctx = _runner()
    runner.telegram_activity_tool_start_callback("call-1", "terminal", {"command": "private"})
    runner.telegram_activity_tool_complete_callback(
        "call-1", "terminal", {"command": "private"}, {"exit_code": 7, "error": "private detail"},
    )
    runner.finish_activity_trail({"failed": True})
    events = [ctx.progress_queue.get_nowait() for _ in range(ctx.progress_queue.qsize())]
    assert [event["status"] for event in events] == ["running", "failed", "turn-failed"]
    assert all("private detail" not in str(event) and "command" not in str(event) for event in events)


@pytest.mark.asyncio
async def test_personal_setting_loader_is_scoped_to_private_user_and_profile(tmp_path):
    from gateway.run_turn import GatewayTurnMixin
    from gateway.telegram_personal_settings import save_personal_settings

    save_personal_settings(tmp_path, "123", {"activity_trail_enabled": True})
    save_personal_settings(tmp_path, "456", {"activity_trail_enabled": False})
    fake_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: tmp_path)
    owner = SimpleNamespace(platform=Platform.TELEGRAM, chat_type="dm", user_id="123")
    other_user = SimpleNamespace(platform=Platform.TELEGRAM, chat_type="dm", user_id="456")
    group = SimpleNamespace(platform=Platform.TELEGRAM, chat_type="group", user_id="123")

    first = await GatewayTurnMixin._load_telegram_personal_settings_for_turn(fake_runner, owner)
    second = await GatewayTurnMixin._load_telegram_personal_settings_for_turn(fake_runner, other_user)
    rejected = await GatewayTurnMixin._load_telegram_personal_settings_for_turn(fake_runner, group)
    assert first["activity_trail_enabled"] is True
    assert second["activity_trail_enabled"] is False
    assert rejected is None


def test_agent_callback_wiring_uses_only_one_safe_activity_lane():
    runner, ctx = _runner()
    ctx.user_config = {}
    ctx._hooks_ref = SimpleNamespace(loaded_hooks=[])
    ctx.native_tool_start_callback = runner.combined_tool_start_callback
    ctx.native_tool_complete_callback = runner.native_tool_complete_callback
    ctx.progress_callback = runner.progress_callback
    agent = SimpleNamespace(request_overrides={})
    runner._runner._service_tier = None
    runner._runner._consume_pending_turn_sidecar_notes = lambda _session: []
    runner._make_bg_review_callbacks = lambda: (lambda _message: None, lambda: None)
    runner._attach_session_title_callback = lambda _agent, _ctx: None

    runner._wire_turn_agent_callbacks(agent, {}, None, None, None, False)
    assert agent.tool_start_callback.__self__ is runner
    assert agent.tool_complete_callback.__self__ is runner

    agent.tool_progress_callback("tool.started", "terminal", "rm /private", {"command": "rm /private"})
    agent.tool_start_callback("call-1", "terminal", {"command": "rm /private"})
    agent.tool_complete_callback("call-1", "terminal", {"command": "rm /private"}, {"exit_code": 0})
    runner.finish_activity_trail({"completed": True})
    events = [ctx.progress_queue.get_nowait() for _ in range(ctx.progress_queue.qsize())]
    assert [event["status"] for event in events] == ["running", "succeeded", "turn-complete"]
