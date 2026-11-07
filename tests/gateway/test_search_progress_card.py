import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from telethon import types

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.telegram.mtproto_rich import send_request
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter


class SearchCardAdapter:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "reply_to": reply_to, "metadata": metadata})
        return SendResult(success=True, message_id="search-card-1")

    async def edit_message(self, chat_id, message_id, content, *, metadata=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "content": content})
        return SendResult(success=True, message_id=message_id)


class CapturingRichClient:
    def __init__(self):
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return SimpleNamespace()


class SearchCardGateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _delivery_adapter_for(self, _source):
        return self.adapter

    def _get_system_prompt_for_channel(self, *_args, **_kwargs):
        return ""


def _turn(message="/search Attock, the gate of northern India", platform=Platform.TELEGRAM):
    source = SimpleNamespace(
        platform=platform, chat_id="123", thread_id="77", parent_chat_id=None,
    )
    ctx = TurnContext(
        source=source,
        message=message,
        context_prompt="platform context",
        event_message_id="50",
        _status_thread_metadata={"thread_id": "77"},
        _run_still_current=lambda: True,
    )
    adapter = SearchCardAdapter()
    gateway = SearchCardGateway(adapter)
    return TurnRunner(gateway, ctx), adapter


def _table_values(card):
    values = {}
    for line in card.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 2 and cells[1].isdigit():
            values[cells[0]] = cells[1]
    return values


async def _send_event(runner, event_type, tool_name, kwargs=None):
    scheduled = []
    runner._schedule = lambda coro, _message: scheduled.append(asyncio.create_task(coro))
    scheduler = runner._search_progress_scheduler
    scheduler._last_intermediate_edit = time.monotonic() - 11
    runner._search_progress_event(event_type, tool_name, kwargs or {})
    if scheduled:
        await asyncio.gather(*scheduled)
    await _wait_for_latest_card(runner)


async def _wait_for_latest_card(runner):
    target = runner._search_progress_last_submitted_text
    scheduler = runner._search_progress_scheduler
    for _ in range(200):
        card = scheduler._cards.get(runner._search_progress_key)
        if card is not None and card.rendered_text == target:
            return
        if any(edit["content"] == target for edit in runner._runner.adapter.edits):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("scheduler did not render the latest progress-card state")


@pytest.mark.asyncio
async def test_search_posts_one_native_rich_card_without_fabricated_progress():
    runner, adapter = _turn()
    assert runner.search_progress_enabled

    prompt = runner._combined_ephemeral_prompt()
    assert "real multi-round web search" in prompt
    assert "real agent tool loop" in prompt
    assert "native Markdown tables, blockquotes, or other rich structures" in prompt
    assert "Do not stop after planning, one search call, or a tool error" in prompt
    assert "actual `todo_list` result" in prompt
    assert "never invent UI tasks, statuses, phases, or percentages" in prompt
    assert "Do not call `execute_code` or `terminal` solely" in prompt

    await runner.start_search_progress()
    assert len(adapter.sent) == 1
    assert adapter.sent[0]["reply_to"] == "50"
    initial = adapter.sent[0]["content"]
    assert initial.startswith("## Search\n**Status: Running**")
    assert "waiting for its first tool result" in initial
    assert "## Agent tasks" in initial
    assert "No task list has been returned by the agent yet" in initial
    assert "__No task list has been returned by the agent yet.__" in initial
    assert "| Measured activity | Count |" in initial
    assert "| Agent tasks |" not in initial
    assert "| Successful web searches | 0 |" in initial
    assert "---" in initial
    assert "- [ ]" not in initial and "- [x]" not in initial
    assert "```" not in initial
    assert "Overall" not in initial
    assert "To-do" not in initial
    assert "Agent plan" not in initial
    assert "☐" not in initial and "☑" not in initial
    assert initial.splitlines()[-1] == "__To cancel this search session, send `/stop`.__"

    # The card call site requests Markdown explicitly (run_turn_runner sets
    # rich_format="markdown" in the card metadata); this direct request mirrors
    # that path, so the card's inline `/stop` code span must not flip it to blocks.
    request = send_request(types.PeerUser(123), initial, rich_format="markdown")
    assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
    assert "## Search" in request.rich_message.markdown
    assert "| Measured activity | Count |" in request.rich_message.markdown
    assert "---" in request.rich_message.markdown
    assert "```" not in request.rich_message.markdown


@pytest.mark.asyncio
async def test_progress_reflects_real_search_and_page_read_tool_events():
    runner, adapter = _turn()
    await runner.start_search_progress()

    await _send_event(runner, "tool.started", "web_search", {"args": {"query": "Attock history"}})
    assert "**Current activity:** Searching the web." in adapter.edits[-1]["content"]

    search_result = {
        "success": True,
        "data": {"web": [
            {"url": "https://www.example.org/p1"},
            {"url": "https://news.example.com/a"},
            {"url": "https://example.org/p2"},
        ]},
    }
    await _send_event(runner, "tool.completed", "web_search", {"result": json.dumps(search_result)})
    assert "**Current activity:** Web search returned 3 result(s)." in adapter.edits[-1]["content"]

    extract_result = {
        "results": [
            {"url": "https://www.example.org/p1#top", "content": "Article text"},
            {"url": "https://news.example.com/a", "content": "Report text"},
            {"url": "https://blocked.example/", "error": "timeout", "content": ""},
        ],
    }
    await _send_event(runner, "tool.completed", "web_extract", {"result": json.dumps(extract_result)})
    live = adapter.edits[-1]["content"]
    assert "**Current activity:** Read 2 source page(s)." in live
    values = _table_values(live)
    assert values["Successful web searches"] == "1"
    assert values["Unique result URLs"] == "3"
    assert values["Unique websites found"] == "2"
    assert values["Unique websites read"] == "2"
    assert values["Source pages read"] == "2"

    before = len(adapter.edits)
    await _send_event(runner, "tool.completed", "todo_list", {
        "result": json.dumps({
            "revision": 1,
            "todos": [
                {"id": "sources", "content": "Search primary sources", "status": "completed"},
                {"id": "read", "content": "Read source pages", "status": "in_progress"},
                {"id": "synthesize", "content": "Synthesize cited answer", "status": "pending"},
                {"id": "optional", "content": "Optional route", "status": "cancelled"},
            ],
        })
    })
    assert len(adapter.edits) > before
    task_card = adapter.edits[-1]["content"]
    assert "- [x] **Completed** — Search primary sources" in task_card
    assert "- [ ] **In progress** — Read source pages" in task_card
    assert "- [ ] **Pending** — Synthesize cited answer" in task_card
    assert "- [ ] **Cancelled** — Optional route" in task_card
    task_values = _table_values(task_card)
    assert task_values["Agent tasks"] == "4"
    assert task_values["Tasks completed"] == "1"
    assert task_values["Tasks in progress"] == "1"
    assert task_values["Tasks pending"] == "1"
    assert task_values["Tasks cancelled"] == "1"
    native_request = send_request(types.PeerUser(123), task_card, rich_format="markdown")
    native_markdown = native_request.rich_message.markdown
    assert "- [x] **Completed** — Search primary sources\n- [ ] **In progress** — Read source pages" in native_markdown
    assert "| Measured activity | Count |\n|---|---:|" in native_markdown
    assert "## Agent tasks" in native_markdown and "## Measured activity" in native_markdown
    assert "---" in native_markdown
    assert "```" not in native_markdown
    assert "☐" not in native_markdown and "☑" not in native_markdown

    await _send_event(runner, "tool.completed", "todo_list", {
        "result": json.dumps({
            "revision": 0,
            "todos": [{"id": "stale", "content": "Stale task", "status": "completed"}],
        }),
    })
    assert adapter.edits[-1]["content"] == task_card
    assert "Stale task" not in adapter.edits[-1]["content"]

    await _send_event(runner, "tool.completed", "web_search", {
        "is_error": True, "result": json.dumps({"success": False}),
    })
    assert _table_values(adapter.edits[-1]["content"])["Successful web searches"] == "1"

    runner._search_progress_last_edit = time.monotonic() - 2
    result = {
        "model": "openai/gpt-4.1",
        "final_response": "See [source](https://example.org/report) and https://news.example.com/a for details.",
    }
    await runner.finish_search_progress(success=True, result=result)
    await _wait_for_latest_card(runner)
    final = adapter.edits[-1]["content"]
    assert "**Status: Complete**" in final
    assert "**Current activity:** The agent returned its final response." in final
    assert _table_values(final)["Source links in answer"] == "2"
    assert "**Model:** `openai/gpt-4.1`" in final
    assert "**Elapsed:**" in final
    assert "- [x] **Completed** — Search primary sources" in final
    assert "To cancel" not in final
    assert "```" not in final
    assert "☐" not in final and "☑" not in final
    assert len(adapter.sent) == 1


@pytest.mark.asyncio
async def test_failed_provider_event_is_truthful_and_terminal_state_is_cancelled():
    runner, adapter = _turn()
    await runner.start_search_progress()

    await _send_event(runner, "tool.started", "web_search", {})
    await _send_event(runner, "tool.completed", "web_search", {
        "result": json.dumps({"success": False, "error": "HTTP 403"}),
    })
    assert "**Current activity:** `web_search` returned an error; the agent is deciding how to continue." in adapter.edits[-1]["content"]
    assert _table_values(adapter.edits[-1]["content"])["Successful web searches"] == "0"

    await runner.finish_search_progress(success=False, result={"interrupted": True, "model": "openai/gpt-4.1"})
    await _wait_for_latest_card(runner)
    final = adapter.edits[-1]["content"]
    assert "**Status: Cancelled**" in final
    assert "**Current activity:** The agent run was interrupted; partial work is saved." in final
    assert "☐" not in final and "☑" not in final
    assert "Overall" not in final
    assert "**Model:** `openai/gpt-4.1`" in final


def test_search_card_is_telegram_only_but_search_guidance_is_scoped_to_search_requests():
    non_telegram, _ = _turn(platform=Platform.DISCORD)
    assert not non_telegram.search_progress_enabled
    assert "real multi-round web search" in non_telegram._combined_ephemeral_prompt()

    no_query, _ = _turn(message="/search")
    assert not no_query.search_progress_enabled
    assert "real multi-round web search" not in no_query._combined_ephemeral_prompt()

    ordinary, _ = _turn(message="Tell me about Attock")
    assert not ordinary.search_progress_enabled
    assert "real multi-round web search" not in ordinary._combined_ephemeral_prompt()


@pytest.mark.asyncio
async def test_terminal_progress_waits_for_post_delivery_callback():
    runner, adapter = _turn()
    await runner.start_search_progress()
    runner._ctx.session_key = "123:77"
    runner._ctx.run_generation = 12
    callbacks = []
    adapter.register_post_delivery_callback = lambda key, callback, *, generation=None: callbacks.append(
        (key, callback, generation)
    )

    await runner.finish_search_progress(success=True, result={"final_response": "Done."})
    assert len(callbacks) == 1
    assert callbacks[0][0] == "123:77"
    assert callbacks[0][2] == 12
    assert adapter.edits == []

    runner._search_progress_last_edit = time.monotonic() - 2
    await callbacks[0][1]()
    await _wait_for_latest_card(runner)
    assert "**Status: Complete**" in adapter.edits[-1]["content"]
    assert "**Current activity:** The agent returned its final response." in adapter.edits[-1]["content"]


@pytest.mark.asyncio
async def test_rendered_todo_card_survives_production_mtproto_send_and_edit():
    runner, progress_adapter = _turn()
    await runner.start_search_progress()
    await _send_event(runner, "tool.completed", "todo_list", {
        "result": json.dumps({
            "revision": 1,
            "todos": [
                {"id": "search", "content": "Search primary sources", "status": "in_progress"},
                {"id": "read", "content": "Read source pages", "status": "completed"},
            ],
        }),
    })
    card = progress_adapter.edits[-1]["content"]

    native_adapter = TelethonAdapter(PlatformConfig())
    client = CapturingRichClient()
    native_adapter._client = client

    async def _input_peer(_chat_id):
        return types.PeerUser(123)

    native_adapter._input_peer_for = _input_peer
    # Drive the production send/edit path the way the card call site does:
    # run_turn_runner puts rich_format="markdown" in the card metadata.
    card_metadata = {"rich_format": "markdown"}
    assert (await native_adapter.send("123", card, metadata=card_metadata)).success
    assert (await native_adapter.edit_message("123", "77", card, metadata=card_metadata)).success

    assert len(client.requests) == 2
    assert {type(request).__name__ for request in client.requests} == {"SendMessageRequest", "EditMessageRequest"}
    for request in client.requests:
        assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
        markdown = request.rich_message.markdown
        assert "## Search" in markdown
        assert "## Agent tasks" in markdown
        assert "- [ ] **In progress** — Search primary sources" in markdown
        assert "- [x] **Completed** — Read source pages" in markdown
        assert "| Measured activity | Count |\n|---|---:|" in markdown
        assert "---" in markdown
        assert "```" not in markdown
        assert "☐" not in markdown and "☑" not in markdown
