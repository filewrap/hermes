"""Phase 5 Group A: Telegram rich cards for /status, /context, /last, /usage,
/agents, /insights, /topup, /version, /profile, /whoami, /commands.

Invariant tests, written red-first: on Telegram each handler sends one native
rich card through ``adapter.send_command_card`` and returns None (no plain-text
fallback); on other platforms the plain-text reply is unchanged.
"""

from datetime import UTC, datetime
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
    """Minimal async session store; ``_store`` must be the runner's session_store
    so ``GatewayRunner.async_session_store`` accepts this facade."""

    def __init__(self, history=None, store=None):
        self._history = history if history is not None else []
        self._store = store

    async def get_or_create_session(self, source):
        return SimpleNamespace(
            session_key="agent:main:telegram:dm:u1", session_id="sess-1",
            created_at=datetime.now(), updated_at=datetime.now(),
            platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0,
            last_prompt_tokens=0,
        )

    async def load_transcript(self, session_id):
        return list(self._history)


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
    runner._cached_agent_for = lambda key: None
    runner._rehydrate_session_model_override = lambda key: None
    runner._session_model_override = lambda key: {}
    runner._resolve_route_context = AsyncMock(return_value=None)
    runner._queue_depth = lambda *args, **kwargs: 0
    runner._session_db = None
    runner._resident_agent_for = lambda key: None
    runner.session_store = MagicMock()
    runner._async_session_store = _Store(store=runner.session_store)
    runner.session_store.get_or_create_session.return_value = SimpleNamespace(
        session_key="agent:main:telegram:dm:u1", session_id="sess-1")
    runner.session_store.load_transcript.return_value = []
    runner._run_in_executor_with_context = AsyncMock(return_value="Insights over 30 days.")
    return runner, adapter


# --- Pure card builders -------------------------------------------------------

def test_lines_to_rich_html_bolds_values_and_escapes_markup():
    from plugins.platforms.telegram.command_cards import lines_to_rich_html

    html_out = lines_to_rich_html("Status", ["# Heading", "**Model:** `gpt-5`", "a <b> & c", "", "- one"])
    assert "<h1>Heading</h1>" in html_out
    assert "<strong>Model:</strong>" in html_out
    assert "<code>gpt-5</code>" in html_out
    assert "a &lt;b&gt; &amp; c" in html_out
    assert "<li>one</li>" in html_out
# --- /status ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_command_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_status_command(_event("/status"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "Hermes Gateway Status" in kwargs["html"]
    assert "📊" not in kwargs["html"]
    assert "sess-1" in kwargs["html"]
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row


@pytest.mark.asyncio
async def test_status_command_stays_plain_text_off_telegram():
    runner, adapter = _make_runner(platform=Platform.SLACK)
    adapter.send_command_card = None
    result = await runner._handle_status_command(_event("/status", platform=Platform.SLACK))
    assert isinstance(result, str) and "Hermes Gateway Status" in result


# --- /last --------------------------------------------------------------------

def _agent_with(summary):
    return SimpleNamespace(get_activity_summary=lambda: summary)


@pytest.mark.asyncio
async def test_last_command_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    summary = {
        "current_tool": None,
        "last_tool_activity": {
            "tool": "terminal", "args": '{"command": "ls"}', "result": "a.txt",
            "error": False, "blocked": False, "duration_s": 0.8, "ts": 1000.0,
        },
        "last_activity_desc": "tool completed: terminal (0.8s)",
    }
    runner._running_agents["agent:main:telegram:dm:u1"] = _agent_with(summary)
    result = await runner._handle_last_command(_event("/last"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "terminal" in kwargs["html"]
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row


@pytest.mark.asyncio
async def test_last_command_empty_session_sends_rich_card_not_plain_text():
    runner, adapter = _make_runner()
    result = await runner._handle_last_command(_event("/last"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    assert "No activity recorded yet" in adapter.send_command_card.await_args.kwargs["html"]


# --- /usage -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_command_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_usage_command(_event("/usage"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed


# --- /agents ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agents_command_sends_rich_card_on_telegram(monkeypatch):
    runner, adapter = _make_runner()
    result = await runner._handle_agents_command(_event("/agents"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    assert "nav" not in adapter.send_command_card.await_args.kwargs  # hm:run:/ nav buttons removed


# --- /context -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_command_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    result = await runner._handle_context_command(_event("/context"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "Context window" in kwargs["html"]
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row


# --- /insights ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_insights_command_sends_rich_card_on_telegram():
    runner, adapter = _make_runner()
    # The handler unpacks (text, html, copies) from the executor hop.
    runner._run_in_executor_with_context = AsyncMock(return_value=(
        "Insights over 30 days.", "<h1>Insights</h1><p>Insights over 30 days.</p>", ()))
    result = await runner._handle_insights_command(_event("/insights"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "Insights over 30 days." in kwargs["html"]
    assert "nav" not in kwargs  # hm:run:/ nav buttons removed: no navigation row


# --- /topup -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_topup_command_turns_portal_link_into_url_button(monkeypatch):
    runner, adapter = _make_runner()

    def _fake_view(*args, **kwargs):
        return SimpleNamespace(
            logged_in=True,
            balance_lines=["Balance: $5.00", "📈 chart line"],
            identity_line="Signed in as tester",
            topup_url="https://example.test/topup",
        )

    monkeypatch.setattr("agent.account_usage.build_credits_view", _fake_view)
    result = await runner._handle_topup_command(_event("/topup"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "https://example.test/topup" not in kwargs["html"]
    assert ("Open billing portal", "https://example.test/topup") in kwargs["links"]
    assert "📈" not in kwargs["html"] and "💳" not in kwargs["html"]


# --- /version, /profile, /whoami ----------------------------------------------

def _version_info(**overrides):
    from hermes_cli.version_info import VersionInfo

    base = {
        "base_version": "2026.9.24",
        "derived_version": "2026.9.24+5.gabc1234def",
        "distance": 5,
        "commit": "abc1234def5678901234567890abcdef12345678",
        "branch": "main",
        "source": "git",
        "dirty": False,
        "commit_date": int(datetime(2026, 11, 4, 12, 30, tzinfo=UTC).timestamp()),
        "distribution": None,
    }
    base.update(overrides)
    return VersionInfo(**base)


def test_version_detail_card_shows_every_field():
    from gateway.slash_commands import _version_detail_card

    text, copies = _version_detail_card(
        _version_info(
            upstream_stable="0.22.6",
            upstream_canary="0.22.6+canary.20261103T064339Z",
            fork_version="0.22.6-38:129cc54ef3",
            commit_message="fix(gateway): re-anchor a running turn's reply",
        ),
        python_version="3.14.9", platform_str="Linux-6.8-x86_64")
    assert "## Version(s)" in text
    assert "- **Hermes Agent:** `0.22.6`" in text
    assert "- **Canary:** `0.22.6+canary.20261103T064339Z`" in text
    assert "- **Fork version:** `0.22.6-38:129cc54ef3`" in text
    assert "2026.9.24" in text
    assert "abc1234def5678901234567890abcdef12345678" in text
    assert "main" in text
    assert "fix(gateway): re-anchor a running turn's reply" in text
    assert "2026-11-04 12:30 UTC" in text
    assert "Commits since release:" in text
    assert "git" in text
    assert "3.14.9" in text
    assert "Linux-6.8-x86_64" in text
    assert ("Copy version", "0.22.6-38:129cc54ef3") in copies
    assert ("Copy commit", "abc1234def5678901234567890abcdef12345678") in copies


def test_version_detail_card_copies_derived_version_without_fork_version():
    from gateway.slash_commands import _version_detail_card

    text, copies = _version_detail_card(
        _version_info(
            upstream_stable="0.22.6",
            upstream_canary="0.22.6+canary.20261103T064339Z",
        ),
        python_version="3.14.9", platform_str="Linux-6.8-x86_64")
    assert "## Version(s)" in text
    assert "- **Hermes Agent:** `0.22.6`" in text
    assert "Fork version:" not in text
    assert ("Copy version", "2026.9.24+5.gabc1234def") in copies


def test_version_detail_card_omits_absent_fields():
    from gateway.slash_commands import _version_detail_card

    text, copies = _version_detail_card(
        _version_info(base_version="unknown", derived_version="unknown",
                      distance=None, commit=None, branch=None,
                      source="unknown", commit_date=None),
        python_version="3.14.9", platform_str="Linux-6.8-x86_64")
    assert "Hermes Agent **unknown**" in text
    assert "Release:" not in text
    assert "Commit:" not in text
    assert "Branch:" not in text
    assert "Commit date:" not in text
    assert "Commits since release:" not in text
    assert "Distribution:" not in text
    assert "Working tree:" not in text
    assert "Source:" in text
    assert "Python:" in text
    assert "Platform:" in text
    assert copies == (("Copy version", "unknown"),)


def test_version_detail_card_shows_dirty_working_tree():
    from gateway.slash_commands import _version_detail_card

    text, _copies = _version_detail_card(
        _version_info(dirty=True), python_version="3.14.9", platform_str="Linux-6.8-x86_64")
    assert "Working tree:" in text
    assert "dirty" in text


@pytest.mark.asyncio
async def test_version_command_sends_rich_card_on_telegram(monkeypatch):
    import hermes_cli.version_info as vi

    runner, adapter = _make_runner()
    monkeypatch.setattr(vi, "get_version_info", lambda: _version_info())
    result = await runner._handle_version_command(_event("/version"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    html = kwargs["html"]
    assert "2026.9.24+5.gabc1234def" in html
    assert "abc1234def5678901234567890abcdef12345678" in html
    assert "main" in html
    assert "Python:" in html
    assert "Platform:" in html
    # No regular buttons: no nav keyboard, no explicit buttons.
    assert tuple(kwargs.get("nav") or ()) == ()
    assert tuple(kwargs.get("buttons") or ()) == ()
    assert ("Copy version", "2026.9.24+5.gabc1234def") in kwargs.get("copies", ())


@pytest.mark.asyncio
async def test_profile_command_sends_rich_card_with_copyable_home(monkeypatch):
    import gateway.slash_commands as sc

    runner, adapter = _make_runner()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    reply = SimpleNamespace(data={"profile": "default", "home": "/home/tester/.hermes"})
    monkeypatch.setattr(sc, "_execute", lambda *a, **k: reply)
    result = await runner._handle_profile_command(_event("/profile"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    kwargs = adapter.send_command_card.await_args.kwargs
    assert "default" in kwargs["html"]
    assert ("/home/tester/.hermes") in str(kwargs.get("copies") or kwargs["html"])


@pytest.mark.asyncio
async def test_whoami_command_sends_rich_card_on_telegram(monkeypatch):
    import gateway.slash_access as sa

    runner, adapter = _make_runner()
    policy = SimpleNamespace(enabled=False, is_admin=lambda uid: False,
                             user_allowed_commands=[])
    monkeypatch.setattr(sa, "policy_for_runner_source", lambda *a, **k: policy)
    result = await runner._handle_whoami_command(_event("/whoami"))
    assert result is None
    adapter.send_command_card.assert_awaited_once()
    assert "u1" in adapter.send_command_card.await_args.kwargs["html"]

