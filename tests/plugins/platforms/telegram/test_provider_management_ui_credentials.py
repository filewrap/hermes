"""Provider credentials UX: list status, Credentials count button, masked previews.

Behaviour contracts for the credentials rework:
- the /providers list shows Added / Missing per provider from its real stored
  credentials (never a guess);
- the standalone provider page has ONE Credentials button carrying the count,
  opening a credentials view with one Delete row per credential and the setup
  actions (Add / rotate API key, OAuth, host check) directly below the list;
- every credential shows a masked preview (first 3 + last 3 chars) and the full
  key or token never enters UI state, logs, or rendered text.
"""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from plugins.platforms.telegram.provider_management_ui import (
    TelegramProviderManagementUI, _credential_hint,
)
from plugins.platforms.telegram.provider_management_ui_text import (
    _credential_status, _mask_credential_hint,
)


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    platform: Platform = Platform.TELEGRAM
    message_id: str = "50"
    profile: str | None = None


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


def _labels(markup):
    return [button.text for row in markup.rows for button in row.buttons]


def _plain_rich_text(value):
    if isinstance(value, str):
        return value
    for name in ("texts", "items", "blocks"):
        children = getattr(value, name, None)
        if children is not None:
            separator = "" if name == "texts" else "\n"
            return separator.join(_plain_rich_text(child) for child in children)
    if hasattr(value, "text"):
        return _plain_rich_text(value.text)
    return ""


@pytest.mark.parametrize("value,expected", [
    ("sk-live-key-x9f2", "sk-…9f2"),
    ("sk-abc123", "sk-…123"),
    ("abc123", "ab…23"),
    ("ab12", "ab…12"),
    ("abc", ""),
    ("", ""),
    (None, ""),
])
def test_masked_credential_hint_shows_ends_only(value, expected):
    assert _mask_credential_hint(value) == expected


def test_credential_status_reads_ready_and_load_error():
    assert _credential_status({"ready": True}) == "Added"
    assert _credential_status({"ready": False}) == "Missing"
    assert _credential_status({"ready": False, "credential_load_error": "boom"}) == "Status unavailable"
    assert _credential_status({"ready": True, "credential_load_error": "boom"}) == "Added"


def test_credential_hint_masks_api_key_and_never_keeps_the_full_key():
    entry = SimpleNamespace(
        refresh_token=None, runtime_api_key="sk-full-secret-key-x9f2", access_token=None,
    )
    assert _credential_hint(entry) == "sk-…9f2"


def test_credential_hint_masks_oauth_refresh_token_not_the_access_token():
    entry = SimpleNamespace(
        refresh_token="refresh-token-abcdef", runtime_api_key="sk-access-key-1234",
        access_token="access-token-9876",
    )
    assert _credential_hint(entry) == "ref…def"


def test_credential_hint_is_empty_when_no_key_material_exists():
    entry = SimpleNamespace(refresh_token=None, runtime_api_key="", access_token=None)
    assert _credential_hint(entry) == ""


def test_credential_hint_survives_a_failing_key_property():
    class _RaisingKeyEntry:
        refresh_token = None
        access_token = None

        @property
        def runtime_api_key(self):
            raise RuntimeError("boom")

    assert _credential_hint(_RaisingKeyEntry()) == ""


def test_load_credentials_returns_masked_hint_and_never_the_full_secret(monkeypatch):
    from agent import credential_pool

    secret = "sk-full-secret-key-x9f2"
    entry = SimpleNamespace(
        id="entry-1", auth_type="api_key", source="manual", priority=0,
        last_status="ok", refresh_token=None, runtime_api_key=secret, access_token=None,
    )

    class Pool:
        def entries(self):
            return [entry]

        def peek(self):
            return entry

    monkeypatch.setattr(credential_pool, "load_pool", lambda _slug: Pool())
    ui = TelegramProviderManagementUI(FakeAdapter())
    credentials = ui._load_credentials(_provider())
    assert len(credentials) == 1
    assert credentials[0]["hint"] == "sk-…9f2"
    assert secret not in repr(credentials)
    assert secret not in repr(ui._states)


def test_load_credentials_masks_oauth_refresh_token(monkeypatch):
    from agent import credential_pool

    secret = "refresh-token-abcdef"
    entry = SimpleNamespace(
        id="entry-1", auth_type="oauth", source="device_code", priority=0,
        last_status="ok", refresh_token=secret, runtime_api_key="", access_token="access-1234",
    )

    class Pool:
        def entries(self):
            return [entry]

        def peek(self):
            return entry

    monkeypatch.setattr(credential_pool, "load_pool", lambda _slug: Pool())
    ui = TelegramProviderManagementUI(FakeAdapter())
    credentials = ui._load_credentials(_provider(slug="qwen-oauth"))
    assert credentials[0]["hint"] == "ref…def"
    assert secret not in repr(credentials)


def test_provider_list_rows_show_added_or_missing_from_real_status():
    ui = TelegramProviderManagementUI(FakeAdapter())
    # The render re-reads the pool; pin the fixture ready flags through the
    # same seams the credential tests use so this stays a rendering test.
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda provider: bool(provider.get("ready"))
    providers = [
        _provider(slug="p0", name="Has creds", is_current=True, ready=True),
        _provider(slug="p1", name="No creds", is_current=False, ready=False),
    ]
    state = ui._new_state(
        Source(), providers, current_model="vendor/model-a",
        current_provider="p0", session_key="s", profile_home=None, view="providers",
    )
    _text, markup = ui._render(state)
    labels = _labels(markup)
    assert "Current · Has creds · Added" in labels
    assert "No creds · Missing" in labels
    assert not any("· credential" in label or "· setup" in label for label in labels)


def test_credentials_button_shows_count_and_opens_credentials_view():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(ready=True)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    state["providers"][0]["credentials"] = [
        {"id": "a", "label": "Credential 1", "auth_type": "api_key", "source": "manual",
         "active": True, "priority": 0, "status": "ok", "refreshable": False, "hint": "sk-…x9f2"},
        {"id": "b", "label": "Credential 2", "auth_type": "oauth", "source": "device_code",
         "active": False, "priority": 1, "status": "ok", "refreshable": True, "hint": "ref…def"},
    ]
    _text, markup = ui._render(state)
    assert "Credentials · 2" in _labels(markup)
    assert "Setup / connect" not in _labels(markup)
    assert "Add / rotate API key" not in _labels(markup)


def test_credentials_button_counts_one_for_ready_provider_with_no_pool_entry():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(ready=True)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    _text, markup = ui._render(state)
    assert "Credentials · 1" in _labels(markup)


def test_credentials_button_counts_zero_when_nothing_is_stored():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(ready=False)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    _text, markup = ui._render(state)
    assert "Credentials · 0" in _labels(markup)


@pytest.mark.asyncio
async def test_credentials_view_has_delete_only_rows_and_direct_add(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    credentials = [
        {"id": "a", "label": "Credential 1", "auth_type": "oauth", "source": "device_code",
         "active": True, "priority": 1, "status": "exhausted", "refreshable": True, "hint": "ref…def"},
        {"id": "b", "label": "Credential 2", "auth_type": "api_key", "source": "manual",
         "active": False, "priority": 0, "status": "ok", "refreshable": False, "hint": "sk-…x9f2"},
    ]
    ui._load_credentials = lambda _provider: credentials
    ui._refresh_provider_credentials(state, ui._provider(state))
    state.update(view="credentials")
    text, markup = ui._render(state)
    labels = _labels(markup)
    assert "Delete · ref…def" in labels
    assert "Delete · sk-…x9f2" in labels
    assert "Add / rotate API key" in labels
    assert "Reset all cooldowns" in labels
    assert "Refresh OAuth" not in labels
    assert "Reset cooldown" not in labels
    assert "Move to first" not in labels
    assert "Move up" not in labels
    assert "Remove credential 1" not in labels
    assert "Add / rotate key" not in labels
    assert "Add another OAuth account" not in labels
    rendered = _plain_rich_text(text)
    assert "ref…def" in rendered
    assert "sk-…x9f2" in rendered
    assert "refresh-token" not in rendered


@pytest.mark.asyncio
async def test_add_rotate_key_opens_collect_key_directly(tmp_path, monkeypatch):
    """One tap on Add / rotate API key in the credentials view opens the
    add-key prompt: no detour through the keys view."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:credentials")
    state = _state(ui)
    assert state["view"] == "credentials"
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:add-key")
    state = _state(ui)
    assert state["view"] == "collect-key"


@pytest.mark.asyncio
async def test_keys_callback_still_opens_keys_view_for_old_cards(tmp_path, monkeypatch):
    """Cards already sent with the old keys callback still land on the keys
    view, whose own add button is unchanged."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
    )
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:credentials")
    state = _state(ui)
    assert state["view"] == "credentials"
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    assert state["view"] == "keys"
    labels = _labels(adapter.requests[-1].reply_markup)
    assert "Add / rotate API key" in labels
    assert "Credentials · 0" in labels
