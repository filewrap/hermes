""" /memories + /memory all: every stored entry in full, no tables, paged collapsibles.

Invariant tests, written red-first: the listing reads the SAME store/scope as
/memory (load_on_disk_store / get_memory_dir — personal scope in an authorized
DM, profile scope elsewhere); each entry renders in full inside one <details>
collapsible (no <table> markup anywhere — a long entry must never be cut to a
column width); lists page at 10 with Prev/Next tokens like /sessions.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from tools.memory_tool_store import ENTRY_DELIMITER, MemoryStore

LONG_ENTRY = "L" * 500
MULTILINE_ENTRY = "First line\nSecond line\nThird line"


def _store_with(*, memory=(), user=()):
    store = MemoryStore(memory_char_limit=100000, user_char_limit=100000)
    store.memory_entries = list(memory)
    store.user_entries = list(user)
    return store


def _entries(n, *, long_at=None):
    return [LONG_ENTRY if i == long_at else f"Memory entry {i} — some detail here"
            for i in range(n)]


def _source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester",
        chat_type="dm",
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform), message_id="m1")


def _make_runner(*, telegram_card: bool = True, platform: Platform = Platform.TELEGRAM):
    """Bare GatewayRunner with a fake Telegram adapter exposing send_command_card."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    card = AsyncMock(return_value=True) if telegram_card else None
    adapter = SimpleNamespace(send=AsyncMock(), send_command_card=card)
    runner.adapters = {platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    return runner, adapter


def _card_html(adapter) -> str:
    card = adapter.send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


def _callback_adapter():
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = object.__new__(TelethonAdapter)
    adapter.gateway_runner = SimpleNamespace()
    adapter._send_rich_ui = AsyncMock()
    adapter._peer_for = lambda chat_id: chat_id
    adapter._mtproto_rich_request = AsyncMock()
    return adapter


# --- store rows: every entry, in full -----------------------------------------

def test_list_memories_returns_every_entry_in_full():
    from tools.memory_tool import list_memories

    store = _store_with(
        memory=[LONG_ENTRY, MULTILINE_ENTRY, "short"],
        user=["user fact one", "user fact two"],
    )
    rows = list_memories(store)
    assert [r["text"] for r in rows] == [
        LONG_ENTRY, MULTILINE_ENTRY, "short", "user fact one", "user fact two",
    ]
    assert [r["target"] for r in rows] == ["memory", "memory", "memory", "user", "user"]
    assert [r["index"] for r in rows] == [1, 2, 3, 1, 2]


def test_format_memories_text_keeps_long_entries_whole_and_has_no_table():
    from tools.memory_tool import format_memories_text, list_memories

    rows = list_memories(_store_with(memory=[LONG_ENTRY], user=["plain user fact"]))
    text = format_memories_text(rows)
    assert LONG_ENTRY in text  # not cut to any column width
    assert "plain user fact" in text
    assert "<table" not in text
    assert "Memory" in text and "User profile" in text


def test_format_memories_text_empty_store_says_so():
    from tools.memory_tool import format_memories_text, list_memories

    assert list_memories(_store_with()) == []
    assert "No saved memories" in format_memories_text([])


# --- rich builder: collapsibles, no tables, paging -----------------------------

def test_memories_listing_html_has_one_collapsible_per_entry_and_no_table():
    from plugins.platforms.telegram.command_cards import memories_listing_html
    from tools.memory_tool import list_memories

    rows = list_memories(
        _store_with(memory=[LONG_ENTRY, MULTILINE_ENTRY], user=["user fact"]))
    html = memories_listing_html(rows)
    assert "<table" not in html
    assert html.count("<details>") == 3
    assert LONG_ENTRY in html  # full long entry, not cut to a column width
    assert "Second line" in html  # multi-line entry kept whole
    assert "<summary>1. " in html
    assert "User profile" in html  # the user section is labelled


def test_memories_listing_html_pages_at_ten():
    from plugins.platforms.telegram.command_cards import memories_listing_html
    from tools.memory_tool import list_memories

    rows = list_memories(_store_with(memory=_entries(11)))
    page1 = memories_listing_html(rows, page=1, page_token="tok12345")
    assert page1.count("<details>") == 10
    assert "<i>Page 1 of 2" in page1
    assert "hm:memories:page:tok12345:2" in page1
    assert "Memory entry 10" not in page1  # 11th entry waits for page 2
    page2 = memories_listing_html(rows, page=2, page_token="tok12345")
    assert "Memory entry 10" in page2
    assert "<i>Page 2 of 2" in page2
    assert "hm:memories:page:tok12345:1" in page2
    assert "hm:memories:page:tok12345:3" not in page2


def test_memories_listing_html_single_page_has_no_divider_or_indexation():
    from plugins.platforms.telegram.command_cards import memories_listing_html
    from tools.memory_tool import list_memories

    html = memories_listing_html(list_memories(_store_with(memory=_entries(3))))
    assert "<hr>" not in html
    assert "Page 1 of" not in html
    assert "hm:memories:page:" not in html


# --- /memory all through the shared handler (CLI path) -------------------------

def test_memory_all_subcommand_lists_every_entry():
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    store = _store_with(memory=[LONG_ENTRY], user=["user fact"])
    text = handle_pending_subcommand(wa.MEMORY, ["all"], memory_store=store)
    assert text is not None
    assert LONG_ENTRY in text
    assert "user fact" in text
    assert "<table" not in text


# --- registration + access ------------------------------------------------------

def test_memories_command_is_registered_owner_only_and_idle():
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.tenant_commands import tenant_command_policy
    from hermes_cli.commands import COMMAND_REGISTRY

    assert any(cmd.name == "memories" for cmd in COMMAND_REGISTRY)
    assert tenant_command_policy("memories") == "owner"
    assert "memories" in GatewayBusySessionMixin._IDLE_COMMANDS


def test_memory_command_advertises_all_subcommand():
    from hermes_cli.commands import COMMAND_REGISTRY

    memory = next(cmd for cmd in COMMAND_REGISTRY if cmd.name == "memory")
    assert "all" in memory.subcommands
    assert "all" in memory.args_hint
    # No desktop_subcommands scope on /memory (None = every subcommand offered),
    # so "all" needs no separate desktop entry.


# --- scope: same store/scope as /memory -----------------------------------------

def test_listing_uses_the_same_scope_as_memory_pending(tmp_path, monkeypatch):
    from gateway.telegram_personal_settings import (
        reset_personal_memory_context, set_personal_memory_context,
    )
    from tools.memory_tool import list_memories, load_on_disk_store

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profile_dir = tmp_path / "memories"
    profile_dir.mkdir()
    (profile_dir / "MEMORY.md").write_text(
        "profile memory fact" + ENTRY_DELIMITER, encoding="utf-8")
    personal_dir = tmp_path / "personal" / "telegram" / "users" / "42" / "memory"
    personal_dir.mkdir(parents=True)
    (personal_dir / "MEMORY.md").write_text(
        "personal memory fact" + ENTRY_DELIMITER, encoding="utf-8")

    # Profile scope (no personal context): the same loader /memory pending uses.
    assert [r["text"] for r in list_memories(load_on_disk_store())] == [
        "profile memory fact"]
    # Authorized-DM personal scope.
    token = set_personal_memory_context(tmp_path, 42)
    try:
        assert [r["text"] for r in list_memories(load_on_disk_store())] == [
            "personal memory fact"]
    finally:
        reset_personal_memory_context(token)


# --- runner: rich card on Telegram, plain text elsewhere -------------------------

def test_rich_memories_listing_registers_token_and_renders_later_pages():
    from gateway.run import GatewayRunner
    from tools.memory_tool import list_memories

    runner = object.__new__(GatewayRunner)
    rows = list_memories(_store_with(memory=_entries(11)))
    html, copies = runner._rich_memories_listing(rows, title="Memories")
    assert "<i>Page 1 of 2" in html
    assert copies == (), "no hint copy pills travel with the listing"
    assert runner.render_memories_listing_page("deadbeef", 1) is None  # unknown token
    token = next(iter(runner._memories_page_tokens))
    page2, _ = runner.render_memories_listing_page(token, 2)
    assert "Memory entry 10" in page2
    assert "<i>Page 2 of 2" in page2


def test_rich_memories_listing_empty_rows_returns_no_html():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    html, copies = runner._rich_memories_listing([], title="Memories")
    assert html is None
    assert copies == ()


@pytest.mark.asyncio
async def test_memories_command_sends_rich_card_on_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text(
        LONG_ENTRY + ENTRY_DELIMITER + "short fact" + ENTRY_DELIMITER,
        encoding="utf-8")
    runner, adapter = _make_runner()
    result = await runner._handle_memories_command(_event("/memories"))
    assert result is None
    html = _card_html(adapter)
    assert "<table" not in html
    assert html.count("<details>") == 2
    assert LONG_ENTRY in html


@pytest.mark.asyncio
async def test_memory_all_subcommand_sends_rich_card_on_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text(
        "listed fact" + ENTRY_DELIMITER, encoding="utf-8")
    runner, adapter = _make_runner()
    result = await runner._handle_memory_command(_event("/memory all"))
    assert result is None
    assert "listed fact" in _card_html(adapter)


@pytest.mark.asyncio
async def test_memories_command_returns_plain_text_off_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text(
        "listed fact" + ENTRY_DELIMITER, encoding="utf-8")
    runner, _adapter = _make_runner(
        telegram_card=False, platform=Platform.SLACK)
    result = await runner._handle_memories_command(
        _event("/memories", platform=Platform.SLACK))
    assert result is not None
    assert "listed fact" in result


# --- CLI surface: plain-text listing through the mixin handlers ----------------

def test_cli_memories_command_prints_every_entry(tmp_path, monkeypatch, capsys):
    """CLI /memories prints the full plain-text listing (same store as /memory)."""
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text(
        LONG_ENTRY + ENTRY_DELIMITER + "short fact" + ENTRY_DELIMITER,
        encoding="utf-8")
    (memories_dir / "USER.md").write_text(
        "user fact" + ENTRY_DELIMITER, encoding="utf-8")
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memories_command("/memories")
    out = capsys.readouterr().out
    assert LONG_ENTRY in out
    assert "short fact" in out and "user fact" in out
    assert "<table" not in out


def test_cli_memory_all_prints_every_entry(tmp_path, monkeypatch, capsys):
    """CLI /memory all prints the same listing as /memories."""
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir()
    (memories_dir / "MEMORY.md").write_text(
        "listed fact" + ENTRY_DELIMITER, encoding="utf-8")
    handler = CLICommandsMixin.__new__(CLICommandsMixin)
    handler.agent = None
    handler._handle_memory_command("/memory all")
    out = capsys.readouterr().out
    assert "listed fact" in out
    assert "<table" not in out


# --- adapter: page callback ------------------------------------------------------

@pytest.mark.asyncio
async def test_memories_page_callback_edits_listing():
    adapter = _callback_adapter()
    html = "<h1>Memories</h1><p><i>Page 2 of 2</i></p>"
    adapter.gateway_runner.render_memories_listing_page = MagicMock(
        return_value=(html, ()))
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_memories_page_callback(event, source, "hm:memories:page:tok12345:2")
    adapter.gateway_runner.render_memories_listing_page.assert_called_once_with("tok12345", 2)
    adapter._mtproto_rich_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_memories_page_callback_expired_token_alerts():
    adapter = _callback_adapter()
    adapter.gateway_runner.render_memories_listing_page = MagicMock(return_value=None)
    source = SimpleNamespace(chat_id="c1", user_id="u1", user_name="tester", thread_id=None)
    event = SimpleNamespace(answer=AsyncMock(), message_id=77)
    await adapter._handle_memories_page_callback(event, source, "hm:memories:page:deadbeef:2")
    adapter._mtproto_rich_request.assert_not_awaited()
    event.answer.assert_awaited_once()
    assert event.answer.await_args.kwargs.get("alert") is True
