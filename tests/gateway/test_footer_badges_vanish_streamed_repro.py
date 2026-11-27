"""Repro (streamed lane): footer ON + beta ON renders NOTHING.

Live 2026-11-24: /footer on, beta footer on in /settings, "Hey" -> reply
"Hey" with no footer at all. The non-streamed final-send path carries the
badges (proven by the sibling repro), so the streamed lane is the suspect:
the trailing footer must send the footer line with the rich badge row.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


_BADGE_BUTTON_RE = re.compile(
    r'<tg-button[^>]*data="([^"]*)"[^>]*>([^<]*)</tg-button>')


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm",
                         user_id="6802929470", is_bot=False)


def _real_adapter():
    """Real TelethonAdapter whose MTProto client is a recorder."""
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))

    class Client:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(id=777)

    adapter._client = Client()
    return adapter


BADGES = [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna"),
          ("9%", "fb:c:8000|272000"),
          ("~", "fb:p:~")]


@pytest.mark.asyncio
async def test_streamed_turn_with_badges_sends_trailing_footer_with_rich_row():
    """A streamed turn with badges on the event must send the trailing footer
    line WITH the rich badge row. Regression: live it sent nothing."""
    from gateway.run import GatewayRunner

    adapter = _real_adapter()
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._should_send_voice_reply = MagicMock(return_value=False)
    event = MessageEvent(text="Hey", source=_source(), message_id="101")
    session_entry = SimpleNamespace(session_id="s1")
    agent_result = {"already_sent": True, "model": "openrouter/gpt-6-luna",
                    "provider": "openrouter", "last_prompt_tokens": 8000,
                    "context_length": 272000}
    out = await runner._hmwa_deliver_turn_response(
        event, _source(), session_entry, "agent:main:telegram:dm:123", 1,
        agent_result, [], "Hey \U0001f60a I'm here.",
        "gpt-6-luna · 9% · ~", False, footer_badges=list(BADGES))
    assert out is None  # streamed body already delivered; no duplicate send
    assert len(adapter._client.requests) == 1, (
        "trailing footer was never sent: footer ON + beta ON renders nothing")
    request = adapter._client.requests[0]
    assert "gpt-6-luna" in request.message
    assert request.reply_markup is None, "footer badges must not ride a keyboard"
    assert type(request.rich_message).__name__ == "InputRichMessageHTML", (
        "badges must ride as literal <tg-button> pills, not a PageBlockButtonRow")
    html = request.rich_message.html
    assert "<tg-button-row" not in html, "trailing footer must not carry a badge row block"
    assert [label for _, label in _BADGE_BUTTON_RE.findall(html)] == [
        "gpt-6-luna", "9%", "~"]


@pytest.mark.asyncio
async def test_streamed_turn_with_badges_falls_back_to_plain_footer_when_badged_send_fails():
    """Footer ON + beta ON, streamed turn, live shape: the badged trailing
    send fails server-side — the footer line must still land as plain text.
    Regression: the failed send was swallowed at DEBUG and the user got
    nothing at all."""
    from gateway.run import GatewayRunner

    adapter = _real_adapter()

    class FailingFirstClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise RuntimeError("boom")
            return SimpleNamespace(id=778)

    adapter._client = FailingFirstClient()
    runner = object.__new__(GatewayRunner)
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._should_send_voice_reply = MagicMock(return_value=False)
    event = MessageEvent(text="Hey", source=_source(), message_id="101")
    session_entry = SimpleNamespace(session_id="s1")
    agent_result = {"already_sent": True, "model": "openrouter/gpt-6-luna",
                    "provider": "openrouter", "last_prompt_tokens": 8000,
                    "context_length": 272000}
    out = await runner._hmwa_deliver_turn_response(
        event, _source(), session_entry, "agent:main:telegram:dm:123", 1,
        agent_result, [], "Hey \U0001f60a I'm here.",
        "gpt-6-luna · 9% · ~", False, footer_badges=list(BADGES))
    assert out is None
    assert len(adapter._client.requests) == 2
    first, plain = adapter._client.requests
    assert "<tg-button" in (getattr(first.rich_message, "html", None) or ""), (
        "the first trailing send must carry the badges")
    assert "<tg-button" not in (getattr(plain.rich_message, "html", None) or ""), (
        "footer ON + beta ON rendered nothing: the badged send failed "
        "and no plain-text fallback was sent")
    assert "gpt-6-luna" in plain.message
