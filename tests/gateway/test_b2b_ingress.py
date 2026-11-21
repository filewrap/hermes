"""Behaviour contracts for the B2B gateway ingress seam: an event the
Telegram adapter already admitted as a peer turn skips the human
authorization check, and a live secure talk skips the loop guard for that
talk only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.bot_loop_guard import BotLoopGuard, BotLoopGuardSettings
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource

CHAT = "dm_peer"
PEER = "peer_bot"
HUMAN = "alice"


def _guard(**overrides):
    settings = BotLoopGuardSettings(**{
        "max_events": 2, "window_seconds": 300, "cooldown_seconds": 600,
        "max_hops": 100, **overrides})
    return BotLoopGuard(settings=lambda: settings, clock=lambda: 1_000.0)


@pytest.fixture
def runner():
    runner = object.__new__(GatewayRunner)
    runner._scale_to_zero_note_real_inbound = lambda: None

    async def _passthrough(event, source):
        return event

    runner._hm_pre_gateway_dispatch_hook = _passthrough
    runner._is_user_authorized_for_source = lambda source, **kw: False
    runner._bot_loop_guard = _guard()
    runner._adapter_profile_for_source = lambda source: ""
    return runner


def _event(user, text, is_bot, **flags):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id=CHAT, chat_type="dm",
                           user_id=user, is_bot=is_bot)
    event = MessageEvent(text=text, message_id=text, source=source)
    for name, value in flags.items():
        setattr(event, name, value)
    return event


@pytest.mark.asyncio
async def test_adapter_admitted_peer_turn_skips_human_authorization(runner):
    assert await runner._hm_admit_event(_event(PEER, "hello", True)) is None
    assert await runner._hm_admit_event(
        _event(PEER, "hello", True, _b2b_admitted=True)) is not None


@pytest.mark.asyncio
async def test_secure_talk_skips_the_loop_guard_for_that_talk_only(runner):
    assert await runner._hm_admit_event(_event(PEER, "one", True, _b2b_admitted=True)) is not None
    assert await runner._hm_admit_event(_event(PEER, "two", True, _b2b_admitted=True)) is not None
    assert await runner._hm_admit_event(_event(PEER, "three", True, _b2b_admitted=True)) is None
    assert await runner._hm_admit_event(
        _event(PEER, "secure", True, _b2b_admitted=True, _b2b_secure=True)) is not None
    assert await runner._hm_admit_event(_event(PEER, "four", True, _b2b_admitted=True)) is None


@pytest.mark.asyncio
async def test_human_messages_still_need_authorization(runner):
    assert await runner._hm_admit_event(_event(HUMAN, "hi", False)) is None
