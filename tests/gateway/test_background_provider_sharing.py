"""Background tasks enforce per-user provider sharing.

``/bg`` runs through ``GatewayBackgroundMixin._run_background_task_inner``. A
task whose resolved provider was added by another user and is not shared
with the requester must be refused with the sharing message before any
agent runs — the other user's credential must never be spent. A task on a
shared (or own) provider proceeds and its result is delivered.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.provider_sharing import ProviderNotSharedError
from gateway.run_background import GatewayBackgroundMixin
from gateway.session import SessionSource


class _MixinRunner(GatewayBackgroundMixin):
    """Bare ``GatewayBackgroundMixin`` with its collaborators stubbed."""

    def __init__(self, adapter, runtime_resolver):
        self._adapter = adapter
        self._runtime_resolver = runtime_resolver
        self._provider_routing = {}
        self._session_db = None
        self._service_tier = None
        self._reasoning_config = None
        self.executor_calls = []
        self.resolve_calls = []

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _thread_metadata_for_source(self, source, event_message_id=None):
        return {}

    def _resolve_session_agent_runtime(self, *, source=None, session_key=None,
                                       user_config=None, enforce_visibility=False, fallback=True):
        self.resolve_calls.append({"enforce_visibility": enforce_visibility})
        return self._runtime_resolver(
            source=source, user_config=user_config, enforce_visibility=enforce_visibility,
        )

    def _resolve_turn_toolsets(self, user_config, source, platform_key):
        return [], None

    def _resolve_session_reasoning_config(self, source=None, model=None):
        return None

    def _resolve_session_service_tier(self, source=None):
        return None

    def _resolve_turn_agent_config(self, prompt, model, runtime_kwargs):
        return {"model": model, "runtime": dict(runtime_kwargs)}

    def _refresh_fallback_model(self):
        return None

    def _cleanup_agent_resources(self, agent):
        return None

    async def _run_in_executor_with_context(self, func, *args):
        self.executor_calls.append(func)
        return func(*args)


def _adapter():
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "task done"))
    adapter.extract_images = MagicMock(return_value=([], "task done"))
    return adapter


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM, user_id="111", chat_id="77", user_name="testuser",
    )


def _sent_content(adapter):
    """Content of the adapter's (single) send, positional or keyword."""
    call = adapter.send.await_args
    if "content" in call.kwargs:
        return call.kwargs["content"]
    return call.args[1]


@pytest.mark.asyncio
async def test_unshared_provider_refused_with_sharing_message(monkeypatch):
    """A /bg task resolving to an unshared provider is refused, never run."""
    adapter = _adapter()

    def _resolver(**_kw):
        raise ProviderNotSharedError("openrouter")

    runner = _MixinRunner(adapter, _resolver)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a, **k: {})

    await runner._run_background_task_inner("do the thing", _source(), "bg_test")

    adapter.send.assert_awaited_once()
    content = _sent_content(adapter)
    assert "openrouter" in content
    assert "isn't shared with you" in content
    assert runner.executor_calls == []
    assert runner.resolve_calls == [{"enforce_visibility": True}]


@pytest.mark.asyncio
async def test_shared_provider_task_proceeds_and_delivers(monkeypatch):
    """A /bg task on a shared/own provider runs and its result is delivered."""
    adapter = _adapter()

    def _resolver(**_kw):
        return ("model-x", {"api_key": "k", "provider": "openrouter"})

    runner = _MixinRunner(adapter, _resolver)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda *a, **k: {})
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "task done", "messages": []}

    with patch("run_agent.AIAgent", return_value=agent) as MockAgent:
        await runner._run_background_task_inner("do the thing", _source(), "bg_test")

    assert len(runner.executor_calls) == 1
    MockAgent.assert_called_once()
    assert MockAgent.call_args.kwargs["model"] == "model-x"
    adapter.send.assert_awaited_once()
    assert "task done" in _sent_content(adapter)
