from hermes_state import AsyncSessionDB, SessionDB
"""Tests for gateway /status behavior and token persistence."""

from datetime import datetime
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.session import (
    AsyncSessionStore,
    SessionEntry,
    SessionSource,
    SessionStore,
    build_session_key,
)


def _make_source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, *, platform: Platform = Platform.TELEGRAM) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=_make_source(platform),
        message_id="m1",
    )


def _status_row(label: str, value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    return f"<td>{label}</td><td>{value}</td>"


def _make_runner(session_entry: SessionEntry, *, platform: Platform = Platform.TELEGRAM):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={platform: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter.send_command_card = AsyncMock(return_value=True)
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = AsyncSessionDB(MagicMock())
    runner._session_db._db.get_session_title.return_value = None
    # Default: no DB row → /status reports 0 tokens.  Tests that exercise
    # the populated path override this.
    runner._session_db._db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._agent_cache = {}
    runner._agent_cache_lock = MagicMock()
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


async def _handle_and_card(runner, event):
    """Drive ``_handle_message`` and return the rich-card html it sent."""
    await runner._handle_message(event)
    card = runner.adapters[event.source.platform].send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


async def _handle_context_and_card(runner, event):
    """Drive ``_handle_context_command`` and return the rich-card html it sent."""
    result = await runner._handle_context_command(event)
    assert result is None, "expected /context to send a rich card on Telegram"
    card = runner.adapters[event.source.platform].send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    return card.await_args.kwargs["html"]


@pytest.mark.asyncio
async def test_status_command_reads_token_totals_from_session_db():
    """Regression test for #17158: /status must source token totals from the
    SQLite SessionDB (where run_agent.py persists them) and sum all component
    counts, not from SessionEntry (which the agent never writes)."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,  # SessionEntry never gets written to — always 0.
    )
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
        "reasoning_tokens": 50,
    }

    result = await _handle_and_card(runner, _make_event("/status"))

    # 1000 + 250 + 500 + 100 + 50 = 1,900
    assert _status_row("Lifetime tokens billed", "1,900") in result


@pytest.mark.asyncio
async def test_status_command_includes_live_agent_model_and_context():
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {
        "input_tokens": 1000,
        "output_tokens": 250,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "model": "openai/gpt-test",
    }
    running_agent = SimpleNamespace(
        model="openai/gpt-test",
        provider="openai",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=12_345,
            context_length=100_000,
        ),
        interrupt=MagicMock(),
    )
    runner._running_agents[build_session_key(_make_source())] = running_agent

    result = await _handle_and_card(runner, _make_event("/status"))

    assert _status_row("Model", "`openai/gpt-test` (openai)") in result
    assert _status_row("Context", "12,345 / 100,000 (12%)") in result
    assert _status_row("Lifetime tokens billed", "1,250") in result


@pytest.mark.asyncio
async def test_status_command_uses_most_recent_persisted_model_route(tmp_path):
    """Persisted status uses the latest coherent route, not the lifetime-dominant route."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    db = SessionDB(db_path=tmp_path / "state.db")
    runner._session_db = AsyncSessionDB(db)
    try:
        db.create_session("sess-1", "telegram", model="z-ai/glm-5.2")
        db.update_token_counts(
            "sess-1",
            model="z-ai/glm-5.2",
            billing_provider="nvidia",
            billing_base_url="https://integrate.api.nvidia.com/v1/",
            input_tokens=480,
            api_call_count=48,
        )
        db.update_token_counts(
            "sess-1",
            model="upstage/solar-pro4:free",
            billing_provider="nous",
            billing_base_url="https://inference-api.nousresearch.com/v1/",
            input_tokens=60,
            api_call_count=6,
        )
        # Reproduce the inconsistent legacy summary observed in #87227.
        db.update_session_model("sess-1", "z-ai/glm-5.2")
        db.update_session_billing_route(
            "sess-1",
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1/",
        )

        result = await _handle_and_card(runner, _make_event("/status"))

        assert _status_row("Model", "`upstage/solar-pro4:free` (nous)") in result
        assert _status_row("Model", "`z-ai/glm-5.2` (nvidia)") not in result
    finally:
        db.close()


@pytest.mark.asyncio
async def test_status_command_prefers_rehydrated_session_model_override(tmp_path):
    """A committed /model switch is current before the selected model records usage."""
    source = _make_source()
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    session_entry = store.get_or_create_session(source)
    runner = _make_runner(session_entry)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    db = SessionDB(db_path=tmp_path / "state.db")
    runner._session_db = AsyncSessionDB(db)
    try:
        db.create_session(session_entry.session_id, "telegram", model="model-a")
        db.update_token_counts(
            session_entry.session_id,
            model="model-a",
            billing_provider="provider-a",
            input_tokens=480,
            api_call_count=48,
        )
        result = SimpleNamespace(
            new_model="model-b",
            target_provider="provider-b",
            provider_label="Provider B",
            api_key="secret",
            base_url="https://b.example/v1",
            api_mode=None,
            request_overrides={},
            runtime_capabilities={},
        )
        switch_ctx = SimpleNamespace(
            session_key=session_entry.session_key,
            current_model="model-a",
            persist_global=False,
            restore_snapshot=None,
        )
        await runner._record_model_switch(
            result, switch_ctx, source=source, one_turn=False, picker=False
        )
        # Simulate a restart: /status must lazily recover the durable override.
        runner._session_state(session_entry.session_key).conversation.model_override = None

        status = await _handle_and_card(runner, _make_event("/status"))

        assert _status_row("Model", "`model-b` (provider-b)") in status
        assert _status_row("Model", "`model-a` (provider-a)") not in status
    finally:
        db.close()


def _runner_with_session_override(override: dict, *, last_prompt_tokens: int):
    """Runner whose session has a committed /model override but no resident agent (no compressor)."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_entry.last_prompt_tokens = last_prompt_tokens
    runner = _make_runner(session_entry)
    runner._session_state(session_entry.session_key).conversation.model_override = override
    return runner


@pytest.mark.asyncio
async def test_status_command_resolves_window_of_session_only_model_switch():
    """After a session-only /model switch with no turn yet, /status must show used / window for the
    switched-to route (looked up against THAT route's endpoint, and never the default route's
    ``model.context_length`` pin) instead of the occupancy-only line (#111436)."""
    override = {"model": "kimi-k3", "provider": "kimi-coding",
                "base_url": "https://api.kimi.com/coding", "api_key": "sk-override"}
    runner = _runner_with_session_override(override, last_prompt_tokens=79_455)
    config = {"model": {"default": "default-model", "provider": "openrouter", "context_length": 80_000}}

    with patch("gateway.run._load_gateway_config", return_value=config), patch(
        "gateway.run._resolve_runtime_agent_kwargs"
    ) as default_runtime, patch(
        "agent.model_metadata.get_model_context_length", return_value=1_048_576
    ) as lookup:
        result = await _handle_and_card(runner, _make_event("/status"))

    assert _status_row("Model", "`kimi-k3` (kimi-coding)") in result
    assert _status_row("Context", "79,455 / 1,048,576 (8%)") in result
    assert "80,000" not in result
    default_runtime.assert_not_called()
    kwargs = lookup.call_args.kwargs
    assert (kwargs["base_url"], kwargs["api_key"], kwargs["provider"]) == (
        override["base_url"], override["api_key"], "kimi-coding")
    assert kwargs["config_context_length"] is None


@pytest.mark.asyncio
async def test_status_command_keeps_occupancy_only_for_unknown_model_window():
    """A model the resolver cannot size falls to DEFAULT_FALLBACK_CONTEXT; /status must not present
    that invented number as the window (a catalog-listed model of the same size still counts)."""
    from agent.context_default_record import mark_context_defaulted, records_context_defaults
    from agent.model_metadata import DEFAULT_FALLBACK_CONTEXT

    @records_context_defaults  # the resolver records that it defaulted; a catalog 256K below does not
    def _unsized(model, base_url="", **_kwargs):
        mark_context_defaulted()
        return DEFAULT_FALLBACK_CONTEXT

    runner = _runner_with_session_override(
        {"model": "proxy-mystery-model", "provider": "custom:proxy", "base_url": "http://127.0.0.1:1/v1"},
        last_prompt_tokens=4_321,
    )
    with patch("gateway.run._load_gateway_config", return_value={}), patch(
        "agent.model_metadata.get_model_context_length", _unsized
    ):
        result = await _handle_and_card(runner, _make_event("/status"))
    assert _status_row("Context", "~4,321 tokens") in result
    assert f"{DEFAULT_FALLBACK_CONTEXT:,}" not in result

    runner = _runner_with_session_override(
        {"model": "grok-4", "provider": "xai", "base_url": ""}, last_prompt_tokens=4_321)
    with patch("gateway.run._load_gateway_config", return_value={}), patch(
        "agent.model_metadata.get_model_context_length", return_value=DEFAULT_FALLBACK_CONTEXT
    ):
        result = await _handle_and_card(runner, _make_event("/status"))
    assert _status_row("Context", f"4,321 / {DEFAULT_FALLBACK_CONTEXT:,} (2%)") in result


@pytest.mark.asyncio
async def test_status_command_default_route_keeps_runtime_endpoint_and_context_pin():
    """No /model switch and no resident agent (first /status after a restart): the winner is the
    persisted route or the SessionDB row, which carry no endpoint of their own. The window must then
    be resolved against the default runtime route (custom base_url + key, ``model.context_length``
    pin intact) exactly as /context does, not against an empty endpoint that drops the pin."""
    config = {"model": {"default": "my-local-model", "provider": "custom",
                        "base_url": "http://127.0.0.1:1/v1", "context_length": 32_768}}
    runtime = {"model": "my-local-model", "provider": "custom",
               "base_url": "http://127.0.0.1:1/v1", "api_key": "local-key"}
    route = {"model": "my-local-model", "billing_provider": "custom"}
    for persisted_route, session_row in ((route, None), ({}, dict(route))):
        session_entry = SessionEntry(
            session_key=build_session_key(_make_source()),
            session_id="sess-1",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            platform=Platform.TELEGRAM,
            chat_type="dm",
        )
        session_entry.last_prompt_tokens = 9_000
        runner = _make_runner(session_entry)
        runner._session_db._db.get_session.return_value = session_row
        runner._session_db._db.get_recent_session_model_route.return_value = persisted_route

        with patch("gateway.run._load_gateway_config", return_value=config), patch(
            "gateway.run._resolve_runtime_agent_kwargs", return_value=runtime
        ) as default_runtime, patch(
            "agent.model_metadata.get_model_context_length",
            side_effect=lambda _model, **kw: kw["config_context_length"] or 8_192,
        ) as lookup:
            result = await _handle_and_card(runner, _make_event("/status"))

        assert _status_row("Model", "`my-local-model` (custom)") in result
        assert _status_row("Context", "9,000 / 32,768 (27%)") in result
        default_runtime.assert_called_once()
        kwargs = lookup.call_args.kwargs
        assert (kwargs["base_url"], kwargs["api_key"], kwargs["config_context_length"]) == (
            runtime["base_url"], runtime["api_key"], 32_768)


@pytest.mark.asyncio
async def test_agents_command_reports_active_agents_and_processes(monkeypatch):
    session_key = build_session_key(_make_source())
    session_entry = SessionEntry(
        session_key=session_key,
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    running_agent = SimpleNamespace(
        session_id="sess-running",
        model="openrouter/test-model",
        interrupt=MagicMock(),
        get_activity_summary=lambda: {"seconds_since_activity": 0},
    )
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts = {session_key: time.time() - 8}
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return [
                {
                    "session_id": "proc-1",
                    "status": "running",
                    "uptime_seconds": 17,
                    "command": "sleep 30",
                }
            ]

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await _handle_and_card(runner, _make_event("/agents"))

    assert "<strong>Active agents:</strong> 1" in result
    assert "<strong>Running background processes:</strong> 1" in result
    assert "proc-1" in result
    running_agent.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_tasks_alias_routes_to_agents_command(monkeypatch):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner = _make_runner(session_entry)
    runner._background_tasks = set()

    class _FakeRegistry:
        def list_sessions(self):
            return []

    monkeypatch.setattr("tools.process_registry.process_registry", _FakeRegistry())

    result = await _handle_and_card(runner, _make_event("/tasks"))

    assert "Active Agents &amp; Tasks" in result


@pytest.mark.asyncio
async def test_first_run_slack_home_channel_onboarding_uses_parent_command(monkeypatch):
    import gateway.run as gateway_run

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source(Platform.SLACK)),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SLACK,
        chat_type="dm",
    )
    runner = _make_runner(session_entry, platform=Platform.SLACK)
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = False
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model": "openai/test-model",
        }
    )

    monkeypatch.delenv("SLACK_HOME_CHANNEL", raising=False)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello", platform=Platform.SLACK))

    assert result == "ok"
    runner.adapters[Platform.SLACK].send.assert_awaited_once()
    onboarding = runner.adapters[Platform.SLACK].send.await_args.args[1]
    assert "/hermes sethome" in onboarding
    assert "Type /sethome" not in onboarding


@pytest.mark.asyncio
async def test_handle_message_stale_result_keeps_newer_generation_callback(monkeypatch):
    import gateway.run as gateway_run

    class _Adapter:
        def __init__(self):
            self._post_delivery_callbacks = {}

        async def send(self, *args, **kwargs):
            return None

        def pop_post_delivery_callback(self, session_key, *, generation=None):
            entry = self._post_delivery_callbacks.get(session_key)
            if entry is None:
                return None
            if isinstance(entry, tuple):
                entry_generation, callback = entry
                if generation is not None and entry_generation != generation:
                    return None
                self._post_delivery_callbacks.pop(session_key, None)
                return callback
            if generation is not None:
                return None
            return self._post_delivery_callbacks.pop(session_key, None)

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.session_store.load_transcript.return_value = [{"role": "user", "content": "earlier"}]
    session_key = session_entry.session_key
    adapter = _Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter

    async def _stale_result(**kwargs):
        # Simulate a newer run claiming the callback slot before the stale run unwinds.
        runner._session_run_generation[session_key] = 2
        adapter._post_delivery_callbacks[session_key] = (2, lambda: None)
        return {
            "final_response": "late reply",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 80,
            "input_tokens": 120,
            "output_tokens": 45,
            "model": "openai/test-model",
        }

    runner._run_agent = AsyncMock(side_effect=_stale_result)

    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100000,
    )

    result = await runner._handle_message(_make_event("hello"))

    assert result is None
    assert session_key in adapter._post_delivery_callbacks
    assert adapter._post_delivery_callbacks[session_key][0] == 2


@pytest.mark.asyncio
async def test_status_command_bypasses_active_session_guard():
    """When an agent is running, /status must be dispatched immediately via
    base.handle_message — not queued or treated as an interrupt (#5046)."""
    import asyncio
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import build_session_key
    from gateway.config import Platform, PlatformConfig

    source = _make_source()
    session_key = build_session_key(source)

    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "📊 **Hermes Gateway Status**\n**Agent Running:** Yes ⚡"

    # Concrete subclass to avoid abstract method errors
    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self, *, is_reconnect: bool = False): pass
        async def disconnect(self): pass
        async def send(self, chat_id, content, **kwargs): pass
        async def get_chat_info(self, chat_id): return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry

    # Simulate an active session
    interrupt_event = asyncio.Event()
    adapter._active_sessions[session_key] = interrupt_event

    event = MessageEvent(
        text="/status",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with, "/status handler was never called (event was queued or dropped)"
    assert sent, "/status response was never sent"
    assert "Agent Running" in sent[0]
    assert not interrupt_event.is_set(), "/status incorrectly triggered an agent interrupt"
    assert session_key not in adapter._pending_messages, "/status was incorrectly queued"


@pytest.mark.asyncio
async def test_profile_command_reports_source_stamped_profile(monkeypatch, tmp_path):
    """On a multiplexed gateway, /profile reports the profile SERVING the
    source (source.profile — URL prefix / per-credential adapter / room map),
    not the multiplexer's active profile, which is always the default and
    made /profile answer "default" in every persona chat."""
    hermes_home = tmp_path / ".hermes"
    profile_home = hermes_home / "profiles" / "milo"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("{}\n")  # identity marker: a bare dir is not a profile

    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    runner.config.multiplex_profiles = True
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    event = _make_event("/profile")
    event.source.profile = "milo"

    runner._delivery_adapter_for = lambda source: runner.adapters[Platform.TELEGRAM]
    await runner._handle_profile_command(event)
    card = runner.adapters[Platform.TELEGRAM].send_command_card
    assert card.await_args is not None, "expected a Telegram rich card"
    result = card.await_args.kwargs["html"]

    assert "<strong>Profile:</strong> <code>milo</code>" in result
    # The reply renders display_hermes_home() for the routed profile, which abbreviates a home
    # under $HOME to ``~/…``; compare against the same rendering rather than the raw path.
    from gateway.run import _profile_runtime_scope
    from hermes_constants import display_hermes_home

    with _profile_runtime_scope(profile_home):
        expected_home = display_hermes_home()
    assert f"<strong>Home:</strong> <code>{expected_home}</code>" in result


# ── /context command tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_command_keeps_configured_window_without_resident_agent():
    """The no-agent fallback must not replace a custom-provider context pin."""
    model = "unsloth/Qwen3.8-27B-GGUF:Q8_0"
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-context-pin",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_entry.last_prompt_tokens = 66_570
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {"model": model}

    config = {
        "model": {
            "default": model,
            "provider": "custom-local-qwen",
            "context_length": 262_144,
        },
        "custom_providers": [
            {
                "name": "custom-local-qwen",
                "base_url": "http://127.0.0.1:8080/v1",
                "models": {},
            }
        ],
    }
    runtime = {
        "provider": "custom-local-qwen",
        "base_url": "http://127.0.0.1:8080/v1",
        "api_key": "",
    }

    with patch("gateway.run._load_gateway_config", return_value=config), patch(
        "gateway.run._resolve_runtime_agent_kwargs", return_value=runtime
    ), patch(
        "hermes_cli.config.get_compatible_custom_providers",
        return_value=config["custom_providers"],
    ), patch(
        "agent.model_metadata.get_model_context_length",
        side_effect=lambda *args, **kwargs: kwargs.get("config_context_length") or 131_072,
    ) as context_lookup:
        result = await _handle_context_and_card(runner, _make_event("/context"))

    assert "<th>Measure</th><th>Tokens</th><th>Window</th>" in result
    assert "<td>In use</td><td>66,570</td><td>25%" in result
    assert "<td>Headroom</td><td>195,574</td>" in result
    assert "131,072" not in result
    from plugins.platforms.telegram.mtproto_rich import detect_rich_format, send_request
    from telethon import types
    assert result.startswith("<h1>Context window</h1>")
    assert detect_rich_format(result) == "html"
    request = send_request(types.InputPeerSelf(), result)
    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    assert context_lookup.call_count == 1
    assert context_lookup.call_args.kwargs["config_context_length"] == 262_144


@pytest.mark.asyncio
async def test_context_cold_start_restores_persisted_usage_and_keeps_full_honest_layout():
    """After restart, durable window/usage and transcript estimates remain useful without inventing live state."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()), session_id="sess-cold-context",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    session_entry.last_prompt_tokens = 31_250
    runner = _make_runner(session_entry)
    runner._session_db._db.get_session.return_value = {
        "model": "openai/gpt-test", "input_tokens": 1_000, "output_tokens": 600,
        "cache_read_tokens": 2_000, "cache_write_tokens": 80,
        "reasoning_tokens": 120, "api_call_count": 7,
    }
    history = [
        {"role": "user", "content": "A persisted conversation message with enough text to estimate tokens."},
        {"role": "assistant", "content": "A persisted assistant reply for transcript-only reconstruction."},
    ]
    runner.session_store.load_transcript.return_value = history
    runner._resident_agent_for = lambda _key: None  # cold start: no compressor or prompt inventory in memory
    runner._resolve_context_figures = AsyncMock(
        return_value=(31_250, 128_000, "openai/gpt-test", "")
    )

    result = await _handle_context_and_card(runner, _make_event("/context all"))

    assert "<h2>Current model window</h2>" in result
    assert "<td>In use</td><td>31,250</td><td>24%</td>" in result
    assert "<td>Capacity</td><td>128,000</td><td>100%</td>" in result
    assert "<td>Headroom</td><td>96,750</td><td>76%</td>" in result
    assert "Cumulative session totals; these are not the current model window." in result
    assert "<td>Input</td><td>1,000</td>" in result
    assert "<td>Output</td><td>600</td>" in result
    assert "<td>Reasoning</td><td>120</td>" in result
    assert "<td>Cache read</td><td>2,000</td>" in result
    assert "<td>Cache write</td><td>80</td>" in result
    assert "<td>Total input + output + cache</td><td>3,680</td>" in result
    assert "Reasoning is reported separately and is part of output" in result
    assert "<td>API calls</td><td>7</td>" in result
    assert "Automatic compression" in result
    assert "Compression count" in result and "Unavailable after restart" in result
    assert "Conversation transcript (rough)" in result and "Share of window" in result
    assert "Context files and sizes" in result and "Inventory unavailable" in result
    assert "Toolsets and skills (/context all)" in result
    assert "Toolset breakdown unavailable" in result
    assert "Skill breakdown unavailable" in result
    # This used to be inserted as an untranslated Markdown footer into an HTML response.
    assert "_(Full compression" not in result
    assert "**" not in result
    assert "<h1>Context window</h1>" == result.splitlines()[0]
    runner._resolve_context_figures.assert_awaited_once()


def _stub_agent(**overrides) -> SimpleNamespace:
    """Build a stub agent with the attributes _handle_context_command reads."""
    props = dict(
        model="openai/gpt-test",
        context_compressor=SimpleNamespace(
            last_prompt_tokens=47_231,
            context_length=200_000,
            threshold_tokens=100_000,
            threshold_percent=0.5,
            compression_count=2,
            _last_compression_savings_pct=63.0,
        ),
        session_api_calls=47,
        session_input_tokens=410_000,
        session_output_tokens=38_000,
        session_reasoning_tokens=12_000,
        session_total_tokens=3_158_641,
        session_cache_read_tokens=2_900_000,
        session_cache_write_tokens=48_000,
    )
    props.update(overrides)
    return SimpleNamespace(**props)


@pytest.mark.asyncio
async def test_context_all_appends_expanded_listings():
    """/context all appends per-toolset and per-skill cost listings."""
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-6",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner = _make_runner(session_entry)
    agent = _stub_agent()
    runner._running_agents[session_entry.session_key] = agent

    fake_payload = {
        "categories": [
            {"id": "skills", "label": "Skills <estimate>", "tokens": 2_000},
        ],
        "context_max": 200_000,
        "context_percent": 24,
        "context_used": 47_231,
        "estimated_total": 2_000,
        "model": "openai/gpt-test",
    }
    fake_details = {
        "skills": [
            {"name": "<hermes-agent>", "index_tokens": 30, "skill_md_tokens": 2_500},
        ],
        "toolsets": [
            {"toolset": "<terminal>", "tool_count": 4, "schema_tokens": 5_100},
        ],
    }
    from unittest.mock import patch as _patch
    with _patch(
        "agent.context_breakdown.compute_session_context_breakdown",
        return_value=fake_payload,
    ), _patch(
        "agent.context_breakdown.compute_context_details",
        return_value=fake_details,
    ):
        result = await _handle_context_and_card(runner, _make_event("/context all"))

    assert "<details><summary>Toolsets by schema cost (1)" in result
    assert "&lt;terminal&gt;" in result and "5,100 tokens" in result
    assert "<details><summary>Skills by cost (1)" in result
    assert "&lt;hermes-agent&gt;" in result
    assert "&lt;Skills &lt;estimate&gt;&gt;" not in result
    assert "Skills &lt;estimate&gt;" in result
    assert "Auto-compression at 100,000 tokens (50%); 52,769 tokens to go." in result
    assert "Last savings" in result and "63%" in result
    assert "Cumulative session totals; these are not the current model window." in result
    assert "410,000" in result and "3,158,641" in result
    # Expanded view drops the hint
    assert "Use /context all" not in result


@pytest.mark.asyncio
async def test_context_for_a_group_does_not_read_another_users_private_agent():
    """The context lookup is bound to the inbound chat, never an owner's DM session."""
    tenant_source = SessionSource(
        platform=Platform.TELEGRAM, user_id="tenant-7", chat_id="group-7",
        user_name="tenant", chat_type="group",
    )
    owner_source = SessionSource(
        platform=Platform.TELEGRAM, user_id="owner-1", chat_id="owner-1",
        user_name="owner", chat_type="dm",
    )
    session_entry = SessionEntry(
        session_key=build_session_key(tenant_source), session_id="tenant-session",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner = _make_runner(session_entry)
    runner._running_agents[build_session_key(owner_source)] = _stub_agent(model="owner-private-model")
    looked_up = []
    runner._resident_agent_for = lambda key: looked_up.append(key) or runner._running_agents.get(key)
    runner._resolve_context_figures = AsyncMock(return_value=(0, 0, "", ""))

    result = await _handle_context_and_card(runner, MessageEvent(text="/context", source=tenant_source))

    assert "owner-private-model" not in result
    assert looked_up == [build_session_key(tenant_source)]


def test_context_rich_explanations_math_and_file_sizes_are_complete():
    from gateway.slash_commands_status import _telegram_context_html

    agent = _stub_agent(session_input_tokens=1_000, session_output_tokens=600,
                        session_reasoning_tokens=120, session_total_tokens=3_680,
                        session_cache_read_tokens=2_000, session_cache_write_tokens=80)
    agent.context_compressor.threshold_tokens = 150_000
    agent.context_compressor.threshold_percent = .75
    result = _telegram_context_html(
        agent=agent, ctx=agent.context_compressor, used=16_689, context_length=200_000,
        model_name='openrouter/free', estimated=False,
        payload={'categories': [
            {'label': 'System prompt', 'tokens': 2_000},
            {'label': 'Tool definitions', 'tokens': 10_000},
        ]}, files=[{'label': 'SOUL.md', 'chars': 668, 'est_tokens': 167, 'status': 'loaded'}],
        files_available=True,
    )
    assert 'Tokens are chunks of text, not messages.' in result
    assert 'Usage % = in-use tokens ÷ capacity × 100' in result
    assert '<td>In use</td><td>16,689</td><td>8%</td>' in result
    assert '<td>Headroom</td><td>183,311</td><td>92%</td>' in result
    assert '150,000 tokens (75%); 133,311 tokens to go.' in result
    assert 'Compression count</strong>　2' in result
    assert 'Each model call sends its context again' in result
    assert '<td>Total input + output + cache</td><td>3,680</td>' in result
    assert '<td>Reasoning</td><td>120</td>' in result
    assert 'avoids double-counting' in result
    assert '<td>System prompt</td><td>~2,000 tokens</td><td>~1.0%</td>' in result
    assert '<td>Tool definitions</td><td>~10,000 tokens</td><td>~5.0%</td>' in result
    assert '<td>SOUL.md</td><td>668 chars</td><td>~167 tokens</td><td>Loaded</td>' in result
    assert '<code>/context all</code>' in result


def test_context_expanded_lists_are_bounded_and_disclose_partial_display_counts():
    from gateway.slash_commands_status import _telegram_context_html
    from plugins.platforms.telegram.mtproto_rich import input_rich_message
    from telethon import types

    long_name = "'" * 1_000
    agent = _stub_agent()
    result = _telegram_context_html(
        agent=agent, ctx=agent.context_compressor, used=47_231, context_length=200_000,
        model_name=long_name, estimated=False, expanded=True,
        payload={'categories': [{'label': long_name, 'tokens': 1_000}] * 5},
        files=[{'label':long_name, 'chars':800, 'est_tokens':200, 'status':'loaded'}] * 70,
        files_available=True,
        details={'toolsets':[{'toolset':long_name, 'tool_count':4, 'schema_tokens':100}] * 80,
                 'skills':[{'name':long_name, 'index_tokens':30, 'skill_md_tokens':200}] * 90},
    )
    assert len(result) < 32_768
    assert 'Showing 20 of 70 context files.' in result
    assert 'Showing 15 of 80 toolsets.' in result
    assert 'Showing 15 of 90 skills.' in result
    assert result.count('<table>') == result.count('</table>')
    assert result.count('<details>') == result.count('</details>')
    native = input_rich_message(result)
    assert isinstance(native, types.InputRichMessageHTML) and bytes(native)
