"""Footer badges ride the message the user sees, in the footer text itself.

Live 2026-11-24: footer ON + beta ON, the reply shows the footer line and
no badges, ever — the badges were attached to the turn's event and the
final send never saw them, because the turn runs on a restored event while
the reply is sent from the original inbound event. The fix puts the badges
in the footer itself: the footer HTML (line + badge row) is part of the
message text, so there is no event, metadata, or hand-over for the badges
to get lost on. These tests pin that: the sent text carries the badge row,
at both delivery seams (the live DM path and the busy follow-up path).
Real GatewayRunner, adapter, SessionAuthority, drain and
``execute_admission`` over SQLite; only the agent turn is a stand-in, and
it attaches the footer the way ``_hmwa_attach_footer`` does.
"""
import asyncio

import pytest

from tests.gateway.test_shared_busy_receive_loop import Adapter, _statuses, _wait_for

_BADGES = [("model", "fb:m:agentrouter|model"), ("context", "fb:c:1|2"), ("cwd", "fb:p:~")]
_FOOTER_LINE = "model · 1% · ~"


class _MetadataAdapter(Adapter):
    """The shared test adapter, also recording the metadata each send carried."""

    def __init__(self):
        super().__init__()
        self.sent_metadata = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent_metadata.append(metadata)
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)


async def _live(monkeypatch, *, badges, gate=None):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_results import execution_result
    from gateway.run_turn_persistence import GatewayTurnPersistenceMixin

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', 'fixture-user')
    runner = GatewayRunner(GatewayConfig())
    adapter = _MetadataAdapter()
    adapter.gateway_runner = runner  # as GatewayRunner._create_adapter binds every adapter
    runner.adapters = {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='badges-chat', chat_type='dm', user_id='fixture-user')
    mixin = GatewayTurnPersistenceMixin()

    async def turn(event, source, quick_key, run_generation):
        if event.text == 'one' and gate is not None:
            await gate.wait()
        response = 'reply to ' + event.text
        if badges:
            # What _hmwa_attach_footer does: the footer HTML (line + badge
            # row) rides on the reply text itself.
            response = mixin._hmwa_attach_footer(
                event, {}, response, _FOOTER_LINE, list(_BADGES), False)
        execution_result.get()['result'] = {'final_response': response, 'messages': []}
        return response
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    return authority, adapter, source


@pytest.mark.asyncio
async def test_dm_reply_carries_the_badges_in_its_text(monkeypatch):
    from gateway.platforms.event import MessageEvent

    authority, adapter, source = await _live(monkeypatch, badges=True)
    await adapter.handle_message(MessageEvent(text='one', source=source, message_id='m1'))
    await _wait_for(lambda: adapter.sent == ['<p>reply to one</p>\n\n<p><tg-button type="callback_data" style="primary" data="fb:m:agentrouter|model">model</tg-button> · <tg-button type="callback_data" style="primary" data="fb:c:1|2">context</tg-button> · <tg-button type="callback_data" style="primary" data="fb:p:~">cwd</tg-button></p>'], 15)
    assert 'footer_badges' not in (adapter.sent_metadata[0] or {})
    assert '<tg-button-row' not in adapter.sent[0]


@pytest.mark.asyncio
async def test_dm_reply_without_badges_carries_no_row(monkeypatch):
    from gateway.platforms.event import MessageEvent

    authority, adapter, source = await _live(monkeypatch, badges=False)
    await adapter.handle_message(MessageEvent(text='one', source=source, message_id='m1'))
    await _wait_for(lambda: adapter.sent == ['reply to one'], 15)
    assert 'footer_badges' not in (adapter.sent_metadata[0] or {})
    assert '<tg-button-row' not in adapter.sent[0]


@pytest.mark.asyncio
async def test_busy_followup_reply_carries_the_badges_in_its_text(monkeypatch):
    from gateway.platforms.event import MessageEvent

    gate = asyncio.Event()
    authority, adapter, source = await _live(monkeypatch, badges=True, gate=gate)
    await adapter.handle_message(MessageEvent(text='one', source=source, message_id='m1'))
    await _wait_for(lambda: _statuses(authority).get('one') == 'started')
    await asyncio.wait_for(adapter.handle_message(MessageEvent(text='two', source=source, message_id='m2')), 5)
    gate.set()
    await _wait_for(lambda: len(adapter.sent) == 2, 15)
    for sent in adapter.sent:
        assert '<tg-button type="callback_data"' in sent, (
            'the busy follow-up delivered from a copy made before the turn must still carry the badges — they are in the text, not on the event')
        assert '<tg-button-row' not in sent
        assert 'footer_badges' not in (adapter.sent_metadata[adapter.sent.index(sent)] or {})
