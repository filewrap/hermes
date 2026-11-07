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
    runner._session_db.get_session = AsyncMock(
        return_value={"id": "sess-x", "title": "Foreign"})
    runner._resume_access_denied_reply = AsyncMock(return_value="blocked")
    assert await runner.arm_session_rename(_source(), "sess-x") is None


@pytest.mark.asyncio
async def test_arm_session_rename_returns_title_and_arms_prompt():
    runner = _make_runner()
    runner._session_db.get_session = AsyncMock(
        return_value={"id": "sess-a", "title": "Alpha"})
    title = await runner.arm_session_rename(_source(), "sess-a")
    assert title == "Alpha"
    assert runner._pending_session_renames["agent:main:telegram:dm:u1"]["session_id"] == "sess-a"


@pytest.mark.asyncio
async def test_rename_reply_renames_and_confirms():
    runner = _make_runner()
    runner._session_db.set_session_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="New Name")
    reply = await runner._hm_session_rename_reply(event, _source(), "agent:main:telegram:dm:u1")
    assert reply is not None and "New Name" in reply
    runner._session_db.set_session_title.assert_awaited_once_with("sess-a", "New Name")
    assert "agent:main:telegram:dm:u1" not in runner._pending_session_renames


@pytest.mark.asyncio
async def test_rename_reply_cancel_drops_pending_without_renaming():
    runner = _make_runner()
    runner._session_db.set_session_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="/cancel")
    reply = await runner._hm_session_rename_reply(event, _source(), "agent:main:telegram:dm:u1")
    assert reply is not None and "cancel" in reply.lower()
    runner._session_db.set_session_title.assert_not_awaited()
    assert "agent:main:telegram:dm:u1" not in runner._pending_session_renames


@pytest.mark.asyncio
async def test_rename_reply_command_leaves_pending_armed():
    runner = _make_runner()
    runner._session_db.set_session_title = AsyncMock(return_value=True)
    runner._pending_session_renames = {
        "agent:main:telegram:dm:u1": {
            "session_id": "sess-a", "title": "Alpha", "created_at": time.monotonic()}}
    event = SimpleNamespace(text="/sessions")
    assert await runner._hm_session_rename_reply(
        event, _source(), "agent:main:telegram:dm:u1") is None
    runner._session_db.set_session_title.assert_not_awaited()
    assert "agent:main:telegram:dm:u1" in runner._pending_session_renames


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
