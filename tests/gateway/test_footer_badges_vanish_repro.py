"""Repro: footer ON + beta ON renders NOTHING on a real (non-streamed) final send.

Live 2026-11-24: /footer on, beta footer on in /settings, "Hey" -> reply
"Hey" with no footer at all — no text footer, no badge buttons. The text
footer was suppressed (badges should replace it) and the badges never
rendered, because they were attached to the turn's event and the final send
never saw them. The fix puts the badges in the footer itself: the footer
HTML (line + badge row) is part of the message text, so the final send
carries them by construction — no event, metadata, or hand-over to lose
them on.
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from tests.fixtures.storage_holder import raw_holder
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle
from persistence_assembly import profile_scope


_BADGE_BUTTON_RE = re.compile(
    r'<tg-button[^>]*data="([^"]*)"[^>]*>([^<]*)</tg-button>')


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_scope", lambda: profile_scope(home=home))
    monkeypatch.setattr(dl, "_owner_stamp", lambda: (os.getpid(), 202))
    monkeypatch.setattr(dl, "ledger_enabled", lambda config=None: True)
    yield


def _source():
    return SimpleNamespace(platform=Platform.TELEGRAM, chat_id="123", thread_id=None,
                           chat_type="dm", parent_chat_id=None, user_id="6802929470")


def _real_adapter():
    """Real TelethonAdapter whose MTProto client is a recorder (like
    tests/plugins/platforms/telegram/test_footer_badges.py)."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(id=777)

    adapter._client = Client()
    runner = MagicMock()
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._schedule_flood_redelivery = MagicMock(return_value=187.0)
    adapter.gateway_runner = runner
    adapter.edit_message = AsyncMock(
        return_value=SendResult(success=False, error="message to edit not found"))
    return adapter


BADGES = [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna"),
          ("9%", "fb:c:8000|272000"),
          ("~", "fb:p:~")]


@pytest.mark.asyncio
async def test_nonstreamed_final_send_with_badges_carries_the_rich_row():
    """The footer HTML (badges nested inline in the footer paragraph)
    rides the final message text — the badges are in the footer, not on
    the event — so the final send renders them as literal <tg-button>
    pills, never a keyboard. Regression: live they vanished."""
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    adapter = _real_adapter()
    mixin = GatewayTurnPersistenceMixin()
    event = MessageEvent(text="Hey", source=_source(), message_id="101")
    # What the turn does: the footer HTML rides on the reply text itself.
    response = mixin._hmwa_attach_footer(
        event, {}, "Hey \U0001f60a I'm here.", "gpt-6-luna · 9% · ~", list(BADGES), False)
    assert not hasattr(event, "_footer_badges")
    assert "<tg-button-row" not in str(response)
    assert str(response).endswith('</p>')
    await adapter._send_final_text(
        event, "agent:main:telegram:dm:123", str(response),
        {}, False, 0, lambda _r: None)
    assert len(adapter._client.requests) == 1
    request = adapter._client.requests[0]
    assert request.reply_markup is None, "footer badges must not ride a keyboard"
    assert type(request.rich_message).__name__ == "InputRichMessageHTML", (
        "badges must ride as literal <tg-button> pills, not a PageBlockButtonRow")
    html = request.rich_message.html
    assert "<tg-button-row" not in html, "badge row block must not reach the payload"
    assert [label for _, label in _BADGE_BUTTON_RE.findall(html)] == [
        "gpt-6-luna", "9%", "~"]
    assert [data for data, _ in _BADGE_BUTTON_RE.findall(html)] == [
        "fb:m:openrouter|openrouter/gpt-6-luna", "fb:c:8000|272000", "fb:p:~"]
