"""Streaming drafts and vault-code prompts on the Telethon adapter.

The fork has no python-telegram-bot: drafts go out through the Bot API
``sendMessageDraft`` method, which a Telethon/MTProto bot client never
provides, so ``send_draft`` fails soft — ``not_connected`` with no client,
``api_unavailable`` with a client that has no ``send_message_draft`` — and
never imports ``telegram``. The vault-code prompt goes out through MTProto
(``ReplyKeyboardForceReply`` + ``mtproto_rich.send_request``); the wiring
test covers consuming the reply, this file covers sending the card.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types as _tl_types

from plugins.platforms.telegram.telegram_flood_window import send_draft as send_draft_unbound
from plugins.platforms.telegram.telegram_vault_code import TelegramVaultCodeMixin


@pytest.mark.asyncio
async def test_disconnected_draft_fails_soft_without_a_telegram_import():
    result = await send_draft_unbound(SimpleNamespace(_bot=None), "123", 7, "preview", None)
    assert result.success is False
    assert result.error == "not_connected"
    assert "telegram" not in sys.modules


@pytest.mark.asyncio
async def test_bot_without_a_draft_method_fails_soft():
    # A Telethon client (or any _bot without send_message_draft) can never send a draft.
    result = await send_draft_unbound(SimpleNamespace(_bot=SimpleNamespace()), "123", 7, "preview", None)
    assert result.success is False
    assert result.error == "api_unavailable"
    assert "telegram" not in sys.modules


class _VaultAdapter(TelegramVaultCodeMixin):
    """The mixin's required adapter surface, stubbed (fake client, no network)."""

    name = "Telegram (MTProto)"

    def __init__(self):
        self._vault_code_reply_state = {}
        self._vault_code_retired_state = {}
        self.sent_requests = []

        async def _send(request):
            self.sent_requests.append(request)
            return SimpleNamespace(id=700, updates=None)

        self._client = AsyncMock(side_effect=_send)
        self.delete_message = AsyncMock(return_value=True)

    def _peer_for(self, chat_id):
        return f"peer:{chat_id}"

    @staticmethod
    def _reply_top_target(metadata):
        return None

    @staticmethod
    def _sent_message_id(response, *, random_id=None, peer=None, topic_id=None):
        return None


@pytest.mark.asyncio
async def test_vault_code_prompt_goes_out_as_a_force_reply_card_over_mtproto():
    adapter = _VaultAdapter()
    submitted = []
    result = await adapter.send_vault_code_prompt(
        "123", "example.com", "6-digit code", user_id="42",
        submit=lambda code: submitted.append(code) or True, timeout_s=60)
    assert result.success is True
    assert result.message_id == "700"
    assert len(adapter.sent_requests) == 1
    request = adapter.sent_requests[0]
    assert isinstance(request.reply_markup, _tl_types.ReplyKeyboardForceReply)
    assert request.reply_markup.selective is True
    assert ("123", "700") in adapter._vault_code_replies()
    assert "telegram" not in sys.modules
