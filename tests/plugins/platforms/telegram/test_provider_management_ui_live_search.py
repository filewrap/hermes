"""Live-pool model search from the provider management UI.

Behaviour contracts for the Search button calling the provider's live model
pool instead of the static curated list:
- a term matching only a live-pool model (absent from the static list) finds it;
- a term matching curated models still works, over the live list;
- the search prompt says it searches the live pool;
- clicking a live-pool result verifies through the same select path as a
  statically-listed model (confirmation on success);
- a failed live fetch keeps the static list visible and searchable, with a
  clear notice instead of a crash or a blank screen;
- a provider with no live model-list endpoint keeps the static list, with a
  notice saying the live pool is unavailable;
- clearing the search returns to the static list.

The live endpoint is faked, never a real network call: the real OpenRouter
profile fetch runs against a canned ``/models`` payload (fake opener), or the
resolver is stubbed for the failure arms.
"""
import json
import urllib.error
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from hermes_cli.models_catalog_static import CuratedFallbackModels
from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: Platform = Platform.TELEGRAM
    message_id: str = "50"


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text or "", alert))


class IncomingEvent:
    def __init__(self, message_id):
        self.message = SimpleNamespace(id=message_id)


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.requests = []
        self.plain_sends = []
        self.deleted = []
        self.delete_result = True
        self.gateway_runner = None

    def _peer_for(self, chat_id):
        return chat_id

    @staticmethod
    def _sent_message_id(response):
        return str(getattr(response, "id", "")) or None

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=55)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((str(chat_id), str(message_id)))
        return self.delete_result

    async def send(self, *args, **kwargs):
        self.plain_sends.append((args, kwargs))
        return SimpleNamespace(success=True)


def _provider(**updates):
    row = {
        "slug": "openrouter",
        "name": "OpenRouter",
        "description": "Route requests through the OpenRouter API.",
        "auth_type": "api_key",
        "api_key_env_vars": ["OPENROUTER_API_KEY"],
        "base_url_env_var": "OPENROUTER_BASE_URL",
        "signup_url": "https://openrouter.ai/keys",
        "is_catalog_provider": True,
        "configured": True,
        "is_current": True,
        "is_profile_default": True,
        "profile_default_model": "vendor/model-a",
        "models": ["vendor/model-a", "vendor/model-b"],
        "total_models": 2,
        "default_model": "vendor/model-a",
        "default_reasoning": "medium",
        "api_key": "SECRET_MUST_NEVER_ENTER_UI_STATE",
        "extra_headers": {"Authorization": "Bearer SECRET_MUST_NEVER_ENTER_UI_STATE"},
    }
    row.update(updates)
    return row


def _state(ui):
    return next(iter(ui._states.values()))


def _plain_rich_text(value):
    if isinstance(value, str):
        return value
    for name in ("texts", "items", "blocks"):
        children = getattr(value, name, None)
        if children is not None:
            separator = "" if name == "texts" else "\n"
            return separator.join(_plain_rich_text(child) for child in children)
    if hasattr(value, "text"):
        text = _plain_rich_text(value.text)
        url = getattr(value, "url", None)
        return f"{text} ({url})" if url else text
    return ""


def _rich_text(request):
    rich = request.rich_message
    rendered = getattr(rich, "html", None) or getattr(rich, "markdown", None)
    if rendered is not None:
        return rendered
    return _plain_rich_text(rich) or getattr(request, "message", "")


def _buttons(request):
    return [button for row in request.reply_markup.rows for button in row.buttons]


def _button_texts(request):
    return [button.text for button in _buttons(request)]


def _model_button_texts(request):
    """Model-button labels with the "Current · " prefix stripped."""
    return [text.removeprefix("Current · ") for text in _button_texts(request)]


def _select_payload_for(request, label):
    for button in _buttons(request):
        if button.text == label:
            return button.type.data.decode("utf-8")
    raise AssertionError(f"no button labelled {label!r} among {_button_texts(request)}")


# Live-pool ids the fake /models endpoint serves: the two static ids plus one
# model the curated list never contained.
_LIVE_IDS = ["vendor/model-a", "vendor/model-b", "vendor/live-only-model"]


@pytest.fixture
def live_openrouter(monkeypatch):
    """Fake the live /models endpoint for the real OpenRouter profile fetch."""
    import sys

    from hermes_cli import runtime_provider, urllib_security
    from providers import get_provider_profile

    # The plugin lives in plugins/model-providers/ (hyphenated): it only
    # enters sys.modules through provider discovery, never a plain import.
    get_provider_profile("openrouter")
    or_module = sys.modules["plugins.model_providers.openrouter"]
    monkeypatch.setattr(or_module, "_CACHE", None)
    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider",
        lambda **_kwargs: {"api_key": "test-key", "base_url": "https://openrouter.ai/api/v1"},
    )
    payload = json.dumps({"data": [{"id": mid} for mid in _LIVE_IDS]}).encode()

    class Response:
        def getcode(self):
            return 200

        def read(self):
            return payload

    @contextmanager
    def fake_open(request, *, timeout):
        assert request.full_url == "https://openrouter.ai/api/v1/models"
        yield Response()

    monkeypatch.setattr(urllib_security, "open_credentialed_url", fake_open)


@pytest.fixture
def failing_openrouter(monkeypatch):
    """The live /models endpoint is down: every fetch attempt raises."""
    from hermes_cli import runtime_provider, urllib_security

    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider",
        lambda **_kwargs: {"api_key": "test-key", "base_url": "https://openrouter.ai/api/v1"},
    )

    def failing_open(*_args, **_kwargs):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr(urllib_security, "open_credentialed_url", failing_open)


async def _open_models_view(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [{"id": "cred-1"}])
    source = Source()
    await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path, view="models",
        selected_provider="openrouter",
    )
    return adapter, ui, source


async def _search(ui, adapter, source, term):
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:search")
    assert await ui.handle_message(IncomingEvent(91), source, term)
    return adapter.requests[-1]


@pytest.mark.asyncio
async def test_live_search_finds_model_absent_from_static_list(tmp_path, monkeypatch, live_openrouter):
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "live-only")
    assert "vendor/live-only-model" in _model_button_texts(card)
    assert "vendor/model-a" not in _model_button_texts(card)
    assert "Source: Live pool" in _rich_text(card)


@pytest.mark.asyncio
async def test_live_search_still_finds_curated_models(tmp_path, monkeypatch, live_openrouter):
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "model-a")
    assert "vendor/model-a" in _model_button_texts(card)
    assert "Source: Live pool" in _rich_text(card)


@pytest.mark.asyncio
async def test_search_prompt_names_the_live_pool(tmp_path, monkeypatch):
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:search")
    assert "live" in _rich_text(adapter.requests[-1]).lower()


@pytest.mark.asyncio
async def test_live_search_result_select_uses_same_select_path(tmp_path, monkeypatch, live_openrouter):
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "live-only")
    payload = _select_payload_for(card, "vendor/live-only-model")
    calls = []

    async def fake_select(state, source_arg, model, provider_slug):
        calls.append((model, provider_slug))
        return f"Switched to {model}."

    monkeypatch.setattr(ui, "_select_model", fake_select)
    assert await ui.handle_callback(CallbackEvent(55), source, payload)
    assert calls == [("vendor/live-only-model", "openrouter")]
    assert "Switched to vendor/live-only-model." in _rich_text(adapter.requests[-1])


@pytest.mark.parametrize("resolver_result", [
    CuratedFallbackModels(["vendor/model-a", "vendor/model-b"]),
    ["vendor/model-a", "vendor/model-b"],
])
@pytest.mark.asyncio
async def test_live_search_failure_keeps_static_list(tmp_path, monkeypatch, failing_openrouter, resolver_result):
    from hermes_cli import models as models_module

    monkeypatch.setattr(models_module, "provider_model_ids", lambda *_a, **_k: resolver_result)
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "model")
    text = _rich_text(card)
    assert "unreachable" in text.lower()
    assert "Source: Live pool" not in text
    assert "vendor/model-a" in _model_button_texts(card)
    assert "vendor/model-b" in _model_button_texts(card)


@pytest.mark.asyncio
async def test_live_search_without_endpoint_keeps_static_list(tmp_path, monkeypatch):
    import providers
    from hermes_cli import models as models_module

    monkeypatch.setattr(
        providers, "get_provider_profile",
        lambda _slug: SimpleNamespace(supports_model_listing=False),
    )
    monkeypatch.setattr(models_module, "provider_model_ids", lambda *_a, **_k: [])
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "model")
    text = _rich_text(card)
    assert "no live model list" in text.lower()
    assert "Source: Live pool" not in text
    assert "vendor/model-a" in _model_button_texts(card)
    assert "vendor/model-b" in _model_button_texts(card)


@pytest.mark.asyncio
async def test_clear_search_returns_to_static_list(tmp_path, monkeypatch, live_openrouter):
    adapter, ui, source = await _open_models_view(tmp_path, monkeypatch)
    card = await _search(ui, adapter, source, "live-only")
    assert "Source: Live pool" in _rich_text(card)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:clear-search")
    card = adapter.requests[-1]
    assert "Source: Live pool" not in _rich_text(card)
    assert "vendor/model-a" in _model_button_texts(card)
    assert "vendor/model-b" in _model_button_texts(card)
