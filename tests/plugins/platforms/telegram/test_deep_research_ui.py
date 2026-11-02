import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from telethon import types

from gateway.deep_research_sessions import create_session, list_sessions, load_session, update_session, save_checkpoint
from plugins.platforms.telegram.deep_research_ui import TelegramDeepResearchUI


@dataclass(frozen=True)
class Source:
    chat_id: str = "42"
    user_id: str = "12345"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"


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
        self.documents = []
        self.events = []
        self.topic_calls = []
        self._post_delivery_callbacks = {}
        self._active_sessions = {}

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

    async def _create_topic_from_message(self, source, title, *, reply_to=None, welcome_text=None, random_id=None):
        self.topic_calls.append((source, title, reply_to, welcome_text, random_id))
        return "777"

    def _topic_welcome_message_id(self, chat_id, thread_id):
        return "778" if str(chat_id) == "42" and str(thread_id) == "777" else None

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))

    async def send_document(self, *args, **kwargs):
        self.documents.append((args, kwargs))
        return SimpleNamespace(success=True, message_id="501")

    def _event_session_key(self, event):
        return f"{event.source.chat_id}:{event.source.thread_id or 'root'}"

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self._post_delivery_callbacks[session_key] = callback

    async def finish_event_delivery(self, event):
        key = self._event_session_key(event)
        callback = self._post_delivery_callbacks.pop(key)
        await callback()

    async def handle_message(self, event):
        self.events.append(event)
        event._gateway_accepted = True


def _rich_text(request):
    rich = request.rich_message
    return getattr(rich, "html", None) or getattr(rich, "markdown", None) or getattr(request, "message", "")


def write_research_artifacts(home, user_id, session_id, *, sources_read=True):
    """Capture tool-read receipts and write matching notes and cited report artifacts."""
    from gateway.deep_research_sessions import resolve_session_dir, save_checkpoint

    urls = [
        "https://agency.example/reports/one",
        "https://university.example/paper/two",
    ]
    session_dir = resolve_session_dir(home, user_id, session_id)
    (session_dir / "research_notes.md").write_text(
        "Source notes\n- Agency finding. " + urls[0] + "\n- Independent study. " + urls[1] + "\n",
        encoding="utf-8",
    )
    report_path = session_dir / "report.md"
    report_path.write_text(
        "Findings support claim [1] and independently confirm it [2].\n\n"
        "## References\n[1] Agency report — " + urls[0] + "\n"
        "[2] University paper — " + urls[1] + "\n",
        encoding="utf-8",
    )
    # These URLs stand in for successful web-extract content receipts, not model-authored claims.
    save_checkpoint(
        home, user_id, session_id,
        todos=[{"id": "verified", "content": "Research and verify sources", "status": "completed", "elapsed_seconds": 12.0}],
        tool_history=[{"name": "web_extract", "success": True, "output": "Verified matching source text"}],
        research_evidence={
            "searches": 2,
            "result_urls": urls,
            "read_urls": urls if sources_read else [],
        },
    )
    return report_path


@pytest.mark.asyncio
async def test_deepresearch_help_is_scannable_native_rich_and_evidence_focused(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    assert await ui.handle_command(SimpleNamespace(source=Source()), Source(), "")

    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert isinstance(request.rich_message, types.InputRichMessage)
    rich_markdown = _rich_text(request)
    assert "## Deep research" in rich_markdown
    assert "| Item | Details |\n|---|---|" in rich_markdown
    assert "| **Start** |" in rich_markdown
    assert "| **Configuration** |" in rich_markdown
    assert "| **Session rules** |" in rich_markdown
    assert "| **Research workflow** |" in rich_markdown
    assert "| **Controls** |" in rich_markdown
    assert "| Max turns | 50, 100, 150, 300, 500 (default: 150) |" in rich_markdown
    assert "| Report format | Markdown, PDF, Word (default: Markdown) |" in rich_markdown
    assert "| Depth | Standard, Deep, Exhaustive (default: Deep) |" in rich_markdown
    assert "| `/deepresearch status` | List sessions. |" in rich_markdown
    assert "| Approval prompts | Disabled for the session's scoped work;" in rich_markdown
    assert "resume --accept-current" in rich_markdown
    assert "credible primary and authoritative sources" in rich_markdown
    assert "cross-check material claims against independent evidence" in rich_markdown
    assert "state evidence gaps, limitations, and uncertainty" in rich_markdown
    assert "/deepresearch status" in rich_markdown
    assert "__If a clarification is non-essential, Hermes states an assumption and continues.__" in rich_markdown
    assert len(rich_markdown) < 3000
    tables = [block for block in request.rich_message.blocks
              if isinstance(block, types.PageBlockTable)]
    assert len(tables) == 1
    section_heads = [row for row in tables[0].rows
                     if len(row.cells) == 1 and row.cells[0].header is True]
    assert len(section_heads) == 5
    assert all(row.cells[0].colspan == 2 for row in section_heads)


@pytest.mark.asyncio
async def test_config_card_settings_render_as_a_native_list(tmp_path):
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    assert await ui.handle_command(SimpleNamespace(source=Source()), Source(),
                                   "Why is the sky blue?")

    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert isinstance(request.rich_message, types.InputRichMessage)
    blocks = request.rich_message.blocks
    lists = [block for block in blocks if isinstance(block, types.PageBlockList)]
    assert len(lists) == 1
    assert len(lists[0].items) == 4
    assert any(isinstance(block, types.PageBlockPreformatted) for block in blocks)
    assert any(isinstance(block, types.PageBlockButtonRow) for block in blocks)
    assert "• Max turns" not in _rich_text(request)
    assert "- Max turns: **150**" in _rich_text(request)


@pytest.mark.asyncio
async def test_deepresearch_status_with_id_inspects_session_not_new_query(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(tmp_path, source.user_id, "Existing research", chat_id=source.chat_id, chat_type="dm")
    update_session(tmp_path, source.user_id, record["session_id"], status="paused")

    assert await ui.handle_command(SimpleNamespace(message_id="88"), source, f"status {record['session_id']}", reply_to="88")

    assert not ui._pending
    assert "Deep research sessions" in _rich_text(adapter.requests[0])
    assert record["session_id"] in _rich_text(adapter.requests[0])
    assert "Start deep research?" not in _rich_text(adapter.requests[0])
    assert not adapter.events


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnected", [False, True])
async def test_deepresearch_rich_card_never_falls_back_to_plain_text(tmp_path, disconnected):
    adapter = FakeAdapter(tmp_path)
    if disconnected:
        adapter._client = None
    else:
        async def fail_rich_request(_request):
            raise RuntimeError("native Rich Message send failed")

        adapter._mtproto_rich_request = fail_rich_request
    ui = TelegramDeepResearchUI(adapter)

    assert await ui.handle_command(SimpleNamespace(source=Source()), Source(), "")

    assert adapter.messages == []


@pytest.mark.asyncio
async def test_deepresearch_requires_confirmation_then_runs_in_created_topic(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    command_event = SimpleNamespace(message_id="88")

    assert await ui.handle_command(command_event, source, "Attock history and trade routes", reply_to="88")
    assert len(adapter.requests) == 1
    assert len(ui._pending) == 1
    assert not adapter.events
    token, pending = next(iter(ui._pending.items()))
    assert pending["query"] == "Attock history and trade routes"

    click = CallbackEvent(source, int(pending["message_id"]))
    await ui.handle_callback(click, f"dr:{token}:start")
    assert not ui._pending
    await __import__("asyncio").sleep(0.05)

    sessions = list_sessions(tmp_path, source.user_id)
    assert len(sessions) == 1
    assert sessions[0]["status"] == "running"
    assert sessions[0]["thread_id"] == "777"
    assert sessions[0]["max_turns"] == 150
    assert sessions[0]["shell_access"] is True
    assert len(adapter.events) == 1
    run_event = adapter.events[0]
    assert run_event.source.thread_id == "777"
    assert run_event.message_id == "778"
    assert run_event.metadata["deep_research"]["max_turns"] == 150
    assert run_event.metadata["gateway_session_key"] == adapter._event_session_key(run_event)
    assert adapter.topic_calls[0][1].startswith("Research: ")
    assert "investigation will start here" in adapter.topic_calls[0][3].lower()
    assert "continue in this thread" not in adapter.topic_calls[0][3].lower()
    assert adapter.messages[-1][0][0] == source.chat_id
    assert adapter.messages[-1][1]["metadata"]["thread_id"] == "777"
    assert adapter.messages[-1][1]["metadata"]["telegram_send_in_topic"] is True
    assert len(adapter.requests) >= 2  # confirmation card and its one-message edit

    report_path = write_research_artifacts(tmp_path, source.user_id, sessions[0]["session_id"])
    await adapter.finish_event_delivery(run_event)
    sessions = list_sessions(tmp_path, source.user_id)
    assert sessions[0]["status"] == "completed"
    assert sessions[0]["artifact_status"] == "ready"
    assert sessions[0]["final_response_status"] == "delivered"
    assert len(adapter.documents) == 1
    assert adapter.documents[0][0][0] == source.chat_id
    assert adapter.documents[0][0][1] == str(report_path)
    assert adapter.documents[0][1]["caption"] == "Deep research report"
    assert adapter.documents[0][1]["metadata"]["thread_id"] == "777"
    assert adapter.documents[0][1]["metadata"]["deep_research_content"] is True

    duplicate = CallbackEvent(source, int(pending["message_id"]))
    await ui.handle_callback(duplicate, f"dr:{token}:start")
    assert len(list_sessions(tmp_path, source.user_id)) == 1
    assert duplicate.answers and duplicate.answers[-1][1].get("alert") is True


@pytest.mark.asyncio
async def test_deepresearch_start_failure_is_reported_inside_created_topic(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    async def fail_in_topic(event):
        adapter.events.append(event)
        raise RuntimeError("provider unavailable")

    adapter.handle_message = fail_in_topic
    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Find primary sources",
        "message_id": "88",
    })

    assert adapter.events[0].source.thread_id == "777"
    assert adapter.messages[-1][1]["metadata"]["thread_id"] == "777"
    assert adapter.messages[-1][1]["metadata"]["telegram_strict_thread"] is True
    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"


@pytest.mark.asyncio
async def test_deepresearch_stop_from_root_routes_to_the_only_active_topic(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    record = create_session(tmp_path, source.user_id, "ongoing work", chat_id=source.chat_id,
                            chat_type="dm", thread_id="777")
    update_session(tmp_path, source.user_id, record["session_id"], status="running")
    ui = TelegramDeepResearchUI(adapter)

    assert await ui.handle_command(SimpleNamespace(), source, "stop")
    assert len(adapter.events) == 1
    assert adapter.events[0].text == "/stop"
    assert adapter.events[0].source.thread_id == "777"


@pytest.mark.asyncio
async def test_deepresearch_missing_report_is_failed_and_not_fabricated(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Find primary sources",
        "message_id": "88",
    })
    run_event = adapter.events[0]
    assert list_sessions(tmp_path, source.user_id)[0]["status"] == "running"

    await adapter.finish_event_delivery(run_event)
    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"
    assert "report.md" in session["last_error"]
    assert adapter.documents == []
    assert "report is not yet available" in adapter.messages[-1][0][1]


@pytest.mark.asyncio
async def test_deepresearch_report_symlink_cannot_escape_private_session(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Find primary sources",
        "message_id": "88",
    })
    run_event = adapter.events[0]

    from gateway.deep_research_sessions import resolve_session_dir
    session_dir = resolve_session_dir(tmp_path, source.user_id, list_sessions(tmp_path, source.user_id)[0]["session_id"])
    outside_report = tmp_path / "outside-report.md"
    outside_report.write_text("must not be sent", encoding="utf-8")
    (session_dir / "report.md").symlink_to(outside_report)

    await adapter.finish_event_delivery(run_event)
    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"
    assert "report.md" in session["last_error"]
    assert adapter.documents == []


@pytest.mark.asyncio
async def test_deepresearch_document_failure_is_reported_without_fallback(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    async def fail_upload(*_args, **_kwargs):
        raise RuntimeError("MTProto upload unavailable")

    adapter.send_document = fail_upload
    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Find primary sources",
        "message_id": "88",
    })
    run_event = adapter.events[0]
    session = list_sessions(tmp_path, source.user_id)[0]
    write_research_artifacts(tmp_path, source.user_id, session["session_id"])

    await adapter.finish_event_delivery(run_event)
    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"
    assert "could not confirm" in session["last_error"].lower()
    assert len(adapter.messages) >= 2
    assert "no research content was sent outside the topic" in adapter.messages[-1][0][1].lower()
    assert session["artifact_status"] == "ready"
    assert session["final_response_status"] == "failed"


@pytest.mark.asyncio
async def test_deepresearch_search_without_source_reads_pauses_and_offers_resume(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Find primary sources",
        "message_id": "88",
    })
    run_event = adapter.events[0]
    session = list_sessions(tmp_path, source.user_id)[0]
    write_research_artifacts(tmp_path, source.user_id, session["session_id"], sources_read=False)

    await adapter.finish_event_delivery(run_event)

    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"
    assert session["failure_class"] == "insufficient_research_evidence"
    assert "successfully read source pages" in session["last_error"]
    assert adapter.documents == []
    assert any("/deepresearch resume" in message[0][1] for message in adapter.messages)
    assert "status" in adapter.messages[-1][0][1].lower()


@pytest.mark.asyncio
async def test_deepresearch_resume_without_id_uses_paused_session_in_current_chat(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    selected = create_session(
        tmp_path, source.user_id, "Current-chat research", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    update_session(tmp_path, source.user_id, selected["session_id"], status="paused",
                   latest_reply_message_id="700", last_error="Provider quota issue")
    save_checkpoint(tmp_path, source.user_id, selected["session_id"], last_error="Provider quota issue")
    other_chat = create_session(
        tmp_path, source.user_id, "Other-chat research", chat_id="99",
        chat_type="group", thread_id="888",
    )
    update_session(tmp_path, source.user_id, other_chat["session_id"], status="paused",
                   latest_reply_message_id="800", last_error="Other chat")

    await ui.handle_command(SimpleNamespace(message_id="500"), source, "resume", reply_to="500")
    for _ in range(30):
        if adapter.events:
            break
        await asyncio.sleep(0.01)

    assert len(adapter.events) == 1
    resumed = adapter.events[0]
    assert resumed.source.thread_id == "777"
    assert resumed.message_id == "700"
    assert resumed.metadata["deep_research"]["session_id"] == selected["session_id"]
    assert load_session(tmp_path, source.user_id, selected["session_id"])["status"] == "running"
    assert load_session(tmp_path, source.user_id, other_chat["session_id"])["status"] == "paused"


@pytest.mark.asyncio
async def test_concurrent_deepresearch_resume_commands_launch_only_once(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Concurrent resume", chat_id=source.chat_id,
        chat_type="group", thread_id="777",
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="paused",
                   latest_reply_message_id="700")
    save_checkpoint(tmp_path, source.user_id, record["session_id"])

    await asyncio.gather(
        ui.handle_command(SimpleNamespace(message_id="501"), source, f"resume {record['session_id']}", reply_to="501"),
        ui.handle_command(SimpleNamespace(message_id="502"), source, f"resume {record['session_id']}", reply_to="502"),
    )
    for _ in range(30):
        if adapter.events:
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)

    assert len(adapter.events) == 1
    assert load_session(tmp_path, source.user_id, record["session_id"])["status"] == "running"


@pytest.mark.asyncio
async def test_deepresearch_status_marks_stale_running_interrupted_and_resumes_on_action(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Interrupted research", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="running",
                   latest_reply_message_id="700")

    await ui._show_sessions(source, reply_to="900")
    interrupted = load_session(tmp_path, source.user_id, record["session_id"])
    assert interrupted["status"] == "interrupted"
    token, pending = next(iter(ui._resume_actions.items()))
    assert pending["session_id"] == record["session_id"]
    click = CallbackEvent(source, pending["message_id"])
    await ui.handle_callback(click, f"dr:{token}:resume")
    for _ in range(30):
        if adapter.events:
            break
        await asyncio.sleep(0.01)

    assert click.answers[0][0] == "Resuming deep research…"
    assert len(adapter.events) == 1
    assert adapter.events[0].source.thread_id == "777"
    assert adapter.events[0].message_id == "700"
    assert load_session(tmp_path, source.user_id, record["session_id"])["status"] == "running"


@pytest.mark.asyncio
async def test_deepresearch_paused_provider_failure_notifies_topic_and_offers_resume(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source(thread_id="777", chat_type="group")
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Provider failure", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="paused",
                   failure_class="provider_authentication",
                   last_error="Refresh provider credentials, then resume this session.",
                   latest_reply_message_id="700")
    run_event = SimpleNamespace(
        source=source,
        metadata={"deep_research": {"session_id": record["session_id"], "latest_reply_message_id": "700"}},
        _gateway_accepted=True,
    )

    await ui._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=record["session_id"],
        source=source, run_event=run_event, session_key="", reply_to="700",
    )

    assert load_session(tmp_path, source.user_id, record["session_id"])["failure_class"] == "provider_authentication"
    assert len(adapter.messages) == 2
    topic_message = adapter.messages[0]
    assert "/deepresearch resume" in topic_message[0][1]
    assert record["session_id"] in topic_message[0][1]
    assert topic_message[1]["metadata"]["thread_id"] == "777"
    assert topic_message[1]["metadata"]["telegram_send_in_topic"] is True
    assert adapter.messages[-1][1]["metadata"]["thread_id"] == "777"
    assert adapter.messages[-1][1]["reply_to"] == "777"
    assert "no research content was sent outside the topic" in adapter.messages[-1][0][1].lower()
    assert load_session(tmp_path, source.user_id, record["session_id"])["final_response_status"] == "failed"


@pytest.mark.asyncio
async def test_deepresearch_pause_already_published_as_final_response_sends_no_second_notice(tmp_path):
    """The runner publishes a terminal pause as the turn's final response;
    the post-delivery callback must not deliver the same pause a second
    time (observed live: two pause messages one second apart)."""
    from gateway.deep_research_sessions import load_session

    source = Source(thread_id="777", chat_type="group")
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Evidence failure", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="paused",
                   failure_class="insufficient_research_evidence",
                   last_error="The final response did not meet the source-evidence checks.",
                   latest_reply_message_id="700")
    run_event = SimpleNamespace(
        source=source,
        metadata={"deep_research": {"session_id": record["session_id"], "latest_reply_message_id": "700"},
                  "deep_research_terminal_pause_published": True},
        _gateway_accepted=True,
    )

    await ui._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=record["session_id"],
        source=source, run_event=run_event, session_key="", reply_to="700",
    )

    assert adapter.messages == []
    assert load_session(tmp_path, source.user_id, record["session_id"])["status"] == "paused"


@pytest.mark.asyncio
async def test_deepresearch_mismatched_source_notice_uses_saved_topic_not_trigger(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source(thread_id="999", chat_type="group")
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Saved route", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    run_event = SimpleNamespace(
        source=source,
        metadata={"deep_research": {"session_id": record["session_id"], "latest_reply_message_id": "88"}},
        _gateway_accepted=True,
    )

    await ui._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=record["session_id"],
        source=source, run_event=run_event, session_key="", reply_to="88",
    )

    assert load_session(tmp_path, source.user_id, record["session_id"])["failure_class"] == "topic_identity_mismatch"
    assert len(adapter.messages) == 1
    args, kwargs = adapter.messages[0]
    assert args[0] == record["chat_id"]
    assert kwargs["metadata"]["thread_id"] == record["thread_id"] == "777"
    assert kwargs["metadata"]["telegram_send_in_topic"] is True
    assert kwargs["reply_to"] != record["trigger_message_id"]


@pytest.mark.asyncio
async def test_deepresearch_status_discloses_saved_artifact_but_failed_delivery(tmp_path):
    source = Source(thread_id="777", chat_type="group")
    adapter = FakeAdapter(tmp_path)
    record = create_session(
        tmp_path, source.user_id, "Saved but not delivered", chat_id=source.chat_id,
        chat_type="group", thread_id="777",
    )
    update_session(
        tmp_path, source.user_id, record["session_id"], status="paused",
        artifact_status="ready", final_response_status="failed",
        last_error="The final report could not be delivered.",
    )

    await TelegramDeepResearchUI(adapter)._show_sessions(source)

    card = _rich_text(adapter.requests[-1])
    assert "Artifact: ready" in card
    assert "Final delivery: failed" in card


@pytest.mark.asyncio
async def test_deepresearch_report_without_original_topic_never_sends_to_parent(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source(thread_id=None)
    adapter = FakeAdapter(tmp_path)
    record = create_session(
        tmp_path, source.user_id, "Private topic-only report", chat_id=source.chat_id,
        chat_type="group", thread_id=None,
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="running")
    write_research_artifacts(tmp_path, source.user_id, record["session_id"])
    run_event = SimpleNamespace(
        source=source,
        metadata={"deep_research": {"session_id": record["session_id"]}},
        _gateway_accepted=True,
    )

    await TelegramDeepResearchUI(adapter)._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=record["session_id"],
        source=source, run_event=run_event, session_key="", reply_to="88",
    )

    assert adapter.documents == []
    assert not any("Findings support claim" in call[0][1] for call in adapter.messages)
    saved = load_session(tmp_path, source.user_id, record["session_id"])
    assert saved["status"] == "paused"
    assert saved["final_response_status"] == "failed"


@pytest.mark.asyncio
async def test_deepresearch_exhausted_topic_send_pauses_without_attaching_report(tmp_path):
    from gateway.deep_research_sessions import load_session

    source = Source(thread_id="777", chat_type="group")
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    record = create_session(
        tmp_path, source.user_id, "Topic delivery failure", chat_id=source.chat_id,
        chat_type="group", thread_id="777", trigger_message_id="88",
    )
    update_session(tmp_path, source.user_id, record["session_id"], status="running",
                   latest_reply_message_id="700")
    run_event = SimpleNamespace(
        source=source,
        metadata={
            "deep_research": {"session_id": record["session_id"], "latest_reply_message_id": "700"},
            "telegram_task_delivery_failure": "Telegram MTProto send failed: ConnectionError",
        },
        _gateway_accepted=True,
    )

    await ui._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=record["session_id"],
        source=source, run_event=run_event, session_key="", reply_to="700",
    )

    session = load_session(tmp_path, source.user_id, record["session_id"])
    assert session["status"] == "paused"
    assert session["failure_class"] == "topic_delivery_exhausted"
    assert not adapter.documents
    assert "/deepresearch resume" in adapter.messages[0][0][1]
    assert "no research content was sent outside the topic" in adapter.messages[-1][0][1].lower()


@pytest.mark.asyncio
async def test_private_bot_topic_failure_explains_threaded_mode_before_research(tmp_path):
    source = Source(chat_type="dm")
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)

    async def fail_topic(_source, _title, **_kwargs):
        adapter._topic_creation_error = (
            "Telegram rejected private-topic creation. Enable Threaded Mode for the bot in BotFather "
            "and allow it to create/send messages in this chat."
        )

    adapter._create_topic_from_message = fail_topic
    await ui._start_session({
        "source": source, "user_id": source.user_id, "query": "Private bot research",
        "message_id": "88", "trigger_message_id": "87",
    })

    assert adapter.events == []
    assert any("BotFather" in message[0][1] and "Threaded Mode" in message[0][1]
               for message in adapter.messages)
    session = list_sessions(tmp_path, source.user_id)[0]
    assert session["status"] == "paused"
    assert session["failure_class"] == "topic_creation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["malformed", "missing", "other_chat", "other_owner"])
async def test_status_lookup_never_starts_research_or_leaks_another_scope(tmp_path, case):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    if case == "malformed":
        session_id = "not-a-session-id"
    elif case == "missing":
        session_id = "a" * 12
    else:
        record = create_session(
            tmp_path, "98765" if case == "other_owner" else source.user_id,
            "Never expose this private question", chat_id="99" if case == "other_chat" else source.chat_id,
            chat_type="dm")
        session_id = record["session_id"]
    await ui.handle_command(SimpleNamespace(message_id="88"), source, f"status {session_id}", reply_to="88")
    assert not ui._pending
    assert not adapter.events
    card = _rich_text(adapter.requests[-1])
    assert "Never expose this private question" not in card
    assert "Start deep research?" not in card


@pytest.mark.asyncio
async def test_resume_refuses_mismatched_draft_without_claim_or_rewrite(tmp_path):
    from gateway.deep_research_sessions import resolve_session_dir, load_session
    source = Source()
    adapter = FakeAdapter(tmp_path)
    record = create_session(tmp_path, source.user_id, "Private question", chat_id=source.chat_id,
                            chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, source.user_id, sid, status="paused", latest_reply_message_id="700")
    write_research_artifacts(tmp_path, source.user_id, sid)
    directory = resolve_session_dir(tmp_path, source.user_id, sid)
    (directory / "report.md").write_text("Uncheckpointed draft")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    ui = TelegramDeepResearchUI(adapter)
    await ui._resume_session(source, sid, trigger_message_id="90")
    assert not adapter.events
    assert load_session(tmp_path, source.user_id, sid)["status"] == "paused"
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    # The refusal is delivered as a rich card in the original topic (with the
    # owner-reconcile button), not as a plain message.
    assert not adapter.messages
    assert len(adapter.requests) == 1
    card = _rich_text(adapter.requests[0])
    assert "report.md differs" in card
    assert "tell me how to reconcile" in card
    assert "Accept current files" in card or "accept-current" in card
    assert adapter.requests[0].reply_to.reply_to_msg_id == 777
    assert len(ui._resume_actions) == 1
    assert next(iter(ui._resume_actions.values()))["accept_artifact_drift"] is True


@pytest.mark.asyncio
async def test_resume_passes_all_saved_evidence_and_artifacts_to_runner(tmp_path):
    source = Source()
    adapter = FakeAdapter(tmp_path)
    record = create_session(tmp_path, source.user_id, "Original question", chat_id=source.chat_id,
                            chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, source.user_id, sid, status="paused", latest_reply_message_id="700", progress_message_id="701")
    write_research_artifacts(tmp_path, source.user_id, sid)
    save_checkpoint(tmp_path, source.user_id, sid,
                    research_evidence={"searches": 21}, last_error="Prior timeout",
                    tool_history=[{"name": "web_search", "success": True, "output": "Recorded evidence"}])
    await TelegramDeepResearchUI(adapter)._resume_session(source, sid, trigger_message_id="90")
    event = adapter.events[0]
    research = event.metadata["deep_research"]
    checkpoint = research["checkpoint"]
    assert checkpoint["research_evidence"]["searches"] == 21
    assert __import__("json").dumps(checkpoint["partial_report"]["content"], ensure_ascii=False) in event.text
    assert "Recorded evidence" in event.text
    assert "Prior timeout" in event.text
    assert "Original question" in event.text
    assert research["progress_message_id"] == "701"
    assert checkpoint["todos"][0]["elapsed_seconds"] == 12.0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "in_progress"])
async def test_ready_cited_report_never_delivered_with_unfinished_tasks(tmp_path, status):
    from gateway.deep_research_sessions import load_session
    source = Source(thread_id="777")
    adapter = FakeAdapter(tmp_path)
    record = create_session(tmp_path, source.user_id, "Question", chat_id=source.chat_id,
                            chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, source.user_id, sid, status="running")
    write_research_artifacts(tmp_path, source.user_id, sid)
    save_checkpoint(tmp_path, source.user_id, sid, todos=[{"id": "remaining", "content": "Unfinished work", "status": status}])
    event = SimpleNamespace(metadata={"deep_research": {"session_id": sid}}, _gateway_accepted=True)
    await TelegramDeepResearchUI(adapter)._complete_session_after_turn(
        home=tmp_path, user_id=source.user_id, session_id=sid,
        source=source, run_event=event, session_key="", reply_to=None)
    assert not adapter.documents
    saved = load_session(tmp_path, source.user_id, sid)
    assert saved["status"] == "paused"
    assert saved["final_response_status"] == "failed"
    assert "unfinished tasks" in saved["last_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [Source(thread_id="888"), Source(thread_id=None),
                                    Source(chat_id="99", thread_id="777"),
                                    Source(user_id="99999", thread_id="777")])
async def test_final_delivery_refuses_any_saved_topic_owner_or_chat_mismatch(tmp_path, source):
    from gateway.deep_research_sessions import load_session
    adapter = FakeAdapter(tmp_path)
    record = create_session(tmp_path, "12345", "Private question", chat_id="42", chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, "12345", sid, status="running")
    write_research_artifacts(tmp_path, "12345", sid)
    event = SimpleNamespace(metadata={"deep_research": {"session_id": sid}}, _gateway_accepted=True)
    await TelegramDeepResearchUI(adapter)._complete_session_after_turn(
        home=tmp_path, user_id="12345", session_id=sid, source=source,
        run_event=event, session_key="", reply_to=None)
    assert not adapter.documents
    assert len(adapter.messages) == 1
    assert adapter.messages[0][0][0] == "42"
    assert "Private question" not in adapter.messages[0][0][1]
    saved = load_session(tmp_path, "12345", sid)
    assert saved["status"] == "paused"
    assert saved["failure_class"] == "topic_identity_mismatch"
    assert saved["thread_id"] == "777"


def test_instructions_arg_parsing_and_config_cycling():
    from plugins.platforms.telegram.deep_research_ui import (
        _MAX_TURN_OPTIONS,
        _cycle,
        _parse_instructions_arg,
    )

    assert _parse_instructions_arg("Why is the sky blue?") == ("Why is the sky blue?", "")
    assert _parse_instructions_arg('Explain tides --instructions "focus on the moon"') == (
        "Explain tides", "focus on the moon",
    )
    assert _parse_instructions_arg("Explain tides --instructions focus on the moon and sun") == (
        "Explain tides", "focus on the moon and sun",
    )
    assert _parse_instructions_arg("--instructions only instructions") == ("", "only instructions")
    assert _cycle(150, _MAX_TURN_OPTIONS) == 300
    assert _cycle(500, _MAX_TURN_OPTIONS) == 50
    assert _cycle(999, _MAX_TURN_OPTIONS) == 50


@pytest.mark.asyncio
async def test_config_card_buttons_cycle_settings_and_reedit_card(tmp_path):
    from plugins.platforms.telegram.deep_research_ui import TelegramDeepResearchUI, _default_config

    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    token = "abcdef123456"
    source = Source()
    ui._pending[token] = {
        "user_id": source.user_id, "chat_id": source.chat_id, "message_id": "55",
        "query": "Why is the sky blue?", "config": _default_config(),
    }
    event = CallbackEvent(source, "55")

    await ui.handle_callback(event, f"dr:{token}:cfg:max_turns")
    assert ui._pending[token]["config"]["max_turns"] == 300
    assert event.answers[0][0] == "Max turns: 300"
    assert len(adapter.requests) == 1
    assert "Max turns: **300**" in _rich_text(adapter.requests[0])
    markup = adapter.requests[0].reply_markup
    callback_data = [
        bytes(button.type.data)
        for row in markup.rows for button in row.buttons
        if getattr(getattr(button, "type", None), "data", None) is not None
    ]
    assert b"dr:abcdef123456:cfg:report_format" in callback_data
    assert b"dr:abcdef123456:start" in callback_data

    await ui.handle_callback(event, f"dr:{token}:cfg:report_format")
    assert ui._pending[token]["config"]["report_format"] == "pdf"
    await ui.handle_callback(event, f"dr:{token}:cfg:depth")
    assert ui._pending[token]["config"]["depth"] == "exhaustive"


@pytest.mark.asyncio
async def test_launch_watchdog_pauses_swallowed_run_with_route_reason(tmp_path, monkeypatch):
    import plugins.platforms.telegram.deep_research_ui as ui_module
    from plugins.platforms.telegram.deep_research_ui import TelegramDeepResearchUI

    monkeypatch.setattr(ui_module, "_LAUNCH_WATCH_GRACE_SECONDS", 0)
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    source = Source()
    record = create_session(tmp_path, source.user_id, "Swallowed research", chat_id=source.chat_id, chat_type="dm", thread_id="777")
    run_event = SimpleNamespace(_gateway_accepted=False)
    await ui._watch_launch(
        source=source, record=record, run_event=run_event,
        session_key="research-key", confirmation_card_id="55",
    )
    current = load_session(tmp_path, source.user_id, record["session_id"])
    assert current["status"] == "paused"
    assert current["failure_class"] == "launch_never_started"
    assert "did not admit" in current["last_error"]
    assert adapter.messages, "the watchdog must notify the topic loudly"
    assert f"resume {record['session_id']}" in adapter.messages[0][0][1]


@pytest.mark.asyncio
async def test_launch_watchdog_leaves_live_or_progressed_runs_alone(tmp_path, monkeypatch):
    import asyncio as _asyncio

    import plugins.platforms.telegram.deep_research_ui as ui_module
    from plugins.platforms.telegram.deep_research_ui import TelegramDeepResearchUI

    monkeypatch.setattr(ui_module, "_LAUNCH_WATCH_GRACE_SECONDS", 0)
    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    source = Source()

    record = create_session(tmp_path, source.user_id, "Live research", chat_id=source.chat_id, chat_type="dm", thread_id="777")
    guard = _asyncio.Event()
    adapter._active_sessions["research-key"] = guard
    await ui._watch_launch(
        source=source, record=record, run_event=SimpleNamespace(_gateway_accepted=True),
        session_key="research-key", confirmation_card_id="55",
    )
    assert load_session(tmp_path, source.user_id, record["session_id"])["status"] == "starting"
    assert not adapter.messages

    record2 = create_session(tmp_path, source.user_id, "Progressed research", chat_id=source.chat_id, chat_type="dm", thread_id="778")
    update_session(tmp_path, source.user_id, record2["session_id"], status="running", progress_message_id="42")
    await ui._watch_launch(
        source=source, record=record2, run_event=SimpleNamespace(_gateway_accepted=True),
        session_key="other-key", confirmation_card_id="55",
    )
    assert load_session(tmp_path, source.user_id, record2["session_id"])["status"] == "running"
    assert not adapter.messages


def _hard_killed_session(tmp_path, *, with_mid_run_checkpoint):
    """A session whose agent wrote past its last checkpoint, then died.

    Mirrors repro B2: launch, mid-run checkpoint (when asked), agent writes
    newer notes/report, no finish_search_progress ever runs.
    """
    from gateway.deep_research_sessions import resolve_session_dir, update_checkpoint_metadata

    source = Source(thread_id="777")
    record = create_session(tmp_path, source.user_id, "What happened at Attock?",
                            chat_id=source.chat_id, chat_type="dm", thread_id="777")
    sid = record["session_id"]
    update_session(tmp_path, source.user_id, sid, status="running", latest_reply_message_id="88")
    update_checkpoint_metadata(tmp_path, source.user_id, sid, latest_reply_message_id="88")
    directory = resolve_session_dir(tmp_path, source.user_id, sid)
    notes_v1 = "## Ledger\n- https://a.example/x — A\n- https://b.example/y — B\n"
    report_v1 = "# Report\nFindings [1] [2].\n\n## References\n- [1] A https://a.example/x\n- [2] B https://b.example/y\n"
    (directory / "research_notes.md").write_text(notes_v1, encoding="utf-8")
    (directory / "report.md").write_text(report_v1, encoding="utf-8")
    if with_mid_run_checkpoint:
        save_checkpoint(
            tmp_path, source.user_id, sid, status="running",
            todos=[{"id": "dr-scope", "content": "Scope the question", "status": "completed"},
                   {"id": "dr-search", "content": "Search broadly", "status": "in_progress"}],
            research_evidence={
                "searches": 3,
                "result_urls": ["https://a.example/x", "https://b.example/y"],
                "read_urls": ["https://a.example/x", "https://b.example/y"],
                "read_sources": [{"url": "https://a.example/x", "host": "a.example", "title": "A"},
                                 {"url": "https://b.example/y", "host": "b.example", "title": "B"}],
            },
            tool_history=[{"name": "web_search", "success": True, "output_excerpt": "ok"}],
        )
        (directory / "research_notes.md").write_text(notes_v1 + "- https://c.example/z — C\n", encoding="utf-8")
        (directory / "report.md").write_text(report_v1 + "\nMore findings [3].\n", encoding="utf-8")
    return source, sid


def test_reconcile_stale_running_refreshes_snapshots_so_hard_kill_resumes(tmp_path):
    """Hard kill after the agent wrote past the last checkpoint must resume.

    The reconcile used to sync only status, leaving the checkpoint's artifact
    snapshots stale, so validate_resume_state refused the resume forever.
    """
    from gateway.deep_research_sessions import (
        claim_session_for_resume, validate_resume_state,
    )

    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    source, sid = _hard_killed_session(tmp_path, with_mid_run_checkpoint=True)
    with pytest.raises(ValueError, match="differs from its checkpoint snapshot"):
        validate_resume_state(tmp_path, source.user_id, sid)

    ui._reconcile_stale_running(source, tmp_path)

    assert load_session(tmp_path, source.user_id, sid)["status"] == "interrupted"
    checkpoint = validate_resume_state(tmp_path, source.user_id, sid)
    assert "https://c.example/z" in checkpoint["source_ledger"]["content"]
    assert "More findings [3]" in checkpoint["partial_report"]["content"]
    claimed = claim_session_for_resume(tmp_path, source.user_id, sid)
    assert claimed is not None and claimed["status"] == "starting"


def test_reconcile_stale_running_keeps_disputed_task_ledger_blocked(tmp_path):
    """Refreshing snapshots must not launder a genuinely disputed checkpoint."""
    import json

    from gateway.deep_research_sessions import validate_resume_state

    adapter = FakeAdapter(tmp_path)
    ui = TelegramDeepResearchUI(adapter)
    source, sid = _hard_killed_session(tmp_path, with_mid_run_checkpoint=True)
    from gateway.deep_research_sessions import resolve_session_dir
    checkpoint_path = resolve_session_dir(tmp_path, source.user_id, sid) / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["todos"] = [{"id": "dr-scope", "content": "Scope the question"}]  # no status: invalid
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    ui._reconcile_stale_running(source, tmp_path)

    assert load_session(tmp_path, source.user_id, sid)["status"] == "interrupted"
    with pytest.raises(ValueError, match="task ledger contains an invalid task"):
        validate_resume_state(tmp_path, source.user_id, sid)
