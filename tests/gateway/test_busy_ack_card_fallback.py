"""Busy ack falls back to the plain reply when the busy action card fails.

The busy action card is a best-effort rich UI. If sending it raises (client
disconnected, rich send rejected, session-key lookup fails), the user must
still get the plain busy reply instead of silence: the ack is the only signal
that their message was received while the agent is busy.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_busy import GatewayBusySessionMixin


class _BusyAckHarness(GatewayBusySessionMixin):
    """Minimal stand-in for the runner side of ``_send_busy_ack_reply``."""

    def __init__(self):
        self.plain_replies: list[str] = []

    def _peek_session_state(self, _key):
        return None

    def _session_key_for_source(self, _source):
        return "agent:main:telegram:dm:123"

    async def _send_busy_reply(self, event, adapter, message):
        self.plain_replies.append(message)


def _event():
    return SimpleNamespace(
        source=SimpleNamespace(chat_id="123", user_id="123"),
        text="hello",
        message_id="1",
    )


@pytest.mark.asyncio
async def test_busy_ack_falls_back_to_plain_reply_when_card_raises():
    harness = _BusyAckHarness()
    adapter = SimpleNamespace(
        send_busy_action_card=AsyncMock(side_effect=RuntimeError("card boom")),
    )
    await harness._send_busy_ack_reply(_event(), adapter, "Hermes is busy.")
    assert harness.plain_replies == ["Hermes is busy."]
    adapter.send_busy_action_card.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_ack_uses_plain_reply_when_card_returns_false():
    harness = _BusyAckHarness()
    adapter = SimpleNamespace(send_busy_action_card=AsyncMock(return_value=False))
    await harness._send_busy_ack_reply(_event(), adapter, "Hermes is busy.")
    assert harness.plain_replies == ["Hermes is busy."]
    adapter.send_busy_action_card.assert_awaited_once()
