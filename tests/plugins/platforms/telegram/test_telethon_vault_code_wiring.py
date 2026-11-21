"""Telethon adapter takes vault one-time-code replies off the inbound path.

A reply to a ForceReply code card (or any private-chat text while one is live)
must be deleted, submitted, and never reach batching — the adapter calls
``consume_vault_code_reply`` in ``_on_new_message`` before
``_process_inbound_serialized``. Unrelated text still flows through.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
from plugins.platforms.telegram.telegram_vault_code import (
    TelegramVaultCodeMixin,
    _CodeReply,
)


class _VaultAdapter(TelegramVaultCodeMixin):
    """The mixin's required adapter surface, stubbed (no client, no network)."""

    name = "Telegram (MTProto)"
    _client = AsyncMock()
    _bot_id = 999

    def __init__(self):
        self._vault_code_reply_state = {}
        self._vault_code_retired_state = {}
        self.delete_message = AsyncMock(return_value=True)
        self._process_inbound_serialized = AsyncMock()

    def _peer_for(self, chat_id):
        return f"peer:{chat_id}"

    @staticmethod
    def _reply_top_target(metadata):
        return None

    @staticmethod
    def _sent_message_id(response, *, random_id=None, peer=None, topic_id=None):
        return None


def _card(adapter, *, user_id="111", chat_id="111", card_id="700"):
    submitted = []
    adapter._vault_code_reply_state[(chat_id, card_id)] = _CodeReply(
        submit=lambda code: submitted.append(code) or True,
        user_id=user_id, chat_id=chat_id, prompt_message_id=card_id,
        prompt_text="card", expires_at=time.monotonic() + 60,
    )
    return submitted


def _event(*, text, sender_id=111, chat_id="111", reply_to=None, message_id=900):
    message = SimpleNamespace(
        text=text, sender_id=sender_id, chat_id=chat_id, id=message_id,
        grouped_id=None, reply_to_msg_id=reply_to, is_private=True,
    )
    return SimpleNamespace(message=message)


@pytest.mark.asyncio
async def test_code_reply_is_consumed_before_processing():
    adapter = _VaultAdapter()
    submitted = _card(adapter)
    await TelethonAdapter._on_new_message(adapter, _event(text=" 954145 ", reply_to=700))
    assert submitted == ["954145"]
    adapter.delete_message.assert_awaited_once_with("111", "900")
    adapter._process_inbound_serialized.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_text_still_reaches_processing():
    adapter = _VaultAdapter()
    await TelethonAdapter._on_new_message(adapter, _event(text="hello"))
    adapter._process_inbound_serialized.assert_awaited_once()
    adapter.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_chat_text_answers_live_card_without_reply():
    adapter = _VaultAdapter()
    submitted = _card(adapter)
    await TelethonAdapter._on_new_message(adapter, _event(text="954145"))
    assert submitted == ["954145"]
    adapter._process_inbound_serialized.assert_not_awaited()
