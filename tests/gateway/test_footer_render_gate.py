"""Footer render gate: the /footer master switch and the footer-badges beta,
plus no footer of any kind on a failed turn.

The rule (locked 2026-11-24): badges render if and only if /footer is ON
(``display.runtime_footer.enabled``) AND the beta is ON
(``platforms.telegram.footer_badges``). The footer line always rides on the
reply text when /footer is ON — badges ride as a rich button row on top of it,
never as its only carrier: a row the server accepts but never renders
must not leave an enabled footer as nothing. Nothing renders when /footer
is OFF. A failed turn gets neither badges nor a text footer: its error text
already carries the ``Model:`` line inside it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import gateway.run as run_mod
from gateway.platforms.base import MessageEvent
from gateway.run_turn import GatewayTurnMixin
from gateway.runtime_footer import FooteredReply
from gateway.session import Platform, SessionSource


class _Runner(GatewayTurnMixin):
    pass


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm",
                         user_id="6802929470", is_bot=False)


def _result(**overrides):
    result = {"model": "openrouter/gpt-6-luna", "provider": "openrouter",
              "last_prompt_tokens": 8000, "context_length": 272000,
              "requested_model": "openrouter/gpt-6-luna", "served_model": None}
    result.update(overrides)
    return result


def _config(footer_on, beta_on):
    cfg = {"display": {"runtime_footer": {"enabled": footer_on}}}
    cfg["platforms"] = {"telegram": {"footer_badges": beta_on}}
    return cfg


@pytest.mark.parametrize("footer_on,beta_on,want_line,want_badges", [
    (False, False, "", []),
    (False, True, "", []),
    (True, False, "gpt-6-luna · 3%", []),
    (True, True, "gpt-6-luna · 3%", ["gpt-6-luna", "3%"]),
], ids=["off/off", "off/beta-on", "on/beta-off", "on/beta-on"])
def test_footer_render_gate_all_four_combinations(footer_on, beta_on, want_line, want_badges):
    """Badges iff footer ON and beta ON; text footer iff footer ON and beta OFF; nothing iff OFF."""
    runner = _Runner()
    with patch.object(run_mod, "_load_gateway_config", return_value=_config(footer_on, beta_on)):
        line = runner._hmwa_runtime_footer_line(_result(), _source(), 3.0)
        badges = runner._hmwa_runtime_footer_badges(_result(), _source(), 3.0)
    assert line == want_line
    assert [label for label, _ in badges] == want_badges


def _event():
    return MessageEvent(text="Say gm", message_id="m1", source=_source())


def test_failed_turn_gets_no_badges_and_no_text_footer():
    """A failed turn's error text already carries the Model: line inside it;
    no badge buttons and no text footer belong below it."""
    runner = _Runner()
    event = _event()
    response = runner._hmwa_attach_footer(
        event, _result(failed=True, error="boom"), "turn failed: boom",
        "gpt-6-luna · 3%", [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")], False)
    assert response == "turn failed: boom"
    assert not isinstance(response, FooteredReply)
    assert getattr(event, "_footer_badges", None) in (None, [])


def test_successful_turn_with_badges_sets_them_and_carries_the_line():
    """Badges ride IN the footer text — the badges nested inline in the
    footer paragraph, the same form the settings patches use — and the
    body converts to HTML so the pills do not flip it to literal
    Markdown. No <tg-button-row>: a row block renders as big
    keyboard-sized buttons. Nothing is set on the event."""
    runner = _Runner()
    event = _event()
    badges = [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")]
    response = runner._hmwa_attach_footer(
        event, _result(), "gm", "gpt-6-luna · 3%", badges, False)
    assert isinstance(response, FooteredReply)
    assert str(response) == (
        "<p>gm</p>\n\n<p>"
        '<tg-button type="callback_data" style="primary" '
        'data="fb:m:openrouter|openrouter/gpt-6-luna">gpt-6-luna</tg-button></p>')
    assert response.runtime_footer == (
        "<p>"
        '<tg-button type="callback_data" style="primary" '
        'data="fb:m:openrouter|openrouter/gpt-6-luna">gpt-6-luna</tg-button></p>')
    assert not hasattr(event, "_footer_badges")


def test_badges_with_empty_line_leave_text_plain():
    """Badges with no footer line to carry: the reply stays plain — no empty
    footer is appended, and nothing is set on the event."""
    runner = _Runner()
    event = _event()
    badges = [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")]
    response = runner._hmwa_attach_footer(
        event, _result(), "gm", "", badges, False)
    assert response == "gm"
    assert not isinstance(response, FooteredReply)
    assert not hasattr(event, "_footer_badges")


def test_successful_turn_without_badges_wraps_the_text_footer():
    """Footer ON + beta OFF: the plain line rides on the reply text."""
    runner = _Runner()
    event = _event()
    response = runner._hmwa_attach_footer(event, _result(), "gm", "gpt-6-luna · 3%", [], False)
    assert isinstance(response, FooteredReply)
    assert str(response) == "gm\n\ngpt-6-luna · 3%"
    assert response.runtime_footer == "gpt-6-luna · 3%"
    assert getattr(event, "_footer_badges", None) in (None, [])


def test_streamed_turn_with_badges_keeps_text_plain_for_the_trailing_send():
    """A streamed turn never gets the text footer inlined (the trailing send
    carries it); nothing is set on the event."""
    runner = _Runner()
    event = _event()
    badges = [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")]
    response = runner._hmwa_attach_footer(
        event, _result(already_sent=True), "gm", "gpt-6-luna · 3%", badges, False)
    assert response == "gm"
    assert not hasattr(event, "_footer_badges")


def test_streamed_turn_without_badges_gets_no_inlined_footer():
    """A streamed turn without badges gets no text footer either (it would
    duplicate the trailing send)."""
    runner = _Runner()
    event = _event()
    response = runner._hmwa_attach_footer(
        event, _result(already_sent=True), "gm", "gpt-6-luna · 3%", [], False)
    assert response == "gm"
    assert not isinstance(response, FooteredReply)


def _trailing_runner(badges):
    runner = _Runner()
    event = SimpleNamespace()
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    return runner, event, adapter, badges


@pytest.mark.asyncio
async def test_trailing_footer_sends_badges_with_the_footer_line():
    """A streamed turn's trailing footer is the footer HTML itself: the
    badges nested inline in one paragraph, the same form the settings
    patches use. No <tg-button-row>: a row block renders as big
    keyboard-sized buttons."""
    runner, event, adapter, badges = _trailing_runner(
        [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna"), ("3%", "fb:c:8000|272000")])
    await runner._hmwa_send_trailing_footer(
        adapter, SimpleNamespace(chat_id="123"), event, "gpt-6-luna · 3%",
        {"thread": "t"}, False, False, footer_badges=badges)
    adapter.send.assert_awaited_once()
    args, kwargs = adapter.send.await_args
    assert args[0] == "123"
    assert args[1] == (
        "<p>"
        '<tg-button type="callback_data" style="primary" '
        'data="fb:m:openrouter|openrouter/gpt-6-luna">gpt-6-luna</tg-button> · '
        '<tg-button type="callback_data" style="primary" '
        'data="fb:c:8000|272000">3%</tg-button></p>')
    assert "footer_badges" not in kwargs["metadata"]
    assert kwargs["metadata"]["thread"] == "t"


@pytest.mark.asyncio
async def test_trailing_footer_without_badges_sends_the_plain_line():
    """Beta OFF: the trailing send is exactly the plain footer line."""
    runner, event, adapter, badges = _trailing_runner([])
    await runner._hmwa_send_trailing_footer(
        adapter, SimpleNamespace(chat_id="123"), event, "gpt-6-luna · 3%",
        {"thread": "t"}, False, False, footer_badges=badges)
    adapter.send.assert_awaited_once()
    args, kwargs = adapter.send.await_args
    assert args[1] == "gpt-6-luna · 3%"
    assert "footer_badges" not in kwargs["metadata"]


@pytest.mark.asyncio
async def test_trailing_footer_failed_send_does_not_raise():
    """A refused trailing send is best-effort: it must not fail the turn.
    ``adapter.send`` returns a failed result instead of raising, so the
    failure is checked on the result, not the exception. A badged send that
    fails is retried once without the badges (so two calls here); the
    plain retry failing too still raises nothing."""
    runner, event, adapter, badges = _trailing_runner(
        [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")])
    adapter.send.return_value = SimpleNamespace(success=False, error="boom")
    await runner._hmwa_send_trailing_footer(
        adapter, SimpleNamespace(chat_id="123"), event, "gpt-6-luna · 3%",
        {}, False, False, footer_badges=badges)
    assert adapter.send.await_count == 2
    _args, kwargs = adapter.send.await_args
    assert "footer_badges" not in kwargs["metadata"]


@pytest.mark.asyncio
async def test_trailing_footer_badged_send_refused_falls_back_to_plain_line():
    """Footer ON + beta ON, streamed turn: the badged trailing send is
    refused (failed ``SendResult``) — the same footer line must go out
    again WITHOUT the badge row. The user must never get nothing when the
    footer is ON."""
    runner, event, _adapter, badges = _trailing_runner(
        [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")])

    async def _send(chat_id, content, metadata=None):
        if "<tg-button" in str(content or ""):
            return SimpleNamespace(success=False, error="BUTTON_DATA_INVALID")
        return SimpleNamespace(success=True)

    adapter = SimpleNamespace(send=AsyncMock(side_effect=_send))
    await runner._hmwa_send_trailing_footer(
        adapter, SimpleNamespace(chat_id="123"), event, "gpt-6-luna · 3%",
        {"thread": "t"}, False, False, footer_badges=badges)
    assert adapter.send.await_count == 2
    args, kwargs = adapter.send.await_args
    assert args[1] == "gpt-6-luna · 3%"
    assert kwargs["metadata"] == {"thread": "t"}
    assert "footer_badges" not in kwargs["metadata"]


@pytest.mark.asyncio
@pytest.mark.parametrize("footer_line,intentional_silence,footer_captioned", [
    ("", False, False), ("gpt-6-luna · 3%", True, False), ("gpt-6-luna · 3%", False, True),
], ids=["no footer line", "intentional silence", "footer on caption"])
async def test_trailing_footer_skips_when_nothing_is_owed(footer_line, intentional_silence, footer_captioned):
    runner, event, adapter, badges = _trailing_runner(
        [("gpt-6-luna", "fb:m:openrouter|openrouter/gpt-6-luna")])
    await runner._hmwa_send_trailing_footer(
        adapter, SimpleNamespace(chat_id="123"), event, footer_line,
        {}, intentional_silence, footer_captioned, footer_badges=badges)
    adapter.send.assert_not_awaited()
