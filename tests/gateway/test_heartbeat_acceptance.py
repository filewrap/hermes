"""An adapter slot is not proof a heartbeat reached execution."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli.heartbeat import HeartbeatManager, HeartbeatState, save_heartbeat
from hermes_cli.wakeups import WakeupManager
from evals.heartbeat_idle_wire import WireAdapter


@pytest.mark.asyncio
async def test_cancelled_admission_is_refunded_but_started_execution_is_not():
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42')
    key = build_session_key(source)
    watch = {key: (source, 'cancel-test')}
    started = asyncio.Event()

    async def model(**kwargs):
        started.set()
        await asyncio.Event().wait()

    runner._run_agent = model
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, SimpleNamespace(session_id="cancel-test"), key))
    runner._hmwa_prepare_turn = AsyncMock(return_value=(
        runner._PreparedTurn([], "", "check", "check", None, None), {}))
    runner._clear_session_env = lambda tokens: None

    async def handler(event):
        return await runner._handle_message_with_agent(event, source, key, 1)

    adapter.set_message_handler(handler)
    await runner._warm_goals_session_db('test')
    for cancel_before_start in (True, False):
        save_heartbeat('cancel-test', HeartbeatState(prompt='check', interval_seconds=60, created_at=1))
        await runner._heartbeat_poll_once(watch)
        task = adapter._session_tasks[key]
        if not cancel_before_start:
            await asyncio.wait_for(started.wait(), 2)
        await adapter.cancel_session_processing(key)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # task callbacks settle the exact attempt
        assert HeartbeatManager('cancel-test').state.fire_count == int(not cancel_before_start)


@pytest.mark.asyncio
async def test_runner_rejection_does_not_consume_tick_or_overwrite_replacement():
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42')
    key = build_session_key(source)
    watch = {key: (source, 'rejected-test')}
    await runner._warm_goals_session_db('test')

    async def denied(event):
        return 'not authorized'

    adapter.set_message_handler(denied)
    save_heartbeat('rejected-test', HeartbeatState(prompt='check', interval_seconds=60, created_at=1))
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    await asyncio.sleep(0)
    assert HeartbeatManager('rejected-test').state.fire_count == 0

    async def replaced(event):
        HeartbeatManager('rejected-test').set('replacement', 120)

    adapter.set_message_handler(replaced)
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    await asyncio.sleep(0)
    state = HeartbeatManager('rejected-test').state
    assert state.prompt == 'replacement' and state.fire_count == 0


class _RecordingAuthority:
    """Stand-in for a session authority: records admitted automation events."""

    def __init__(self):
        self.admitted = []

    def logical_owner(self, session_id):
        return session_id

    async def admit_automation(self, adapter, event, identity):
        self.admitted.append((event, identity))
        event._gateway_accepted = True


def _authority_poll_runner(session_id, monkeypatch):
    """Bare runner on the authority lane: session_authority set, admissions empty."""
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    adapter = WireAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    adapter.wire = []
    runner._delivery_adapter_for = lambda source: adapter
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='42', user_id='42')
    key = build_session_key(source)
    runner.session_store = SimpleNamespace(peek_session_id=lambda _key: session_id)
    runner.session_authority = _RecordingAuthority()
    monkeypatch.setattr(
        'gateway.session_persistence.authority_persistence',
        lambda authority: SimpleNamespace(admissions=SimpleNamespace(list=lambda session_id: [])),
    )
    adapter.set_message_handler(lambda event: 'ok')
    return runner, adapter, source, key, {key: (source, session_id)}


@pytest.mark.asyncio
async def test_authority_lane_fires_due_wakeup_when_no_heartbeat_set(monkeypatch):
    """The authority lane is the only lane on a running gateway (session_authority is set at
    boot), and admit_heartbeat used to consult only HeartbeatManager — a due one-shot wakeup
    was claimed by nobody and fired never, silently. Regression for the Nov 22 live silence:
    two overdue wakeups loaded fine but never fired."""
    runner, _adapter, _source, _key, watch = _authority_poll_runner('wakeup-authority-test', monkeypatch)
    await runner._warm_goals_session_db('test')
    now = time.time()
    wakeup, err, _ = WakeupManager('wakeup-authority-test').schedule(
        're-check the deploy', delay='10s', now=now - 60)
    assert err is None and wakeup is not None

    await runner._heartbeat_poll_once(watch)
    authority = runner.session_authority
    assert len(authority.admitted) == 1
    event, identity = authority.admitted[0]
    assert 're-check the deploy' in event.text
    assert json.loads(identity) == ['wakeup', 'wakeup-authority-test', wakeup.id]
    assert WakeupManager('wakeup-authority-test').load().pending == []  # claimed, not double-fireable

    await runner._heartbeat_poll_once(watch)
    assert len(authority.admitted) == 1  # second poll admits nothing


@pytest.mark.asyncio
async def test_authority_lane_heartbeat_still_wins_over_due_wakeup(monkeypatch):
    """When both a recurring heartbeat and a one-shot wakeup are due, the heartbeat fires
    first (same priority as the non-authority poll branch) and the wakeup stays pending."""
    runner, _adapter, _source, _key, watch = _authority_poll_runner('wakeup-priority-test', monkeypatch)
    await runner._warm_goals_session_db('test')
    now = time.time()
    wakeup, err, _ = WakeupManager('wakeup-priority-test').schedule(
        're-check the deploy', delay='10s', now=now - 60)
    assert err is None and wakeup is not None
    save_heartbeat('wakeup-priority-test', HeartbeatState(prompt='hb check', interval_seconds=60, created_at=1))

    await runner._heartbeat_poll_once(watch)
    authority = runner.session_authority
    assert len(authority.admitted) == 1
    event, identity = authority.admitted[0]
    assert 'hb check' in event.text  # heartbeat prompt, rendered in its template
    assert json.loads(identity)[0] == 'heartbeat'
    assert [w.id for w in WakeupManager('wakeup-priority-test').load().pending] == [wakeup.id]


@pytest.mark.asyncio
async def test_authority_lane_does_not_claim_wakeup_behind_pending_admission(monkeypatch):
    """A pending automation admission coalesces the tick: the due wakeup stays unclaimed so
    the next poll retries it instead of the claim being spent on a turn that never starts."""
    runner, _adapter, _source, _key, watch = _authority_poll_runner('wakeup-dedup-test', monkeypatch)
    await runner._warm_goals_session_db('test')
    now = time.time()
    wakeup, err, _ = WakeupManager('wakeup-dedup-test').schedule(
        're-check the deploy', delay='10s', now=now - 60)
    assert err is None and wakeup is not None
    pending_row = SimpleNamespace(payload={'native_text_v1': {'automation': {'heartbeat': 'wakeup-dedup-test'}}})
    monkeypatch.setattr(
        'gateway.session_persistence.authority_persistence',
        lambda authority: SimpleNamespace(admissions=SimpleNamespace(list=lambda session_id: [pending_row])),
    )

    await runner._heartbeat_poll_once(watch)
    assert runner.session_authority.admitted == []
    assert [w.id for w in WakeupManager('wakeup-dedup-test').load().pending] == [wakeup.id]
