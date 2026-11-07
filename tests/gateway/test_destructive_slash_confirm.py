"""Tests for the gateway's destructive-slash-confirm wrapper.

When ``approvals.destructive_slash_confirm`` is True (default), /new,
/reset, and /undo route through the slash-confirm primitive — native
yes/no buttons on Telegram/Discord/Slack, text fallback elsewhere.
When False (after "Always Approve"), the destructive action runs
immediately.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    """Mirror tests/gateway/test_unknown_command.py::_make_runner."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    # The native Telegram confirmation card was rendered; keep the pending
    # one-shot callback registered until it is approved or cancelled.
    adapter.send_slash_confirm = AsyncMock(return_value=SimpleNamespace(success=True))
    runner.adapters = {Platform.TELEGRAM: adapter}

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()

    runner._running_agents = {}
    runner._pending_messages = {}
    import itertools as _it
    runner._slash_confirm_counter = _it.count(1)
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner._thread_metadata_for_source = lambda *a, **kw: None
    runner._reply_anchor_for_event = lambda _e: None
    runner._typed_command_prefix_for = lambda _platform: "/"
    return runner


@pytest.mark.asyncio
async def test_gate_on_pending_confirm_registered(monkeypatch):
    """When the gate is on, a pending slash-confirm entry is registered for
    the session — the user's /approve reply will resolve it."""
    from tools import slash_confirm as _slash_confirm_mod
    runner = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    session_key = build_session_key(_make_source())
    runner._session_key_for_source = lambda src: session_key
    _slash_confirm_mod.clear(session_key)

    execute = AsyncMock(return_value="reset done")

    await runner._maybe_confirm_destructive_slash(
        event=_make_event("/new"),
        command="new",
        title="/new",
        detail="This starts a fresh session and discards the current conversation history.",
        execute=execute,
    )

    prompt = runner.adapters[Platform.TELEGRAM].send_slash_confirm.await_args.kwargs["message"]
    assert "**Confirm /new**" in prompt
    assert "discards the current conversation history" in prompt
    assert "**Text fallback:** reply `/approve`, `/always`, or `/cancel`." in prompt
    assert "_Text fallback:" not in prompt
    assert not any(icon in prompt for icon in ("⚠️", "✅", "❌", "ℹ️", "🟡"))
    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    assert pending["command"] == "new"
    _slash_confirm_mod.clear(session_key)


@pytest.mark.asyncio
async def test_resolve_always_persists_opt_out_and_runs_execute(monkeypatch):
    """Resolving with 'always' must (a) flip the config gate to False,
    (b) run execute, and (c) include a one-time opt-out note in the reply."""
    from tools import slash_confirm as _slash_confirm_mod
    runner = _make_runner()
    runner._read_user_config = lambda: {"approvals": {"destructive_slash_confirm": True}}
    session_key = build_session_key(_make_source())
    runner._session_key_for_source = lambda src: session_key
    _slash_confirm_mod.clear(session_key)

    saved: dict = {}

    def _fake_save(path, value):
        saved[path] = value
        return True

    import cli as cli_mod
    monkeypatch.setattr(cli_mod, "save_config_value", _fake_save)

    execute = AsyncMock(return_value="Fresh session started.")

    await runner._maybe_confirm_destructive_slash(
        event=_make_event("/new"),
        command="new",
        title="/new",
        detail="Discards history.",
        execute=execute,
    )

    pending = _slash_confirm_mod.get_pending(session_key)
    assert pending is not None
    resolved = await _slash_confirm_mod.resolve(
        session_key, pending["confirm_id"], "always",
    )

    execute.assert_awaited_once()
    assert saved.get("approvals.destructive_slash_confirm") is False
    assert resolved is not None
    assert "Fresh session started." in resolved
    assert "config.yaml" in resolved
    assert not any(icon in resolved for icon in ("⚠️", "✅", "❌", "ℹ️", "🟡"))


@pytest.mark.asyncio
async def test_cancel_response_has_no_emoji_and_preserves_conversation():
    from gateway.run_busy import GatewayBusySessionMixin

    execute = AsyncMock(return_value="must not run")
    response = await GatewayBusySessionMixin._run_confirmed_destructive_slash(
        "cancel", "new", execute, "session-1",
    )

    assert response == "/new cancelled. Conversation unchanged."
    assert not any(icon in response for icon in ("⚠️", "✅", "❌", "ℹ️", "🟡"))
    execute.assert_not_awaited()
