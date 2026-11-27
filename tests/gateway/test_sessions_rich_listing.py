"""Rich /sessions + /resume listings: collapsibles, real italics, 10 per page.

Invariant tests, written red-first: the preview renders in real ``<i>`` italics
(never literal ``_..._`` markers — the card path on main had no italic handling,
so ``_preview_`` printed literally on the live bot); each session is one
``<details>`` collapsible with Resume/Rename callback buttons inside; the
current session gets a Current pill before its collapsible; a single page has
no divider and no page indexation; more than one page adds both plus Prev/Next;
rename is a real rename through the pending-reply intercept, guarded by the
same IDOR check as /resume.
"""

import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from plugins.platforms.telegram.command_cards import sessions_listing_html


def _rows(n):
    return [{"id": f"sess-{i}", "title": f"Title {i}", "preview": f"preview {i}"}
            for i in range(n)]


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester",
        chat_type="dm",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_db = AsyncMock()
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:u1"
    runner._resume_access_denied_reply = AsyncMock(return_value=None)
    runner._schedule_telegram_topic_title_rename = lambda *a: None
    return runner


def _callback_adapter():
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = object.__new__(TelethonAdapter)
    adapter.gateway_runner = SimpleNamespace()
    adapter._send_rich_ui = AsyncMock()
    adapter._peer_for = lambda chat_id: chat_id
    adapter._mtproto_rich_request = AsyncMock()
    return adapter


# --- builder: italics ---------------------------------------------------------

def test_preview_renders_real_italics_not_literal_markers():
    html = sessions_listing_html([{"id": "s1", "title": "Alpha", "preview": "hello world"}])
    assert "<i>hello world</i>" in html
    assert "_hello world_" not in html


def test_notice_renders_real_italics_not_literal_markers():
    # Red on main: the admin-scope note carries Markdown `_..._` and backticks,
    # and sessions_listing_html escaped it raw inside <i> — so the note printed
    # literally ("_Note: `all` ... only._") under real italics.
    notice = "_Note: `all` (cross-chat listing) requires a configured admin; showing this chat's sessions only._"
    html = sessions_listing_html([{"id": "s1", "title": "Alpha", "preview": "hello world"}], notice=notice)
    assert "<i>Note:" in html
    assert "_Note:" not in html
    assert "`all`" not in html
    assert "only._" not in html
    assert "<code>all</code>" in html


def test_lines_to_rich_html_renders_italic_spans():
    # Red on main: the card path had no italic handling, so _hello_ printed literally.
    from plugins.platforms.telegram.command_cards import lines_to_rich_html

    html = lines_to_rich_html("Sessions", ["_hello_"])
    assert "<i>hello</i>" in html


def test_values_are_escaped_not_injected():
    html = sessions_listing_html([{"id": "s1", "title": "<b>Bold</b>", "preview": "<i>x</i>"}])
    assert "&lt;b&gt;Bold&lt;/b&gt;" in html
    assert "<b>Bold</b>" not in html


# --- builder: collapsibles + buttons ------------------------------------------

def test_each_session_is_a_collapsible_with_resume_and_rename_inside():
    html = sessions_listing_html(_rows(2))
    assert html.count("<details>") == 2
    assert "<summary>Title 0</summary>" in html
    assert "<summary>Title 1</summary>" in html
    for i in range(2):
        details = html.index(f"<summary>Title {i}</summary>")
        resume = html.index(f"hm:resume:sess-{i}", details)
        rename = html.index(f"hm:rename:sess-{i}", details)
        end = html.index("</details>", details)
        assert resume < end and rename < end  # buttons live inside the collapsed body
    assert '<tg-button type="copy_text" text="sess-0">sess-0</tg-button>' in html
    assert "• session id:" in html
    assert "• message: <i>" in html


def test_preview_is_a_long_italic_message_trail():
    """The message trail is long enough to read (120 chars), not cut at 40."""
    long_preview = "x" * 120
    html = sessions_listing_html(
        [{"id": "sess-0", "title": "Title 0", "preview": long_preview}])
    assert f"<p>• message: <i>{long_preview}</i></p>" in html
    overlong = "A" * 120 + "B" * 80
    html = sessions_listing_html(
        [{"id": "sess-0", "title": "Title 0", "preview": overlong}])
    assert "A" * 120 in html and "B" * 80 not in html


def test_current_session_pill_is_inside_its_collapsible_header():
    html = sessions_listing_html(_rows(3), current_session_id="sess-1")
    assert html.count("Current") == 1
    header = html.index("<summary>Title 1 ")
    assert "Current" in html[header:html.index("</summary>", header)]
    assert html.index("<summary>Title 0</summary>") < header  # only the current one


def test_numbered_listing_prefixes_summary_with_index():
    html = sessions_listing_html(_rows(2), numbered=True)
    assert "<summary>1. Title 0</summary>" in html
    assert "<summary>2. Title 1</summary>" in html


def test_callback_payloads_fit_telegram_64_byte_limit():
    long_id = "20261104_123456_" + "a" * 25  # 40 chars, like a real session id
    html = sessions_listing_html(
        [{"id": long_id, "title": "Long", "preview": "p"}], page_token="tok12345")
    payloads = re.findall(r'data="([^"]+)"', html)
    assert payloads, "expected callback buttons in the listing"
    assert all(len(p.encode("utf-8")) <= 64 for p in payloads)


# --- builder: pagination -------------------------------------------------------

def test_single_page_has_no_divider_or_indexation():
    html = sessions_listing_html(_rows(3))
    assert "<hr>" not in html
    assert "Page 1 of" not in html
    assert "hm:sessions:page:" not in html


def test_multi_page_adds_divider_indexation_and_next_button():
    html = sessions_listing_html(_rows(11), page=1, page_token="tok12345")
    assert html.count("<details>") == 10
    assert "<hr>" in html
    assert "<i>Page 1 of 2" in html
    assert "hm:sessions:page:tok12345:2" in html
    assert "Title 10" not in html  # 11th session waits for page 2
    page2 = sessions_listing_html(_rows(11), page=2, page_token="tok12345")
    assert "<summary>Title 10</summary>" in page2
    assert "<i>Page 2 of 2" in page2
    assert "hm:sessions:page:tok12345:1" in page2
    assert "hm:sessions:page:tok12345:3" not in page2


def test_empty_rows_render_no_collapsibles():
    html = sessions_listing_html([])
    assert "<details>" not in html
    assert "Resume with /resume" in html  # hints still render


# --- runner: page tokens -------------------------------------------------------

def test_rich_listing_registers_token_and_renders_later_pages():
    runner = _make_runner()
    html, copies = runner._rich_sessions_listing(_rows(11), current_session_id=None,
                                                 title="Sessions")
    assert "<i>Page 1 of 2" in html
    assert copies == (), "no hint copy pills travel with the listing"
    assert runner.render_sessions_listing_page("deadbeef", 1) is None  # unknown token
    token = next(iter(runner._sessions_page_tokens))
    page2, _ = runner.render_sessions_listing_page(token, 2)
    assert "<summary>Title 10</summary>" in page2
    assert "<i>Page 2 of 2" in page2


def test_rich_listing_empty_rows_returns_no_html():
    runner = _make_runner()
    html, copies = runner._rich_sessions_listing([], current_session_id=None, title="Sessions")
    assert html is None
    assert copies == ()


# --- runner: rename arming + intercept -----------------------------------------

@pytest.mark.asyncio
async def test_arm_session_rename_denied_for_foreign_session():
    runner = _make_runner()
    runner._session_db.aio.sessions.get = AsyncMock(
        return_value=SimpleNamespace(
            as_session_row=lambda: {"id": "sess-x", "title": "Foreign"}))
    runner._resume_access_denied_reply = AsyncMock(return_value="blocked")
    assert await runner.arm_session_rename(_source(), "sess-x") is None


@pytest.mark.asyncio
async def test_arm_session_rename_returns_title_and_arms_prompt():
    runner = _make_runner()
    runner._session_db.aio.sessions.get = AsyncMock(
        return_value=SimpleNamespace(
            as_session_row=lambda: {"id": "sess-a", "title": "Alpha"}))
    title = await runner.arm_session_rename(_source(), "sess-a")
    assert title == "Alpha"
    assert runner._pending_session_renames["agent:main:telegram:dm:u1"]["session_id"] == "sess-a"


@pytest.mark.asyncio
async def test_rename_reply_renames_and_confirms():
    runner = _make_runner()
    runner._session_db.aio.sessions.set_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="New Name")
    reply = await runner._hm_session_rename_reply(event, _source(), "agent:main:telegram:dm:u1")
    assert reply is not None and "New Name" in reply
    runner._session_db.aio.sessions.set_title.assert_awaited_once_with("sess-a", "New Name")
    assert "agent:main:telegram:dm:u1" not in runner._pending_session_renames


@pytest.mark.asyncio
async def test_rename_reply_cancel_drops_pending_without_renaming():
    runner = _make_runner()
    runner._session_db.aio.sessions.set_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="/cancel")
    reply = await runner._hm_session_rename_reply(event, _source(), "agent:main:telegram:dm:u1")
    assert reply is not None and "cancel" in reply.lower()
    runner._session_db.aio.sessions.set_title.assert_not_awaited()
    assert "agent:main:telegram:dm:u1" not in runner._pending_session_renames


@pytest.mark.asyncio
async def test_rename_reply_command_leaves_pending_armed():
    runner = _make_runner()
    runner._session_db.aio.sessions.set_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="/sessions")
    assert await runner._hm_session_rename_reply(
        event, _source(), "agent:main:telegram:dm:u1") is None
    runner._session_db.aio.sessions.set_title.assert_not_awaited()
    assert "agent:main:telegram:dm:u1" in runner._pending_session_renames


@pytest.mark.asyncio
async def test_rename_reply_with_real_session_db(tmp_path):
    """Regression (2026-11-24): the rename-reply step wrote through the same
    non-existent ``_session_db.set_session_title`` as the arm read through
    ``get_session`` — the reply died the same silent way after the user typed
    the new title. The write must use the store's real ``aio.sessions``
    surface."""
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    home = tmp_path / ".hermes"
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession(session_id="sess-a", source="telegram"))
        await db.aio.sessions.set_title("sess-a", "Alpha")
        runner = _make_runner()
        runner._session_db = db
        runner._pending_session_renames = {
            "agent:main:telegram:dm:u1": {
                "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
        event = SimpleNamespace(text="New Name")
        reply = await runner._hm_session_rename_reply(
            event, _source(), "agent:main:telegram:dm:u1")
        assert reply is not None and "New Name" in reply
        record = await db.aio.sessions.get("sess-a")
        assert record is not None
        assert record.as_session_row()["title"] == "New Name"
        assert "agent:main:telegram:dm:u1" not in runner._pending_session_renames
    finally:
        db.close()


# --- adapter callbacks ---------------------------------------------------------

@pytest.mark.asyncio
async def test_rename_callback_arms_and_prompts():
    adapter = _callback_adapter()
    adapter.gateway_runner.arm_session_rename = AsyncMock(return_value="Alpha")
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_rename_callback(event, source, "hm:rename:sess-a")
    adapter.gateway_runner.arm_session_rename.assert_awaited_once_with(source, "sess-a")
    adapter._send_rich_ui.assert_awaited_once()
    prompt = adapter._send_rich_ui.await_args.args[1]
    assert "Alpha" in prompt and "/cancel" in prompt


@pytest.mark.asyncio
async def test_rename_callback_denied_alerts_without_prompting():
    adapter = _callback_adapter()
    adapter.gateway_runner.arm_session_rename = AsyncMock(return_value=None)
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_rename_callback(event, source, "hm:rename:sess-x")
    adapter._send_rich_ui.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs.get("alert") is True


@pytest.mark.asyncio
async def test_sessions_page_callback_edits_listing():
    adapter = _callback_adapter()
    html = "<h1>Sessions</h1><p><i>Page 2 of 2</i></p>"
    adapter.gateway_runner.render_sessions_listing_page = MagicMock(
        return_value=(html, (("Copy /sessions all", "/sessions all"),)))
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_sessions_page_callback(event, source, "hm:sessions:page:tok12345:2")
    adapter.gateway_runner.render_sessions_listing_page.assert_called_once_with("tok12345", 2)
    adapter._mtproto_rich_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_sessions_page_callback_expired_token_alerts():
    adapter = _callback_adapter()
    adapter.gateway_runner.render_sessions_listing_page = MagicMock(return_value=None)
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_sessions_page_callback(event, source, "hm:sessions:page:deadbeef:2")
    adapter._mtproto_rich_request.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs.get("alert") is True


# --- builder: Delete button + row meta -----------------------------------------

def test_each_session_has_a_delete_button_inside_its_collapsible():
    html = sessions_listing_html(_rows(2), page_token="tok12345")
    for i in range(2):
        details = html.index(f"<summary>Title {i}</summary>")
        delete = html.index(f"hm:delete:tok12345:1:sess-{i}", details)
        end = html.index("</details>", details)
        assert delete < end  # Delete lives inside the collapsed body, with Resume/Rename
    assert "Delete" in html


def test_delete_button_omitted_without_page_token():
    # No token -> no return context for the post-delete refresh, so no button.
    html = sessions_listing_html(_rows(1))
    assert "hm:delete:" not in html


def test_row_meta_shows_compact_tokens_short_model_and_relative_time():
    now = time.time()
    row = {"id": "s1", "title": "Alpha", "preview": "hello",
           "model": "nous/qwen3-max",
           "input_tokens": 300000, "output_tokens": 80000,
           "cache_read_tokens": 6000, "cache_write_tokens": 800,
           "reasoning_tokens": 0, "last_active": now - 2 * 3600}
    runner = _make_runner()
    html, _ = runner._rich_sessions_listing([row], current_session_id=None, title="Sessions")
    assert "386.8K tokens" in html
    assert "qwen3-max" in html
    assert "nous/" not in html
    assert "2h ago" in html


def test_row_meta_absent_when_nothing_recorded():
    runner = _make_runner()
    html, _ = runner._rich_sessions_listing(
        [{"id": "s1", "title": "Alpha", "preview": "hello"}],
        current_session_id=None, title="Sessions")
    assert "tokens" not in html
    assert "ago" not in html


# --- runner: delete arming + confirm -------------------------------------------

def _armed_delete_runner():
    runner = _make_runner()
    runner._session_db.state_meta.get = MagicMock(return_value=None)
    runner._session_db.state_meta.set = MagicMock()
    runner.session_store = MagicMock()
    runner._sessions_page_tokens = {"tok12345": {
        "rows": [{"id": "sess-a", "title": "Alpha"}, {"id": "sess-b", "title": "Beta"}],
        "title": "Sessions", "current": None, "created_at": time.monotonic()}}
    runner._sessions_delete_confirm_tokens = {"confirmtok123456": {
        "session_id": "sess-a", "title": "Alpha", "page_token": "tok12345",
        "page": 1, "created_at": time.monotonic()}}
    return runner


@pytest.mark.asyncio
async def test_arm_session_delete_returns_confirm_token_and_title():
    runner = _make_runner()
    runner._session_db.aio.sessions.get = AsyncMock(
        return_value=SimpleNamespace(
            as_session_row=lambda: {"id": "sess-a", "title": "Alpha"}))
    armed = await runner.arm_session_delete(_source(), "sess-a", "tok12345", 1)
    assert armed is not None
    token, title = armed
    assert title == "Alpha"
    record = runner._sessions_delete_confirm_tokens[token]
    assert record["session_id"] == "sess-a"
    assert record["page_token"] == "tok12345" and record["page"] == 1


@pytest.mark.asyncio
async def test_arm_session_delete_denied_for_foreign_session():
    runner = _make_runner()
    runner._session_db.aio.sessions.get = AsyncMock(
        return_value=SimpleNamespace(
            as_session_row=lambda: {"id": "sess-x", "title": "Foreign"}))
    runner._resume_access_denied_reply = AsyncMock(return_value="blocked")
    assert await runner.arm_session_delete(_source(), "sess-x", "tok12345", 1) is None


@pytest.mark.asyncio
async def test_arm_session_delete_with_real_session_db(tmp_path):
    """Regression (2026-11-24, live report): tapping Delete on /sessions did
    nothing — the button stayed pressed until Telegram timed it out. The arm
    read the session through ``_session_db.get_session``, which does not exist
    on the Persistence handle, so the AttributeError died in the adapter's
    outer ``except`` before any answer. The lookup must use the store's real
    ``aio.sessions.get`` surface."""
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    home = tmp_path / ".hermes"
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession(session_id="sess-a", source="telegram"))
        await db.aio.sessions.set_title("sess-a", "Alpha")
        runner = _make_runner()
        runner._session_db = db
        armed = await runner.arm_session_delete(_source(), "sess-a", "tok12345", 1)
        assert armed is not None
        token, title = armed
        assert title == "Alpha"
        assert runner._sessions_delete_confirms()[token]["session_id"] == "sess-a"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_arm_session_rename_with_real_session_db(tmp_path):
    """Same dead-button cause as Delete: Rename armed through the same
    non-existent ``_session_db.get_session`` and died the same silent way."""
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope

    home = tmp_path / ".hermes"
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession(session_id="sess-a", source="telegram"))
        await db.aio.sessions.set_title("sess-a", "Alpha")
        runner = _make_runner()
        runner._session_db = db
        assert await runner.arm_session_rename(_source(), "sess-a") == "Alpha"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_confirm_deletes_cancels_wakeups_and_drops_listing(monkeypatch, tmp_path):
    from hermes_cli.wakeups import Wakeup, WakeupList

    runner = _armed_delete_runner()
    runner._session_db.state_meta.get = MagicMock(return_value=WakeupList(
        pending=[Wakeup(id="wk_1", prompt="remind me", due_at=time.time() + 60,
                        created_at=time.time())]).to_json())
    runner._resolve_profile_home_for_source = lambda source: tmp_path
    calls = {}

    def fake_delete(store, session_id, sessions_dir):
        calls["delete"] = (store, session_id, sessions_dir)
        return True

    monkeypatch.setattr("gateway.platforms.api_server_store.delete_session", fake_delete)
    monkeypatch.setattr("gateway.session_ingress_media.collect_unheld_api_images",
                        lambda store: calls.setdefault("images", True))
    outcome = await runner.delete_session_confirmed(_source(), "confirmtok123456")
    assert outcome["status"] == "deleted" and outcome["title"] == "Alpha"
    assert outcome["page_token"] == "tok12345" and outcome["page"] == 1
    assert calls["delete"][1] == "sess-a"
    assert calls["delete"][2] == tmp_path / "sessions"
    runner._session_db.state_meta.set.assert_called_once()
    assert "wakeups:sess-a" in runner._session_db.state_meta.set.call_args.args[0]
    runner.session_store.remove_by_session_id.assert_called_once_with("sess-a")
    assert calls.get("images") is True
    rows = runner._sessions_page_tokens["tok12345"]["rows"]
    assert [r["id"] for r in rows] == ["sess-b"]
    assert "confirmtok123456" not in runner._sessions_delete_confirm_tokens


@pytest.mark.asyncio
async def test_confirm_refuses_guarded_session_without_cancelling_wakeups(monkeypatch, tmp_path):
    from persistence import SessionWriteGuarded

    runner = _armed_delete_runner()
    runner._resolve_profile_home_for_source = lambda source: tmp_path

    def guarded_delete(store, session_id, sessions_dir):
        raise SessionWriteGuarded("session sess-a is active")

    monkeypatch.setattr("gateway.platforms.api_server_store.delete_session", guarded_delete)
    outcome = await runner.delete_session_confirmed(_source(), "confirmtok123456")
    assert outcome["status"] == "active"
    runner._session_db.state_meta.set.assert_not_called()
    runner.session_store.remove_by_session_id.assert_not_called()
    rows = runner._sessions_page_tokens["tok12345"]["rows"]
    assert [r["id"] for r in rows] == ["sess-a", "sess-b"]
    assert "confirmtok123456" not in runner._sessions_delete_confirm_tokens


@pytest.mark.asyncio
async def test_confirm_with_real_store_removes_row_and_keeps_memories(tmp_path, monkeypatch):
    from pathlib import Path as _Path

    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from hermes_cli.wakeups import Wakeup, WakeupList
    from persistence.conversation.sessions import NewSession
    from persistence_assembly import open_persistence, profile_scope
    from tools.memory_tool_store import MemoryStore

    home = tmp_path / ".hermes"
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = open_persistence(profile_scope(home=home))
    try:
        db.sessions.create(NewSession(session_id="sess-a", source="telegram"))
        await db.aio.sessions.set_title("sess-a", "Alpha")
        db.state_meta.set("wakeups:sess-a", WakeupList(
            pending=[Wakeup(id="wk_1", prompt="remind me", due_at=time.time() + 60,
                            created_at=time.time())]).to_json())
        memory = MemoryStore()
        memory.add("memory", "the user prefers quiet mornings")
        assert "quiet mornings" in (home / "memories" / "MEMORY.md").read_text()

        runner = _make_runner()
        runner._session_db = db
        runner._resolve_profile_home_for_source = lambda source: home
        config = GatewayConfig()
        runner.session_store = SessionStore(home / "sessions", config)
        runner.session_store.get_or_create_session(_source())
        runner._sessions_page_tokens = {"tok12345": {
            "rows": [{"id": "sess-a", "title": "Alpha"}],
            "title": "Sessions", "current": None, "created_at": time.monotonic()}}
        runner._sessions_delete_confirm_tokens = {"confirmtok123456": {
            "session_id": "sess-a", "title": "Alpha", "page_token": "tok12345",
            "page": 1, "created_at": time.monotonic()}}
        outcome = await runner.delete_session_confirmed(_source(), "confirmtok123456")
        assert outcome["status"] == "deleted"
        assert await db.aio.sessions.get("sess-a") is None
        assert WakeupList.from_json(
            db.state_meta.get("wakeups:sess-a") or '{"pending": []}').pending == []
        assert "quiet mornings" in (home / "memories" / "MEMORY.md").read_text()
        assert [r["id"] for r in runner._sessions_page_tokens["tok12345"]["rows"]] == []
    finally:
        if "runner" in locals():
            runner.session_store.close_all_db_handles()
        db.close()


# --- adapter callbacks: delete ---------------------------------------------------

def _delete_adapter():
    from plugins.platforms.telegram.hm_action_callbacks import handle_delete_callback

    adapter = SimpleNamespace(
        gateway_runner=SimpleNamespace(),
        _peer_for=lambda chat_id: chat_id,
        _mtproto_rich_request=AsyncMock(),
        _is_owner_user=lambda user_id, source=None: True,
    )
    return adapter, handle_delete_callback


def _delete_event():
    return SimpleNamespace(answer=AsyncMock(), message_id=77)


def _delete_source():
    return SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)


def _markup_payloads(request):
    out = []
    for row in request.reply_markup.rows:
        for button in row.buttons:
            out.append(button.type.data.decode("utf-8"))
    return out


@pytest.mark.asyncio
async def test_delete_callback_arms_and_shows_confirm():
    adapter, handle_delete_callback = _delete_adapter()
    adapter.gateway_runner.arm_session_delete = AsyncMock(return_value=("ctok1234567890ab", "Alpha"))
    event = _delete_event()
    await handle_delete_callback(adapter, event, _delete_source(), "hm:delete:tok12345:1:sess-a")
    adapter.gateway_runner.arm_session_delete.assert_awaited_once_with(
        _delete_source(), "sess-a", "tok12345", 1)
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert "Alpha" in request.message
    payloads = _markup_payloads(request)
    assert "hm:delete:confirm:ctok1234567890ab" in payloads
    assert any(p.startswith("hm:delete:cancel:") for p in payloads)


@pytest.mark.asyncio
async def test_delete_callback_non_owner_gets_alert_only():
    adapter, handle_delete_callback = _delete_adapter()
    adapter._is_owner_user = lambda user_id, source=None: False
    event = _delete_event()
    await handle_delete_callback(adapter, event, _delete_source(), "hm:delete:tok12345:1:sess-a")
    adapter._mtproto_rich_request.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs.get("alert") is True


@pytest.mark.asyncio
async def test_delete_confirm_callback_deletes_and_refreshes_listing():
    adapter, handle_delete_callback = _delete_adapter()
    adapter.gateway_runner.delete_session_confirmed = AsyncMock(return_value={
        "status": "deleted", "title": "Alpha", "message": "Session 'Alpha' deleted.",
        "page_token": "tok12345", "page": 1})
    adapter.gateway_runner.render_sessions_listing_page = MagicMock(return_value=(
        "<h1>Sessions</h1><p><i>Page 1 of 1</i></p>", ()))
    event = _delete_event()
    await handle_delete_callback(
        adapter, event, _delete_source(), "hm:delete:confirm:ctok1234567890ab")
    adapter.gateway_runner.delete_session_confirmed.assert_awaited_once_with(
        _delete_source(), "ctok1234567890ab")
    adapter.gateway_runner.render_sessions_listing_page.assert_called_once_with("tok12345", 1)
    adapter._mtproto_rich_request.assert_awaited_once()
    request = adapter._mtproto_rich_request.await_args.args[0]
    assert "Sessions" in request.message


@pytest.mark.asyncio
async def test_delete_confirm_callback_active_session_alerts_without_refresh():
    # The listing is unchanged for a refused delete, and editing it with identical
    # content would fail — so the tap only gets the alert, no re-render.
    adapter, handle_delete_callback = _delete_adapter()
    adapter.gateway_runner.delete_session_confirmed = AsyncMock(return_value={
        "status": "active", "title": "Alpha",
        "message": "Session 'Alpha' is active — a turn is running in it.",
        "page_token": "tok12345", "page": 1})
    event = _delete_event()
    await handle_delete_callback(
        adapter, event, _delete_source(), "hm:delete:confirm:ctok1234567890ab")
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs.get("alert") is True
    assert "active" in (event.answer.await_args.args[0] or "").lower()
    adapter._mtproto_rich_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_cancel_callback_restores_listing():
    adapter, handle_delete_callback = _delete_adapter()
    adapter.gateway_runner.delete_session_confirmed = AsyncMock()
    adapter.gateway_runner.render_sessions_listing_page = MagicMock(return_value=(
        "<h1>Sessions</h1><p><i>Page 1 of 1</i></p>", ()))
    event = _delete_event()
    await handle_delete_callback(
        adapter, event, _delete_source(), "hm:delete:cancel:tok12345:1")
    adapter.gateway_runner.delete_session_confirmed.assert_not_awaited()
    adapter.gateway_runner.render_sessions_listing_page.assert_called_once_with("tok12345", 1)
    adapter._mtproto_rich_request.assert_awaited_once()
