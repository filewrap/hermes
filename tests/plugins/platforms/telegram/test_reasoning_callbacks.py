"""Behaviour contracts for the ``hm:reasoning:*`` callback dispatcher.

The /reasoning rich cards (see ``tests/gateway/test_reasoning_rich_cards.py``)
carry three nested-button payloads; until this dispatcher existed every tap
answered "This Telegram action is unavailable." Each payload re-dispatches
the typed command through the adapter's normal inbound path, exactly like
the ``hm:resume:`` tap re-dispatches ``/resume <id>``:

- ``hm:reasoning:pick``            -> ``/reasoning`` (effort picker, or the
                                      settings card where the adapter has none)
- ``hm:reasoning:toggle-display``  -> ``/reasoning show|hide`` (flips what the
                                      tapped card shows)
- ``hm:reasoning:scope``           -> ``/reasoning reset`` when a session
                                      override exists, else a toast that the
                                      scope is already global
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

SESSION_KEY = "agent:main:telegram:dm:12345"


def _source(user_id="12345", chat_id="12345"):
    return SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id=str(chat_id),
        chat_type="dm",
        user_id=str(user_id),
        user_name="testuser",
        thread_id=None,
    )


def _callback_event(message_id=55):
    event = MagicMock()
    event.message_id = message_id
    event.answer = AsyncMock()
    return event


def _fake_runner(*, show_reasoning=False, overrides=None):
    """A runner double exposing only the state the dispatcher reads."""
    return SimpleNamespace(
        _show_reasoning=show_reasoning,
        _session_reasoning_overrides=dict(overrides or {}),
        _session_key_for_source=lambda source: SESSION_KEY,
        _normalize_source_for_session_key=lambda source: source,
    )


@pytest.fixture
def fake_adapter():
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter.config = SimpleNamespace(extra={})
    adapter.gateway_runner = _fake_runner()
    adapter.handle_message = AsyncMock()
    adapter._source_session_key = lambda source: SESSION_KEY
    return adapter


class TestPickDispatch:
    @pytest.mark.asyncio
    async def test_pick_redispatches_bare_reasoning(self, fake_adapter):
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:pick")

        event.answer.assert_called_once_with()
        fake_adapter.handle_message.assert_called_once()
        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert isinstance(dispatched, MessageEvent)
        assert dispatched.text == "/reasoning"
        assert dispatched.message_type == MessageType.COMMAND
        assert dispatched.source is source
        assert dispatched.user_id == source.user_id


class TestToggleDisplayDispatch:
    @pytest.mark.asyncio
    async def test_toggle_display_hides_when_showing(self, fake_adapter):
        fake_adapter.gateway_runner._show_reasoning = True
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:toggle-display")

        event.answer.assert_called_once_with()
        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert dispatched.text == "/reasoning hide"

    @pytest.mark.asyncio
    async def test_toggle_display_shows_when_hidden(self, fake_adapter):
        fake_adapter.gateway_runner._show_reasoning = False
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:toggle-display")

        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert dispatched.text == "/reasoning show"

    @pytest.mark.asyncio
    async def test_toggle_display_without_runner_answers_unavailable(self, fake_adapter):
        fake_adapter.gateway_runner = None
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:toggle-display")

        event.answer.assert_called_once_with("This action is unavailable.", alert=True)
        fake_adapter.handle_message.assert_not_called()


class TestScopeDispatch:
    @pytest.mark.asyncio
    async def test_scope_clears_existing_override(self, fake_adapter):
        fake_adapter.gateway_runner._session_reasoning_overrides = {
            SESSION_KEY: {"enabled": True, "effort": "high"},
        }
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:scope")

        event.answer.assert_called_once_with()
        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert dispatched.text == "/reasoning reset"

    @pytest.mark.asyncio
    async def test_scope_toasts_when_already_global(self, fake_adapter):
        fake_adapter.gateway_runner._session_reasoning_overrides = {}
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:scope")

        event.answer.assert_called_once_with("Reasoning scope is already global.")
        fake_adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_scope_without_runner_answers_unavailable(self, fake_adapter):
        fake_adapter.gateway_runner = None
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:scope")

        event.answer.assert_called_once_with("This action is unavailable.", alert=True)
        fake_adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_scope_falls_back_to_adapter_key(self, fake_adapter):
        """A runner without the key derivation still resolves the override
        through the adapter's own session key."""
        runner = SimpleNamespace(
            _session_reasoning_overrides={SESSION_KEY: {"enabled": True, "effort": "low"}},
        )
        fake_adapter.gateway_runner = runner
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, "hm:reasoning:scope")

        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert dispatched.text == "/reasoning reset"


class TestUnknownPayload:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", ["hm:reasoning:bogus", "hm:reasoning:", "hm:reasoning:set:high"])
    async def test_unknown_reasoning_payload_answers_unavailable(self, fake_adapter, data):
        source = _source()
        event = _callback_event()

        await fake_adapter._handle_reasoning_callback(event, source, data)

        event.answer.assert_called_once_with("This Telegram action is unavailable.", alert=True)
        fake_adapter.handle_message.assert_not_called()


class TestCallbackQueryWiring:
    @pytest.mark.asyncio
    async def test_on_callback_query_routes_reasoning_pick(self, fake_adapter, monkeypatch):
        """A tap no longer falls through to 'This Telegram action is
        unavailable.': it reaches the reasoning dispatcher."""
        monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda home: False)
        source = _source()
        fake_adapter._source = AsyncMock(return_value=source)
        fake_adapter._authorized = lambda _src: True
        fake_adapter._handle_control_callback = AsyncMock(return_value=False)
        event = _callback_event()
        event.data = b"hm:reasoning:pick"

        await fake_adapter._on_callback_query(event)

        event.answer.assert_called_once_with()
        dispatched = fake_adapter.handle_message.await_args.args[0]
        assert dispatched.text == "/reasoning"

    @pytest.mark.asyncio
    async def test_on_callback_query_unknown_reasoning_payload_stays_unavailable(
        self, fake_adapter, monkeypatch
    ):
        monkeypatch.setattr("gateway.mongo_multiuser.enabled", lambda home: False)
        source = _source()
        fake_adapter._source = AsyncMock(return_value=source)
        fake_adapter._authorized = lambda _src: True
        fake_adapter._handle_control_callback = AsyncMock(return_value=False)
        event = _callback_event()
        event.data = b"hm:reasoning:bogus"

        await fake_adapter._on_callback_query(event)

        event.answer.assert_called_once_with("This Telegram action is unavailable.", alert=True)
        fake_adapter.handle_message.assert_not_called()


# --- Dispatcher + real /reasoning handler ------------------------------------
# The dispatcher's job ends at the synthetic command; these prove the command
# it emits actually flips state and re-renders worker 1's rich card, using the
# same bare-runner harness as tests/gateway/test_reasoning_rich_cards.py.

class _CardAdapter:
    """Adapter with the Telegram card method; records every card sent."""

    def __init__(self):
        self.cards = []

    async def send_command_card(self, *, source, html, links=(), copies=(), buttons=(), reply_to=None):
        self.cards.append({"source": source, "html": html})
        return True


def _make_runner(adapter=None):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source, anchor=None: {}
    runner._reply_anchor_for_event = lambda event: None
    return runner


def _home_with_config(tmp_path, monkeypatch, body):
    home = tmp_path / "hermes"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    return home


def _real_source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="12345",
        chat_id="12345",
        user_name="testuser",
    )


@pytest.fixture
def live_adapter(tmp_path, monkeypatch):
    """Adapter whose re-dispatch runs the REAL /reasoning handler."""
    _home_with_config(
        tmp_path, monkeypatch,
        "agent:\n  reasoning_effort: medium\ndisplay:\n  show_reasoning: false\n",
    )
    card_adapter = _CardAdapter()
    runner = _make_runner(card_adapter)
    adapter = TelethonAdapter.__new__(TelethonAdapter)
    adapter.config = SimpleNamespace(extra={})
    adapter.gateway_runner = runner

    async def _forward(event):
        await runner._handle_reasoning_command(event)

    adapter.handle_message = AsyncMock(side_effect=_forward)
    adapter._card_adapter = card_adapter
    adapter._live_runner = runner
    return adapter


class TestDispatcherWithRealHandler:
    @pytest.mark.asyncio
    async def test_toggle_display_flips_both_ways_and_rerenders_card(self, live_adapter):
        source = _real_source()
        runner = live_adapter._live_runner

        await live_adapter._handle_reasoning_callback(
            _callback_event(), source, "hm:reasoning:toggle-display")
        assert runner._show_reasoning is True
        assert len(live_adapter._card_adapter.cards) == 1
        assert ">on<" in live_adapter._card_adapter.cards[0]["html"]
        assert "hm:reasoning:toggle-display" in live_adapter._card_adapter.cards[0]["html"]

        await live_adapter._handle_reasoning_callback(
            _callback_event(), source, "hm:reasoning:toggle-display")
        assert runner._show_reasoning is False
        assert len(live_adapter._card_adapter.cards) == 2
        assert ">off<" in live_adapter._card_adapter.cards[1]["html"]

    @pytest.mark.asyncio
    async def test_scope_clears_override_and_renders_reset_card(self, live_adapter):
        source = _real_source()
        runner = live_adapter._live_runner
        session_key = runner._session_key_for_source(source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "high"}

        await live_adapter._handle_reasoning_callback(
            _callback_event(), source, "hm:reasoning:scope")

        assert session_key not in runner._session_reasoning_overrides
        assert len(live_adapter._card_adapter.cards) == 1
        assert "global config" in live_adapter._card_adapter.cards[0]["html"]

        # A second tap finds no override: toast, no new card.
        event = _callback_event()
        await live_adapter._handle_reasoning_callback(event, source, "hm:reasoning:scope")
        event.answer.assert_called_once_with("Reasoning scope is already global.")
        assert len(live_adapter._card_adapter.cards) == 1

    @pytest.mark.asyncio
    async def test_pick_opens_bare_reasoning_card(self, live_adapter):
        source = _real_source()

        await live_adapter._handle_reasoning_callback(
            _callback_event(), source, "hm:reasoning:pick")

        # The bare command renders the settings card (this adapter has no
        # choice picker, so the card IS the picker surface on Telegram).
        assert len(live_adapter._card_adapter.cards) == 1
        html = live_adapter._card_adapter.cards[0]["html"]
        assert "hm:reasoning:pick" in html
        assert "hm:reasoning:scope" in html
        assert "hm:reasoning:toggle-display" in html
