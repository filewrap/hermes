"""A successful busy redirect re-anchors the running turn's reply to the redirecting message.

The turn's reply anchor and ledger identity are bound to the message that OPENED it and the
final send is bracketed against that event, so before this a redirected turn answered B while
its reply still quoted A (#115001, Repro A). Both redirect entry points — the interrupt-mode
busy path and the priority path — must move the anchor; a refused redirect must not.
"""

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import _reply_anchor_for_event
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


class Receiver:
    _supports_active_turn_redirect = True

    def __init__(self, accept=True):
        self.accept = accept

    def redirect(self, text):
        return self.accept

    def steer(self, text):
        return self.accept

    def interrupt(self, text=None, **kwargs):
        self.interrupted_with = text
        return True


def _running_group_turn(runner, key, receiver):
    """A running turn in a Telegram group, opened by a trigger-word message."""
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-100123", user_id="u1", chat_type="group")
    opening = MessageEvent(text="Emilia hey", source=source, message_id="A")
    ctx = TurnContext(session_key=key, event_message_id="A", inbound_message_id="A")
    turn = runner._session_state(key).turn
    turn.agent, turn.event, turn.ctx = receiver, opening, ctx
    # A reply to the bot's own message: the follow-up that folds into the turn.
    followup = MessageEvent(
        text="Hiee", source=source, message_id="B",
        reply_to_message_id="BOT", reply_to_is_own_message=True)
    return opening, ctx, followup, source


def _running_turn(runner, key, receiver):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", user_id="u1", chat_type="dm")
    opening = MessageEvent(text="What is the weather in Shanghai?", source=source, message_id="A")
    ctx = TurnContext(session_key=key, event_message_id="A", inbound_message_id="A")
    turn = runner._session_state(key).turn
    turn.agent, turn.event, turn.ctx = receiver, opening, ctx
    redirecting = MessageEvent(text="What day is tomorrow?", source=source, message_id="B")
    return opening, ctx, redirecting, source


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["busy_interrupt", "priority"])
async def test_successful_redirect_moves_the_turn_reply_anchor_to_the_redirecting_message(route):
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, redirecting, source = _running_turn(runner, "key", receiver)

    if route == "priority":
        await runner._hm_busy_interrupt(redirecting, source, receiver, "key")
    else:
        outcome = await runner._resolve_busy_steer_or_redirect(redirecting, "key", "interrupt", receiver)
        assert outcome.redirected is True

    # The final send is bracketed against the OPENING event: it now quotes B and is ledgered as B.
    assert _reply_anchor_for_event(opening) == "B"
    assert opening.ledger_message_id == "B"
    # The queued-first-response lane reads the TurnContext anchor: it follows too.
    assert (ctx.event_message_id, ctx.inbound_message_id) == ("B", "B")
    # A's own identity is untouched (the ledger keys on ledger_message_id, not on this).
    assert opening.message_id == "A"


@pytest.mark.asyncio
async def test_refused_or_foreign_redirect_leaves_the_anchor_on_the_opening_message():
    runner = GatewayRunner(config=GatewayConfig())
    refusing = Receiver(accept=False)
    opening, ctx, redirecting, _ = _running_turn(runner, "key", refusing)
    outcome = await runner._resolve_busy_steer_or_redirect(redirecting, "key", "interrupt", refusing)
    assert outcome.redirected is False
    assert _reply_anchor_for_event(opening) == "A" and opening.ledger_message_id is None
    assert ctx.event_message_id == "A"

    # A redirect that lands on an agent which no longer owns the slot (a newer turn claimed it)
    # must not re-anchor the newer turn.
    displaced = Receiver()
    opening2, ctx2, redirecting2, _ = _running_turn(runner, "key2", Receiver())
    assert await runner._resolve_busy_steer_or_redirect(redirecting2, "key2", "interrupt", displaced)
    assert _reply_anchor_for_event(opening2) == "A" and ctx2.event_message_id == "A"


@pytest.mark.asyncio
async def test_plain_interrupt_fallthrough_moves_the_turn_reply_anchor_to_the_follow_up():
    """A follow-up folded into a running turn through the plain interrupt() path must
    re-anchor the turn's reply to the follow-up, exactly as a successful redirect does.

    The redirect is attempted first and refuses when no model request is live (the
    opener's response already landed, the turn is still busy in post-delivery); the
    fallthrough interrupt() then folds the follow-up into the same turn. Before this
    the turn answered the follow-up while its reply still quoted the opener: in a
    group, replying to the bot's message got a reply quoting the previous
    trigger-word message instead of the reply itself.
    """
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver(accept=False)  # redirect() refuses: no live model request
    opening, ctx, followup, source = _running_group_turn(runner, "key", receiver)

    await runner._hm_busy_interrupt(followup, source, receiver, "key")

    assert getattr(receiver, "interrupted_with", None) == "Hiee"
    # The final send is bracketed against the OPENING event: it now quotes B and is ledgered as B.
    assert _reply_anchor_for_event(opening) == "B"
    assert opening.ledger_message_id == "B"
    assert (ctx.event_message_id, ctx.inbound_message_id) == ("B", "B")
    assert opening.message_id == "A"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route", ["busy_interrupt", "priority_interrupt", "busy_steer", "priority_steer", "slash_steer"])
async def test_a_turn_that_takes_in_an_addressed_message_keeps_the_silence_fallback(route):
    """Redirect and steer fold a new message into the running turn, which then answers it too: if
    that message was addressed to the bot, a bare silence marker must not end the turn silently."""
    runner = GatewayRunner(config=GatewayConfig())
    receiver = Receiver()
    opening, ctx, incoming, source = _running_turn(runner, "key", receiver)
    opening.reply_expected, incoming.reply_expected = False, True

    if route == "priority_interrupt":
        await runner._hm_busy_interrupt(incoming, source, receiver, "key")
    elif route == "priority_steer":
        runner._hm_busy_steer(incoming, receiver, "key")
    elif route == "slash_steer":
        incoming.text = "/steer " + incoming.text
        assert (await runner._busy_steer_command(incoming, "key", source)).startswith("⏩")
    else:
        outcome = await runner._resolve_busy_steer_or_redirect(incoming, "key", route[5:], receiver)
        assert outcome.redirected or outcome.steered

    assert (opening.reply_expected, ctx.reply_expected) == (True, True)
