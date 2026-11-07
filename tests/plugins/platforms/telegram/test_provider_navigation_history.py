"""Navigation history + regular (non-rich) provider buttons.

Behaviour contracts for the /providers navigation fix:
- every sub-page has a Back button;
- Back returns to the last-used screen (history stack), never straight to main;
- provider-name buttons on the /providers list are regular inline-keyboard buttons;
- the oauth authorization link is a rich inline URL button, not a bare hyperlink.
"""
from dataclasses import dataclass
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from gateway.config import Platform
from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI
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


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.requests = []
        self.deleted = []
        self.gateway_runner = None
        self._pv_ui = None
        self._ms_ui = None

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
        return True

    def _provider_ui(self):
        if self._pv_ui is None:
            self._pv_ui = TelegramProviderManagementUI(self)
        return self._pv_ui

    def _model_settings_ui(self):
        if self._ms_ui is None:
            self._ms_ui = TelegramModelSettingsUI(self)
        return self._ms_ui


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
        "ready": True,
        "is_current": True,
        "is_profile_default": True,
        "profile_default_model": "vendor/model-a",
        "models": ["vendor/model-a", "vendor/model-b"],
        "total_models": 2,
        "default_model": "vendor/model-a",
        "default_reasoning": "medium",
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


def _labels(markup):
    return [button.text for row in markup.rows for button in row.buttons]


def _payload_for_label(markup, label):
    return next(
        button.type.data.decode("utf-8")
        for row in markup.rows for button in row.buttons if button.text == label
    )


@pytest.fixture
def ui(monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    return ui


async def _click(ui, source, label):
    markup = ui.adapter.requests[-1].reply_markup
    payload = _payload_for_label(markup, label)
    assert await ui.handle_callback(CallbackEvent(55), source, payload)
    return next(iter(ui._states.values()), None)


async def _open_detail(ui, source, **kwargs):
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=None, view="provider",
        selected_provider="openrouter", **kwargs,
    )
    return _state(ui)


def test_provider_list_buttons_are_regular_keyboard_buttons_not_rich(ui):
    state = ui._new_state(
        Source(), [_provider(), _provider(slug="nous", name="Nous", is_current=False)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="providers",
    )
    text, markup = ui._render(state)
    body = ElementTree.fromstring("<body>" + text + "</body>")
    # No provider navigation as in-body rich callback buttons.
    assert [button.get("data") for button in body.findall(".//tg-button")
            if button.get("type") == "callback_data"] == []
    # The provider names live on the regular keyboard instead.
    payloads = [button.type.data.decode("utf-8")
                for row in markup.rows for button in row.buttons]
    assert any(payload.endswith(":provider:0") for payload in payloads)
    assert any(payload.endswith(":provider:1") for payload in payloads)
    names = [button.text for row in markup.rows for button in row.buttons]
    assert any("OpenRouter" in name for name in names)
    assert any("Nous" in name for name in names)
    # The current route shows the provider and the model as genuine rich copy
    # buttons (copy has no regular equivalent), one per paragraph.
    assert (
        '<p>Provider: <tg-button type="copy_text" style="success" '
        'text="openrouter">openrouter</tg-button></p>'
    ) in text
    copy_buttons = [button for button in body.findall(".//tg-button")
                    if button.get("type") == "copy_text"]
    assert [button.text for button in copy_buttons] == ["openrouter", "vendor/model-a"]


@pytest.mark.asyncio
async def test_back_steps_through_last_used_provider_screens(ui):
    source = Source()
    await _open_detail(ui, source)
    state = await _click(ui, source, "Setup / connect")
    assert state["view"] == "keys"
    assert "Back" in _labels(ui.adapter.requests[-1].reply_markup)

    state = await _click(ui, source, "Back")
    assert state["view"] == "provider"

    state = await _click(ui, source, "Credentials")
    assert state["view"] == "credentials"
    assert "Back" in _labels(ui.adapter.requests[-1].reply_markup)

    state = await _click(ui, source, "Back")
    assert state["view"] == "provider"

    state = await _click(ui, source, "Back")
    assert state["view"] == "providers"


@pytest.mark.asyncio
async def test_reasoning_view_has_back_and_close_and_back_returns_to_detail(ui):
    source = Source()
    await _open_detail(ui, source)
    state = await _click(ui, source, "Reasoning")
    assert state["view"] == "reasoning"
    labels = _labels(ui.adapter.requests[-1].reply_markup)
    assert "Back" in labels and "Close" in labels

    state = await _click(ui, source, "Back")
    assert state["view"] == "provider"


@pytest.mark.asyncio
async def test_cancel_key_input_returns_to_credentials_not_provider_detail(ui):
    source = Source()
    await _open_detail(ui, source)
    state = await _click(ui, source, "Credentials")
    assert state["view"] == "credentials"
    state = await _click(ui, source, "Add / rotate key")
    assert state["view"] == "collect-key"
    state = await _click(ui, source, "Cancel")
    assert state["view"] == "credentials"


def test_oauth_authorization_link_is_a_rich_url_button_not_a_bare_link(ui):
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="oauth", selected_provider="openrouter",
    )
    state.update(oauth_url="https://login.example.test/authorize",
                 oauth_code="ABCD-EFGH", oauth_status="Waiting for authorization code",
                 oauth_needs_code=True)
    text, _markup = ui._render(state)
    assert "[Open provider authorization page](" not in text
    body = ElementTree.fromstring("<body>" + text + "</body>")
    url_buttons = [button for button in body.findall(".//tg-button")
                   if button.get("type") == "url"]
    assert [button.get("url") for button in url_buttons] == [
        "https://login.example.test/authorize"]
    # The verification code stays visible as a highlight-able copy button.
    copy_buttons = [button for button in body.findall(".//tg-button")
                    if button.get("type") == "copy_text"]
    assert any("ABCD-EFGH" in (button.text or "") for button in copy_buttons)


def test_standalone_provider_list_shows_summary_table_like_model_flow(ui):
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0)
                 for i in range(3)]
    state = ui._new_state(Source(), providers, current_model="vendor/model-a",
                          current_provider="p0", session_key="s", profile_home=None,
                          view="providers")
    text, _markup = ui._render(state)
    assert "<td><b>Total</b></td><td>3</td>" in text
    assert "canonical providers" not in text


def test_model_catalog_bolds_entry_counts(ui):
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    text, _markup = ui._render(state)
    assert "Entries: **2**" in text and "matching search: **2**" in text


@pytest.mark.asyncio
async def test_model_flow_back_from_provider_list_restores_model_dashboard(ui):
    adapter = ui.adapter
    source = Source()
    ms_ui = adapter._model_settings_ui()
    dashboard = {"current_model": "vendor/model-a", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 2, "reasoning_supported": False,
                 "session_settings": {}, "reasoning_effort": "provider default"}
    ms_state = ms_ui._new_state(source, "session-1", None, "dashboard",
                                context={"dashboard": dashboard, "providers": [_provider()],
                                         "current_model": "vendor/model-a",
                                         "current_provider": "openrouter"})
    ms_state["message_id"] = "55"
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=None, view="providers", message_id=55,
    )
    state = _state(ui)
    assert state.get("model_flow") is True
    assert "Back" in _labels(adapter.requests[-1].reply_markup)

    await _click(ui, source, "Back")
    assert state["id"] not in ui._states  # provider state is dropped once the dashboard owns the card
    assert "Model dashboard" in _plain_rich_text(adapter.requests[-1].rich_message)
    assert ms_ui._states[ms_state["id"]] is ms_state
