"""Rich /kanban help page: bare ``/kanban`` on Telegram sends a structured card
(heading, one indented row per subcommand from the curated help block, italic
footer hints, copy pills for the common subcommands) and returns None; off
Telegram the plain help text is unchanged, and non-help output still flows
through the existing reply path.
"""

import html as html_lib
import re
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


def _source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform, user_id="u1", chat_id="c1", user_name="tester", chat_type="dm",
    )


def _event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(text=text, source=_source(platform), message_id="m1")


class _Store:
    """Minimal async session store for a bare GatewayRunner."""

    def __init__(self, session_id="sess-1", store=None):
        self._session_id = session_id
        self._store = store

    async def get_or_create_session(self, source):
        return SimpleNamespace(
            session_key="agent:main:telegram:dm:u1", session_id=self._session_id,
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0,
            last_prompt_tokens=0,
        )

    async def load_transcript(self, session_id):
        return []


def _make_runner(*, telegram_card: bool = True, platform: Platform = Platform.TELEGRAM):
    """Bare GatewayRunner with a fake Telegram adapter exposing send_command_card."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    card = AsyncMock(return_value=True) if telegram_card else None
    adapter = SimpleNamespace(send=AsyncMock(), send_command_card=card)
    runner.adapters = {platform: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._reply_anchor_for_event = lambda event: None
    runner._session_key_for_source = lambda source: "agent:main:telegram:dm:u1"
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._background_tasks = set()
    runner._agent_cache = {}
    runner._session_db = None
    runner.session_store = MagicMock()
    runner._async_session_store = _Store(store=runner.session_store)
    runner._reply_metadata = lambda event: None
    runner._thread_metadata_for_source = lambda source: None
    return runner, adapter


def _card_kwargs(adapter):
    card = adapter.send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs


def _help_rows(help_text: str) -> list[str]:
    """First backtick span of every indented subcommand line in the help block."""
    return [re.match(r"`([^`]+)`", line.strip()).group(1)
            for line in help_text.split("\n") if line.strip().startswith("`")]


# --- Pure card builder ---------------------------------------------------------

def test_kanban_help_card_renders_every_help_row():
    from gateway.slash_commands import _kanban_help_card_html
    from hermes_cli.kanban import run_slash

    help_text = run_slash("")
    html = _kanban_help_card_html(help_text)
    rows = _help_rows(help_text)
    assert rows, "help block has no subcommand rows to render"
    assert html.count("<li>") == len(rows)
    for name in rows:
        assert f"<code>{html_lib.escape(name, quote=False)}</code>" in html
    assert "<h1>Kanban</h1>" in html
    assert "manage the shared task board" in html
    assert "<i>" in html and "Read-only commands are safe" in html
    assert "**" not in html


def test_kanban_help_card_has_no_emoji():
    from gateway.slash_commands import _kanban_help_card_html
    from hermes_cli.kanban import run_slash
    from plugins.platforms.telegram.command_cards import _EMOJI_RE

    assert _EMOJI_RE.search(_kanban_help_card_html(run_slash(""))) is None


# --- Gateway handler -----------------------------------------------------------

@pytest.mark.asyncio
async def test_bare_kanban_sends_rich_help_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_kanban_command(_event("/kanban"))
    assert result is None
    kwargs = _card_kwargs(adapter)
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row
    html = kwargs["html"]
    assert "<h1>Kanban</h1>" in html
    assert "<ul>" in html and "<li>" in html
    assert "<i>" in html and "Read-only commands are safe" in html
    copies = dict(kwargs.get("copies") or ())
    assert copies.get("Copy /kanban list") == "/kanban list"
    assert copies.get("Copy /kanban show") == "/kanban show <id>"
    assert copies.get("Copy /kanban create") == "/kanban create <title>"
    # No hm:run: payloads anywhere on the card.
    assert "hm:run:" not in html
    assert not any("hm:run:" in str(v) for v in (kwargs.get("buttons") or ()))


@pytest.mark.asyncio
async def test_bare_kanban_stays_plain_off_telegram():
    from hermes_cli.kanban import run_slash

    runner, _adapter = _make_runner(telegram_card=False, platform=Platform.SIGNAL)
    result = await runner._handle_kanban_command(_event("/kanban", platform=Platform.SIGNAL))
    assert result == run_slash("")


@pytest.mark.asyncio
async def test_kanban_list_output_still_flows_through_reply_path(monkeypatch):
    from hermes_cli import kanban

    listing = "t_abc123  ready  Fix the thing\nt_def456  done   Ship it"
    monkeypatch.setattr(kanban, "run_slash", lambda text: listing)
    runner, adapter = _make_runner()
    result = await runner._handle_kanban_command(_event("/kanban list"))
    assert result is None
    assert "t_abc123" in _card_kwargs(adapter)["html"]
    runner2, _adapter2 = _make_runner(telegram_card=False, platform=Platform.SIGNAL)
    assert await runner2._handle_kanban_command(
        _event("/kanban list", platform=Platform.SIGNAL)) == listing
