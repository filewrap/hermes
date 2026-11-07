"""A turn that raises on the gateway keeps its real cause for a local client (CLI, TUI, Desktop),
whose panel otherwise read ``Details: ⚠️ Something went wrong and I couldn't finish this reply.``
The owner on Telegram sees the real cause in the chat reply too — they are the operator, and a
generic apology hides the exact failure (e.g. a provider credential cooling down). A non-owner on
Telegram keeps the sanitized apology: raw exception text never reaches a stranger's chat."""

import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.session_results import execution_result


def _committed_reply(platform, *, owner=False):
    runner = object.__new__(GatewayRunner)

    async def stop_typing(event, source):
        return None

    runner._hmwa_stop_typing_for_turn = stop_typing
    runner._session_state = lambda key: SimpleNamespace(turn=SimpleNamespace(agent=None))
    adapter = SimpleNamespace(_is_owner_user=lambda user_id, source=None: owner)
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=platform, chat_id="c", user_id="u")
    prepared = runner._PreparedTurn([], "", None, None, None, None)
    err = ImportError("The 'anthropic' package is required for the Anthropic provider: restart Hermes")
    captured = {}
    token = execution_result.set(captured)
    try:
        asyncio.run(runner._hmwa_agent_error_reply(
            err, MessageEvent(text="x", source=source), source, None, "k", prepared,
        ))
    finally:
        execution_result.reset(token)
    return captured["result"]["error"]


def test_local_client_sees_the_raised_cause_and_non_owner_chat_keeps_the_apology():
    from hermes_cli.cli_chat_error_copy import chat_error_response

    panel = chat_error_response(_committed_reply(Platform.LOCAL), provider="anthropic")
    assert "Details: ImportError: The 'anthropic' package is required" in panel, panel

    chat = _committed_reply(Platform.TELEGRAM, owner=False)
    assert "anthropic" not in chat and "ImportError" not in chat, chat


def test_telegram_owner_sees_the_raised_cause_in_the_chat_reply():
    chat = _committed_reply(Platform.TELEGRAM, owner=True)
    assert "ImportError: The 'anthropic' package is required" in chat, chat
