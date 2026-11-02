from dataclasses import dataclass
from types import SimpleNamespace
import asyncio

import pytest

from gateway.config import Platform
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


def _callback_payload(markup, suffix):
    return next(
        button.type.data.decode("utf-8")
        for row in markup.rows
        for button in row.buttons
        if button.type.data.decode("utf-8").endswith(suffix)
    )


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


@pytest.mark.parametrize("slug,name", [("qwen-oauth", "Qwen OAuth"), ("nous", "Nous Portal")])
@pytest.mark.asyncio
async def test_provider_detail_sections_keep_inline_copy_and_existing_actions(slug, name):
    from telethon import types

    provider = _provider(
        slug=slug, name=name, auth_type="oauth", ready=True, is_current=True,
        description=f"About: {name} provider description.",
        is_profile_default=True, profile_default_model="vendor/model-a",
        default_model="vendor/model-a", default_reasoning="high", total_models=42,
        catalog_refreshed=False, catalog_status="cached_or_curated",
    )
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [provider], current_model="vendor/model-a", current_provider=slug,
        session_key="session-1", profile_home=None, view="provider",
        selected_provider=slug,
    )
    text, markup = ui._render(state)
    assert isinstance(text, types.InputRichMessage)
    rendered = _plain_rich_text(text)
    assert rendered.startswith(f"{name}\n{name} provider description.")
    assert "About:" not in rendered
    assert rendered.index(f"{name} provider description.") < rendered.index("Status & routing")
    assert "Provider ID: " + slug in rendered
    assert "Credential status: Added · 1 cred" in rendered
    assert "Current route: Selected · " + slug + " / vendor/model-a" in rendered
    assert "Profile-wide default: " + slug + " / vendor/model-a" in rendered
    assert "Setup & model defaults" in rendered
    assert "Provider-listed default/catalog value: vendor/model-a" in rendered
    assert "Model catalog" in rendered
    assert "Models available: 42" in rendered
    assert "Freshness: Cached/curated snapshot; freshness not verified on this view" in rendered
    assert "Credential note" not in rendered
    assert "About:" not in rendered

    rich_message = text

    def walk_tl_objects(value):
        if isinstance(value, types.TLObject):
            yield value
            for child in vars(value).values():
                if isinstance(child, (list, tuple)):
                    for nested in child:
                        yield from walk_tl_objects(nested)
                elif isinstance(child, types.TLObject):
                    yield from walk_tl_objects(child)

    copy_texts = [
        node.type.copy_text for node in walk_tl_objects(rich_message)
        if type(node).__name__ == "TextButton"
        and isinstance(node.type, types.InlineButtonTypeCopy)
    ]
    assert copy_texts == [slug, f"{slug} / vendor/model-a", f"{slug} / vendor/model-a", "vendor/model-a"]
    body_callbacks = [
        node for node in walk_tl_objects(rich_message)
        if isinstance(node, types.TextButton)
        and isinstance(node.type, types.InlineButtonTypeCallback)
    ]
    assert [button.type.data.decode("utf-8") for button in body_callbacks] == [
        "pv:1:credential-status",
    ]
    assert "Credential status: Added · 1 cred" in rendered
    buttons = [button for row in markup.rows for button in row.buttons]
    assert [button.text for button in buttons] == [
        "Overview", "Browse models", "Setup / connect", "Credentials", "Reasoning",
        "Make default for all", "Refresh live models", "Back", "Close",
    ]
    assert [button.type.data.decode("utf-8") for button in buttons] == [
        "pv:1:overview", "pv:1:models:0", "pv:1:keys", "pv:1:credentials", "pv:1:reasoning",
        "pv:1:pin-default-all", "pv:1:refresh", "pv:1:back", "pv:1:close",
    ]
    assert buttons[0].style.bg_primary is True
    assert buttons[1].style is None or buttons[1].style.bg_primary is not True
    assert await ui._send_new(Source(), state)
    request = ui.adapter.requests[-1]
    assert isinstance(request.reply_markup, types.ReplyInlineMarkup)
    assert bytes(request.rich_message)
    keyboard = [button for row in request.reply_markup.rows for button in row.buttons]
    assert [button.text for button in keyboard[:5]] == [
        "Overview", "Browse models", "Setup / connect", "Credentials", "Reasoning",
    ]
    assert keyboard[0].style.bg_primary is True
    assert any(isinstance(node, types.TextButton)
               and isinstance(node.type, types.InlineButtonTypeCallback)
               and node.type.data.decode("utf-8") == "pv:1:credential-status"
               for node in walk_tl_objects(request.rich_message))


@pytest.mark.asyncio
async def test_overview_navigation_returns_to_provider_summary(tmp_path, monkeypatch):
    from telethon import types

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path,
        view="provider", selected_provider="openrouter",
    )
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    assert state["view"] == "keys"
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:overview")
    state = _state(ui)
    assert state["view"] == "provider"
    assert "Status & routing" in _rich_text(adapter.requests[-1])
    markup = adapter.requests[-1].reply_markup
    assert isinstance(markup, types.ReplyInlineMarkup)
    overview = next(button for row in markup.rows for button in row.buttons if button.text == "Overview")
    assert overview.style.bg_primary is True


@pytest.mark.parametrize("ready,expected", [(True, "Added"), (False, "Missing")])
@pytest.mark.parametrize("view", ["provider", "keys", "credentials"])
@pytest.mark.asyncio
async def test_credential_status_rich_callback_is_short_and_shared_across_views(ready, expected, view):
    ui = TelegramProviderManagementUI(FakeAdapter())
    ui._load_credentials = lambda _provider: ([{
        "id": "safe-id", "auth_type": "api_key", "priority": 0, "status": "ok", "active": True,
    }] if ready else [])
    ui._has_local_auth_material = lambda _provider: False
    state = ui._new_state(
        Source(), [_provider(ready=ready)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view=view, selected_provider="openrouter",
    )
    state["message_id"] = "55"
    callback = CallbackEvent(55)

    assert await ui.handle_callback(callback, Source(), f"pv:{state['id']}:credential-status")
    assert callback.answers == [(expected, True)]
    assert ui._states[state["id"]] is state

    repeated = CallbackEvent(55)
    assert await ui.handle_callback(repeated, Source(), f"pv:{state['id']}:credential-status")
    assert repeated.answers == [(expected, True)]


@pytest.mark.asyncio
async def test_credential_health_pill_is_native_callback_and_returns_redacted_entry_status():
    from telethon import types

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    entries = [{
        "id": "safe-id", "auth_type": "oauth", "source": "device_code",
        "priority": 0, "status": "ok", "active": True,
    }]
    ui._load_credentials = lambda _provider: entries
    ui._has_local_auth_material = lambda _provider: True
    source = Source()
    state = ui._new_state(
        source, [_provider(ready=True)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="credentials", selected_provider="openrouter",
    )
    ui._refresh_provider_credentials(state, state["providers"][0])
    rich, _markup = ui._render(state)
    body_callbacks = [node for node in _walk_tl_objects(rich, types)
                      if isinstance(node, types.TextButton)
                      and isinstance(node.type, types.InlineButtonTypeCallback)]
    assert [node.type.data.decode("utf-8") for node in body_callbacks] == [
        f"pv:{state['id']}:credential-status",
        f"pv:{state['id']}:credential-status:0",
    ]
    assert "safe-id" not in _plain_rich_text(rich)

    state["message_id"] = "55"
    assert await ui._send_new(source, state)
    request = adapter.requests[-1]
    assert isinstance(request.rich_message, types.InputRichMessage)
    assert len(request.rich_message._bytes()) > 0
    assert len(request.reply_markup._bytes()) > 0
    callback = CallbackEvent(55)
    assert await ui.handle_callback(callback, source, f"pv:{state['id']}:credential-status:0")
    assert callback.answers == [("Credential 1 · OAuth · Active · Ready", True)]


def test_credential_inventory_errors_show_unavailable_not_missing(monkeypatch, caplog):
    from agent import credential_pool

    def unreadable(_slug):
        raise OSError("raw filesystem detail must not be shown")

    monkeypatch.setattr(credential_pool, "load_pool", unreadable)
    ui = TelegramProviderManagementUI(FakeAdapter())
    ui._has_local_auth_material = lambda _provider: False
    state = ui._new_state(
        Source(), [_provider(ready=False)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="provider", selected_provider="openrouter",
    )
    provider = ui._provider(state)
    ui._refresh_provider_credentials(state, provider)
    rendered = _plain_rich_text(ui._render(state)[0])
    assert "Credential status: Status unavailable" in rendered
    assert "Inventory unavailable" in rendered
    assert "Credential status: Missing" not in rendered
    assert "raw filesystem detail" not in rendered
    assert "raw filesystem detail" not in caplog.text


def test_setup_and_credentials_views_use_plain_status_and_callback_keyboard_actions():
    from telethon import types

    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(ready=False)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="keys", selected_provider="openrouter",
    )
    setup, setup_markup = ui._render(state)
    assert isinstance(setup, types.InputRichMessage)
    setup_text = _plain_rich_text(setup)
    assert "Status: Missing" in setup_text
    expected_method, instructions = ui._setup_recipe(state["providers"][0])
    assert f"Connection method: {expected_method}" in setup_text
    assert "How to connect" in setup_text
    assert instructions[0].replace("`", "") in setup_text
    assert "Choose an action" not in setup_text
    setup_body_buttons = [
        node for block in setup.blocks
        for node in _walk_tl_objects(block, types)
        if isinstance(node, types.TextButton)
    ]
    assert [(type(button.type).__name__, getattr(button.type, "url", None))
            for button in setup_body_buttons] == [
                ("InlineButtonTypeCallback", None),
                ("InlineButtonTypeUrl", "https://openrouter.ai/keys"),
            ]
    setup_labels = [button.text for row in setup_markup.rows for button in row.buttons]
    assert "Add / rotate API key" in setup_labels

    provider = state["providers"][0]
    provider["ready"] = True
    provider["credentials"] = [{
        "id": "credential-id", "label": "token-derived-label", "auth_type": "oauth",
        "source": "device_code", "priority": 0, "status": "ok", "active": True,
        "refreshable": True,
    }]
    state.update(view="credentials")
    credentials, credentials_markup = ui._render(state)
    assert isinstance(credentials, types.InputRichMessage)
    credentials_text = _plain_rich_text(credentials)
    assert "Credential 1 · OAuth" in credentials_text
    assert "Active" in credentials_text and "Ready" in credentials_text
    assert "device_code" not in credentials_text
    assert "priority" not in credentials_text.lower()
    assert "status ok" not in credentials_text.lower()
    assert "token-derived-label" not in credentials_text
    credential_callbacks = [node.type.data.decode("utf-8")
                            for block in credentials.blocks for node in _walk_tl_objects(block, types)
                            if isinstance(node, types.TextButton)
                            and isinstance(node.type, types.InlineButtonTypeCallback)]
    assert credential_callbacks == [
        f"pv:{state['id']}:credential-status",
        f"pv:{state['id']}:credential-status:0",
    ]
    credential_labels = [button.text for row in credentials_markup.rows for button in row.buttons]
    assert "Remove credential 1" in credential_labels
    assert "Refresh OAuth" in credential_labels


def _walk_tl_objects(value, types):
    if isinstance(value, types.TLObject):
        yield value
        for child in vars(value).values():
            if isinstance(child, (list, tuple)):
                for nested in child:
                    yield from _walk_tl_objects(nested, types)
            elif isinstance(child, types.TLObject):
                yield from _walk_tl_objects(child, types)


@pytest.mark.asyncio
async def test_provider_selection_renders_before_check_and_skips_when_credentials_are_missing(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: True)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)

    observed = []
    def validate_after_render(provider):
        assert len(adapter.requests) == 2
        observed.append(_rich_text(adapter.requests[-1]))
        assert "Checking provider endpoint" in observed[-1]
        assert provider["ready"] is True
        return "HTTP 401 Unauthorized"

    monkeypatch.setattr(ui, "_validate_provider_endpoint_sync", validate_after_render)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    assert observed and len(adapter.requests) == 3
    assert "Endpoint check: HTTP 401 Unauthorized" in _rich_text(adapter.requests[-1])
    assert "Credential note" not in _rich_text(adapter.requests[-1])

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    monkeypatch.setattr(
        ui, "_validate_provider_endpoint_sync",
        lambda _provider: (_ for _ in ()).throw(AssertionError("must not validate without local credentials")),
    )
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    assert len(adapter.requests) == 2
    assert "Not checked: no local credentials are configured." in _rich_text(adapter.requests[-1])


def test_openrouter_validation_uses_authenticated_key_endpoint_without_echoing_key(monkeypatch):
    from contextlib import contextmanager
    import providers
    from hermes_cli import runtime_provider, urllib_security

    secret = "sk-or-secret-never-return-this"
    profile = SimpleNamespace(supports_model_listing=True, base_url="https://openrouter.ai/api/v1")
    monkeypatch.setattr(providers, "get_provider_profile", lambda _slug: profile)
    monkeypatch.setattr(
        runtime_provider, "resolve_runtime_provider",
        lambda **_kwargs: {"api_key": secret, "base_url": profile.base_url},
    )
    requests = []

    class Response:
        @staticmethod
        def getcode():
            return 200

    @contextmanager
    def open_endpoint(request, *, timeout):
        requests.append((request.full_url, request.get_header("Authorization"), timeout))
        yield Response()

    monkeypatch.setattr(urllib_security, "open_credentialed_url", open_endpoint)
    result = TelegramProviderManagementUI._validate_provider_endpoint_sync({
        "slug": "openrouter", "ready": True,
    })
    assert result == "Key check accepted (HTTP 200)."
    assert requests == [
        ("https://openrouter.ai/api/v1/key", f"Bearer {secret}", 8.0),
    ]
    assert secret not in result


@pytest.mark.parametrize("requested,query,category", [(-1, "", "all"), (1, "", "all"), (99, "", "all"), (99, "missing <&>", "all"), (1, "", "login")])
def test_standalone_provider_pages_have_compact_structured_layout(requested, query, category):
    from xml.etree import ElementTree
    from plugins.platforms.telegram.provider_management_ui import _PROVIDER_PAGE_SIZE

    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider <{i}> & **name**",
                           is_current=i == 0, ready=i == 1,
                           auth_type="oauth" if i % 2 else "api_key")
                 for i in range(_PROVIDER_PAGE_SIZE * 2 + 1)]
    state = ui._new_state(Source(), providers, current_model="vendor/<model> & **literal**",
                          current_provider="p0", session_key="s", profile_home=None, view="providers")
    state.update(page=requested, search_query=query, filter=category)
    rendered, markup = ui._render(state)
    body = ElementTree.fromstring("<body>" + rendered + "</body>")
    children = list(body)
    visible = ui._visible_providers(state)
    pages = max(1, (len(visible) + _PROVIDER_PAGE_SIZE - 1) // _PROVIDER_PAGE_SIZE)
    page = min(max(requested, 0), pages - 1)
    assert [child.tag for child in children[:3]] == ["h1", "p", "hr"]
    assert children[0].text == "Providers"
    assert [child.tag for child in children[-2:]] == ["hr", "p"]
    assert children[-1].find("i").text == f"Page {page + 1} of {pages} • total {len(visible)}"
    assert len(body.findall("hr")) == 2
    assert [heading.text for heading in body.findall("h2")] == ["Current route"]
    assert not body.findall("h3")
    assert body.find("script") is None
    text = "".join(body.itertext())
    assert "vendor/<model> & **literal**" in text
    assert "local presence only" not in text and "no live auth validation" not in text
    items = visible[page * _PROVIDER_PAGE_SIZE:(page + 1) * _PROVIDER_PAGE_SIZE]
    assert not body.findall("ul")
    assert "Providers on this page" not in text
    # Provider names live on the regular keyboard (not in-body rich buttons),
    # and the current route shows the provider as a highlighted value and the
    # model as a copy button.
    in_body = body.findall(".//tg-button")
    assert [button.get("type") for button in in_body] == ["copy_text"]
    assert [button.text for button in in_body
            if button.get("type") == "copy_text"] == ["vendor/<model> & **literal**"]
    assert [button.get("text") for button in in_body
            if button.get("type") == "copy_text"] == ["vendor/<model> & **literal**"]
    assert bool(items) or "No providers match this filter/search." in text
    keyboard_buttons = [button for row in markup.rows for button in row.buttons]
    payloads = [button.type.data.decode("utf-8") for button in keyboard_buttons]
    expected = [i for i, _ in items]
    assert [int(p.rsplit(":", 1)[1]) for p in payloads if ":provider:" in p] == expected
    current_button = next(
        (button for button in keyboard_buttons
         if button.type.data.decode("utf-8").endswith(":provider:0")), None)
    if any(index == 0 for index, _ in items):
        assert current_button is not None and current_button.style.bg_success is True
    assert "Hints: Use /model to configure model settings. · Use /help to see all commands." in text
    keyboard = [button.text for button in keyboard_buttons]
    assert "Model catalogs" not in keyboard and "Model settings" not in keyboard


@pytest.mark.asyncio
async def test_standalone_provider_callbacks_keep_html_footer_last_and_notice_inside(tmp_path, monkeypatch):
    from xml.etree import ElementTree

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: False)
    source = Source()
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0) for i in range(9)]
    assert await ui.open(source, providers, current_model="vendor/model", current_provider="p0",
                         session_key="s", profile_home=tmp_path)
    assert type(adapter.requests[-1].rich_message).__name__ == "InputRichMessageHTML"
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:page:1")
    state = _state(ui)
    state["notice"] = "<script>not markup</script> & status"
    await ui._edit(state)
    body = ElementTree.fromstring("<body>" + adapter.requests[-1].rich_message.html + "</body>")
    assert [node.tag for node in list(body)[-3:]] == ["blockquote", "hr", "p"]
    assert body.find("blockquote").find("b").text == "Notice:"
    assert body.find("blockquote").find("b").tail == " <script>not markup</script> & status"
    assert list(body)[-1].find("i").text == "Page 2 of 2 • total 9"
    assert body.find("script") is None
    assert all(type(req.rich_message).__name__ == "InputRichMessageHTML" for req in adapter.requests)
    assert all(type(req).__name__ == "EditMessageRequest" and req.id == 55 for req in adapter.requests[1:])


def test_provider_and_model_catalogues_have_exact_page_footers(monkeypatch):
    ui = TelegramProviderManagementUI(FakeAdapter())
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home="/tmp/profile", view="providers",
    )
    ui._refresh_provider_credentials(state, state["providers"][0])
    text, markup = ui._render(state)
    assert text.rstrip().endswith("<p><i>Page 1 of 1 • total 1</i></p>")
    provider_button = next(
        button for row in markup.rows for button in row.buttons
        if "OpenRouter" in (button.text or "")
    )
    assert "setup" in provider_button.text
    assert provider_button.style.bg_success is True

    state.update(view="models", selected_provider=None, page=0)
    text, _ = ui._render(state)
    assert text.rstrip().endswith("Page 1 of 1 • total 1")

    state.update(view="models", selected_provider=None, page=0)
    text, _ = ui._render(state)
    assert text.rstrip().endswith("Page 1 of 1 • total 1")

    state.update(view="models", selected_provider="openrouter", page=0)
    text, _ = ui._render(state)
    assert text.rstrip().endswith("Page 1 of 1 • total 2")


@pytest.mark.asyncio
async def test_model_catalog_current_values_are_copyable_and_model_ids_stay_in_keyboard(tmp_path, monkeypatch):
    from telethon import types

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    model_ids = [f"vendor/model-{i:02}" for i in range(11)]
    provider = _provider(models=model_ids, total_models=len(model_ids), default_model=model_ids[0],
                        profile_default_model=model_ids[0])
    providers = [provider]
    providers.extend({
        "slug": f"provider-{i}", "name": f"Provider {i}", "configured": True,
        "is_catalog_provider": True, "models": model_ids, "total_models": len(model_ids),
    } for i in range(1, 11))
    source = Source()
    assert await ui.open(
        source, providers, current_model=model_ids[0], current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path, model_flow=True,
    )
    state = _state(ui)

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:all-models")
    catalogs = _rich_text(adapter.requests[-1])
    assert "Current values" in catalogs and "Available catalogs" in catalogs
    assert "Route:" in catalogs and "Profile-wide default:" in catalogs
    catalog_copy_texts = [node.type.copy_text for node in _walk_tl_objects(adapter.requests[-1].rich_message, types)
                          if isinstance(node, types.TextButton)
                          and isinstance(node.type, types.InlineButtonTypeCopy)]
    assert catalog_copy_texts == ["openrouter / vendor/model-00", "openrouter / vendor/model-00"]
    assert isinstance(adapter.requests[-1].reply_markup, types.ReplyInlineMarkup)

    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:models:0")
    model_page = _rich_text(adapter.requests[-1])
    state = _state(ui)
    assert "Models · OpenRouter" in model_page
    assert "Catalog" in model_page and "Current values" in model_page
    assert "Model IDs on this page" not in model_page
    assert "vendor/model-01" not in model_page
    assert "Route:" in model_page and "Profile-wide default:" in model_page
    assert "Provider-listed default:" in model_page
    model_copy_texts = [node.type.copy_text for node in _walk_tl_objects(adapter.requests[-1].rich_message, types)
                        if isinstance(node, types.TextButton)
                        and isinstance(node.type, types.InlineButtonTypeCopy)]
    assert model_copy_texts == [
        "openrouter / vendor/model-00", "openrouter / vendor/model-00", "vendor/model-00",
    ]
    model_callbacks = [button.type.data.decode("utf-8") for row in adapter.requests[-1].reply_markup.rows
                       for button in row.buttons]
    assert any(value.startswith(f"pv:{state['id']}:select:") for value in model_callbacks)
    assert "vendor/model-01" in [button.text for row in adapter.requests[-1].reply_markup.rows
                                  for button in row.buttons]

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:page:1")
    second_model_page = _rich_text(adapter.requests[-1])
    assert "vendor/model-10" not in second_model_page
    assert "Model IDs on this page" not in second_model_page


@pytest.mark.asyncio
async def test_model_settings_button_opens_existing_gateway_settings_handler(tmp_path):
    adapter = FakeAdapter()
    received = []

    async def open_settings(event):
        received.append(event)

    adapter.gateway_runner = SimpleNamespace(_handle_msettings_command=open_settings)
    ui = TelegramProviderManagementUI(adapter)
    source = Source(thread_id="72")
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:settings")
    assert len(received) == 1
    assert received[0].get_command() == "msettings"
    assert received[0].source.thread_id == "72"


@pytest.mark.asyncio
async def test_open_edits_model_dashboard_target_and_callbacks_remain_bound(tmp_path, monkeypatch):
    from telethon.tl.functions.messages import EditMessageRequest

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source(chat_id="123", user_id="123", thread_id="7")
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path, view="providers",
        message_id="77",
    )
    state = _state(ui)
    assert state["message_id"] == "77"
    assert state["chat_id"] == "123" and state["user_id"] == "123" and state["thread_id"] == "7"
    assert isinstance(adapter.requests[-1], EditMessageRequest)
    assert type(adapter.requests[-1].rich_message).__name__ == "InputRichMessageHTML"

    old_state_id = state["id"]
    assert await ui.handle_callback(CallbackEvent(77), source, f"pv:{old_state_id}:provider:0")
    state = _state(ui)
    assert state["id"] != old_state_id and state["message_id"] == "77"
    count_after_edit = len(adapter.requests)
    duplicate = CallbackEvent(77)
    assert await ui.handle_callback(duplicate, source, f"pv:{old_state_id}:provider:0")
    assert "expired" in duplicate.answers[-1][0]
    assert len(adapter.requests) == count_after_edit


@pytest.mark.asyncio
async def test_provider_cards_are_native_rich_private_and_secret_free(tmp_path):
    from telethon import types
    from telethon.tl.functions.messages import SendMessageRequest

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path, view="providers",
    )
    state = _state(ui)
    assert state["message_id"] == "55"
    assert "api_key" not in state["providers"][0]
    assert "extra_headers" not in state["providers"][0]
    request = adapter.requests[0]
    assert isinstance(request, SendMessageRequest)
    assert type(request.rich_message).__name__ == "InputRichMessageHTML"
    assert "SECRET_MUST_NEVER_ENTER_UI_STATE" not in _rich_text(request)
    assert request.reply_markup.rows

    old_id = state["id"]
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{old_id}:provider:0")
    state = _state(ui)
    assert state["id"] != old_id and state["view"] == "provider"
    provider_page = _rich_text(adapter.requests[-1])
    assert "Current route: Selected · openrouter / vendor/model-a" in provider_page
    assert "Profile-wide default: openrouter / vendor/model-a" in provider_page
    assert "Provider-listed default/catalog value: vendor/model-a" in provider_page
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    setup = _rich_text(adapter.requests[-1])
    assert "Setup / connect · OpenRouter" in setup
    assert "OpenRouter" in setup and "OPENROUTER_API_KEY" not in setup
    setup_buttons = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert "Add / rotate API key" in setup_buttons
    assert any(isinstance(node, types.TextButton)
               and isinstance(node.type, types.InlineButtonTypeCallback)
               and node.type.data.decode("utf-8") == f"pv:{state['id']}:credential-status"
               for node in _walk_tl_objects(adapter.requests[-1].rich_message, types))
    assert adapter.plain_sends == []
    assert all(type(req.rich_message).__name__ in {"InputRichMessageMarkdown", "InputRichMessage"}
               for req in adapter.requests[1:])
    assert any(type(req.rich_message).__name__ == "InputRichMessage" for req in adapter.requests[1:])


@pytest.mark.asyncio
async def test_full_canonical_catalog_and_every_auth_setup_route_are_buttoned(tmp_path):
    from hermes_cli.provider_catalog import provider_catalog

    catalog = provider_catalog()
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    providers = [{
        "slug": entry.slug,
        "name": entry.label,
        "description": entry.description,
        "auth_type": entry.auth_type,
        "api_key_env_vars": list(entry.api_key_env_vars),
        "base_url_env_var": entry.base_url_env_var,
        "signup_url": entry.signup_url,
        "catalog_order": entry.order,
        "is_catalog_provider": True,
        "configured": False,
        "models": [],
    } for entry in catalog]
    source = Source()
    assert await ui.open(
        source, providers, current_model="", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path,
    )
    state = _state(ui)
    first = _rich_text(adapter.requests[-1])
    assert f"{len(catalog)} canonical providers" in first
    assert len(state["providers"]) == len(catalog)
    assert all(item["is_catalog_provider"] for item in state["providers"])
    assert "run setup on the Hermes host" not in first.lower()

    for item in state["providers"]:
        method, instructions = ui._setup_recipe(item)
        assert method and instructions, item["slug"]
        assert not any("run setup in the cli" in instruction.casefold() for instruction in instructions), item["slug"]
        state.update(view="keys", selected_provider=item["slug"])
        _, markup = ui._render(state)
        actions = {button.type.data.decode("utf-8").split(":", 3)[2]
                   for row in markup.rows for button in row.buttons}
        actionable = (
            "add-key" in actions or "oauth" in actions or "local-check" in actions
            or "custom-create" in actions or item["slug"] == "moa"
        )
        assert actionable, (item["slug"], method, actions)

    state.update(view="providers", selected_provider=None)
    login_filter = _callback_payload(ui._render(state)[1], ":filter:login")
    assert await ui.handle_callback(CallbackEvent(55), source, login_filter)
    state = _state(ui)
    codex_index = next(i for i, item in enumerate(state["providers"]) if item["slug"] == "openai-codex")
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:{codex_index}")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    from telethon import types
    actions = [button.type.data.decode("utf-8")
               for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert any(value.endswith(":oauth") for value in actions)
    assert any(isinstance(button, types.TextButton)
               and isinstance(button.type, types.InlineButtonTypeCallback)
               and button.type.data.decode("utf-8") == f"pv:{state['id']}:credential-status"
               for button in _walk_tl_objects(adapter.requests[-1].rich_message, types))


def test_configured_providers_sort_before_catalog_and_aliases_are_not_invented():
    from hermes_cli.provider_catalog import provider_catalog

    catalog = provider_catalog()
    assert len(catalog) == len({entry.slug for entry in catalog})
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [
            {"slug": "unconfigured", "name": "Unconfigured", "configured": False, "catalog_order": 1},
            {"slug": "configured-b", "name": "Configured B", "configured": True, "catalog_order": 10},
            {"slug": "configured-a", "name": "Configured A", "configured": True, "is_current": True, "catalog_order": 20},
        ],
        current_model="m", current_provider="configured-a", session_key="s", profile_home=None,
        view="providers",
    )
    assert [row["slug"] for row in state["providers"]] == ["configured-a", "configured-b", "unconfigured"]


@pytest.mark.asyncio
async def test_api_key_is_stored_before_input_cleanup_and_never_echoed(tmp_path, monkeypatch):
    from hermes_cli import auth_commands
    stored = []
    adapter = FakeAdapter()
    def store(provider, secret, added_by=None):
        assert adapter.deleted == []
        assert added_by == "123"
        stored.append((provider, secret))
        return {"id": "cred1", "label": "key-1"}
    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui", store)
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:add-key")
    state = _state(ui)
    secret = "sk-test-never-display-this"
    assert await ui.handle_message(IncomingEvent(91), Source(message_id="91"), secret)
    assert adapter.deleted == [("123", "91")]
    assert stored == [("openrouter", secret)]
    state = _state(ui)
    assert ui._provider(state)["ready"] is True
    rendered = _rich_text(adapter.requests[-1])
    assert secret not in rendered
    assert "credential added" in rendered.lower()
    assert secret not in repr(ui._states)


@pytest.mark.asyncio
async def test_remove_credential_requires_confirm_and_uses_hermes_removal_helper(tmp_path, monkeypatch):
    from hermes_cli import auth_commands

    removed = []
    credential_store = [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]

    def remove_credential(provider, target):
        removed.append((provider, target))
        credential_store.clear()
        return {"removed": True}

    monkeypatch.setattr(auth_commands, "remove_credential_for_ui", remove_credential)
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: list(credential_store))
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: bool(credential_store))
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    assert state["providers"][0]["ready"] is True
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:credentials")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-ask:0")
    state = _state(ui)
    assert state["view"] == "remove-confirm"
    assert removed == []
    assert "Credential 1" in _rich_text(adapter.requests[-1])
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-confirm")
    assert removed == [("openrouter", "abc123")]
    assert ui._provider(state)["ready"] is False
    assert "Status: Missing" in _rich_text(adapter.requests[-1])


def test_refresh_clears_configured_when_last_credential_is_gone():
    """Configured tracks live credentials: removing the last one clears the flag.

    Before this fix "configured" was a one-way latch, so /model kept showing a
    provider as added with 0 credentials after its final credential was removed.
    """
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    store: list[dict] = [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]
    ui._load_credentials = lambda _provider: list(store)
    ui._has_local_auth_material = lambda _provider: bool(store)
    state = ui._new_state(
        Source(), [_provider(configured=True, ready=True)], current_model="vendor/model-a",
        current_provider="openrouter", session_key="session-1", profile_home=None,
        view="credentials", selected_provider="openrouter",
    )
    ui._refresh_provider_credentials(state, state["providers"][0])
    assert state["providers"][0]["ready"] is True
    assert state["providers"][0]["configured"] is True

    store.clear()
    ui._refresh_provider_credentials(state, state["providers"][0])
    assert state["providers"][0]["ready"] is False
    assert state["providers"][0]["configured"] is False


@pytest.mark.asyncio
async def test_refresh_notice_surfaces_codex_list_failure(tmp_path, monkeypatch):
    """A failed Codex model list must show up on the refresh notice, not vanish.

    The live fetch used to fail silently (stale models, no error anywhere);
    the recorded failure is now reported so the user has something to show.
    """
    from hermes_cli import codex_models

    monkeypatch.setattr(codex_models, "get_codex_model_list_error", lambda: "HTTP 429")
    adapter = FakeAdapter()

    async def snapshot(_source):
        return (
            [_provider(slug="openai-codex", name="OpenAI Codex", configured=True, is_current=True)],
            "codex-model", "openai-codex", "s", str(tmp_path),
        )

    adapter.gateway_runner = SimpleNamespace(_provider_inventory_snapshot=snapshot)
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _provider: [{
        "id": "codex-1", "label": "Credential 1", "auth_type": "oauth",
        "source": "device_code", "active": True,
    }]
    ui._has_local_auth_material = lambda _provider: True
    source = Source()
    state = ui._new_state(
        source, [_provider(slug="openai-codex", name="OpenAI Codex")],
        current_model="codex-model", current_provider="openai-codex",
        session_key="s", profile_home=tmp_path,
        view="provider", selected_provider="openai-codex",
    )
    await ui._refresh_live_catalog(state, source)
    assert "Codex model list failed (HTTP 429); showing cached models." in state["notice"]


@pytest.mark.asyncio
async def test_refresh_notice_says_no_live_list_without_credentials(tmp_path, monkeypatch):
    """Refresh is cache-first: for a provider with no credentials it must say
    so, not report success while the list stays empty."""
    adapter = FakeAdapter()

    async def snapshot(_source):
        return (
            [_provider()],
            "vendor/model-a", "openrouter", "s", str(tmp_path),
        )

    adapter.gateway_runner = SimpleNamespace(_provider_inventory_snapshot=snapshot)
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _provider: []
    ui._has_local_auth_material = lambda _provider: False
    source = Source()
    state = ui._new_state(
        source, [_provider()],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
        view="provider", selected_provider="openrouter",
    )
    await ui._refresh_live_catalog(state, source)
    assert "OpenRouter has no credentials, so there is no live model list to refresh." in state["notice"]


@pytest.mark.asyncio
async def test_models_notice_sits_above_divider_not_beside_page_footer():
    """The notice goes above the divider; the divider and the page footer stay locked together.

    The catalog used to render values, divider, Notice, Page — the notice
    sat between the divider and the page indexation it belongs above.
    """
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    state = ui._new_state(
        Source(), [_provider(models=[f"vendor/model-{i}" for i in range(12)], total_models=12)],
        current_model="vendor/model-0", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    state["message_id"] = "55"
    state["notice"] = "Live catalog refresh worked — HTTP `200`, 2 models for OpenRouter."
    await ui._edit(state)
    blocks = list(adapter.requests[-1].rich_message.blocks)
    plain = [_plain_rich_text(block) for block in blocks]
    notice_at = next(i for i, text in enumerate(plain) if text.startswith("Notice: "))
    page_at = next(i for i, text in enumerate(plain) if text.startswith("Page "))
    divider_at = max(
        i for i, block in enumerate(blocks)
        if type(block).__name__ == "PageBlockDivider" and i < page_at
    )
    assert notice_at < divider_at < page_at


@pytest.mark.asyncio
async def test_providers_notice_sits_above_divider_in_model_flow():
    """In the HTML providers view the notice also goes above the divider, not before the footer."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    state = ui._new_state(
        Source(), [_provider()],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="providers", selected_provider="openrouter",
    )
    state["model_flow"] = True
    state["message_id"] = "55"
    state["notice"] = "Live catalog refresh worked — HTTP `200`, 2 models for OpenRouter."
    await ui._edit(state)
    html = _rich_text(adapter.requests[-1])
    footer_at = html.index("Page ")
    divider_at = html.rfind("<hr />", 0, footer_at)
    assert html.index("Notice:") < divider_at < footer_at
    assert "<code>200</code>" in html


class _FakeCatalogResponse:
    """Minimal stand-in for a urllib response: status + JSON body."""

    def __init__(self, status, body):
        self._status = status
        self._body = body

    def getcode(self):
        return self._status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _live_catalog_harness(tmp_path, monkeypatch, opener):
    """State + UI whose live catalog probe hits ``opener`` instead of the network."""
    adapter = FakeAdapter()

    async def snapshot(_source):
        return (
            [_provider()],
            "vendor/model-a", "openrouter", "s", str(tmp_path),
        )

    adapter.gateway_runner = SimpleNamespace(_provider_inventory_snapshot=snapshot)
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _provider: [{
        "id": "cred-1", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]
    ui._has_local_auth_material = lambda _provider: True
    profile = SimpleNamespace(
        supports_model_listing=True, auth_type="api_key",
        models_url="https://openrouter.ai/api/v1/models",
        base_url="https://openrouter.ai/api/v1",
    )
    monkeypatch.setattr("providers.get_provider_profile", lambda slug: profile)
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {"api_key": "test-key", "base_url": "https://openrouter.ai/api/v1"},
    )
    monkeypatch.setattr("hermes_cli.urllib_security.open_credentialed_url", opener)
    source = Source()
    state = ui._new_state(
        source, [_provider()],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path,
        view="models", selected_provider="openrouter",
    )
    return adapter, ui, source, state


@pytest.mark.asyncio
async def test_refresh_notice_reports_live_success_with_status(tmp_path, monkeypatch):
    """A live refresh that answers HTTP 200 must say it worked, with the status and the count —
    and the status must render as code, not plain text."""
    import json

    body = json.dumps({"data": [{"id": "vendor/model-a"}, {"id": "vendor/model-b"}]}).encode()
    adapter, ui, source, state = _live_catalog_harness(
        tmp_path, monkeypatch, lambda request, timeout: _FakeCatalogResponse(200, body),
    )
    await ui._refresh_live_catalog(state, source)
    assert "Live catalog refresh worked" in state["notice"]
    assert "HTTP `200`" in state["notice"]
    assert "2 models for OpenRouter" in state["notice"]
    assert ui._provider(state)["catalog_refreshed"] is True

    state["message_id"] = "55"
    await ui._edit(state)
    blocks = list(adapter.requests[-1].rich_message.blocks)
    notice_block = next(
        block for block in blocks if _plain_rich_text(block).startswith("Notice: ")
    )
    fixed = [
        node for node in notice_block.text.texts
        if type(node).__name__ == "TextFixed"
    ]
    assert [_plain_rich_text(node) for node in fixed] == ["200"]


@pytest.mark.asyncio
async def test_refresh_notice_reports_live_failure_with_status(tmp_path, monkeypatch):
    """A live refresh refused with HTTP 401 must say it failed, with the status — never claim success."""
    from urllib.error import HTTPError

    def refused(request, timeout):
        raise HTTPError("https://openrouter.ai/api/v1/models", 401, "Unauthorized", None, None)

    _adapter, ui, source, state = _live_catalog_harness(tmp_path, monkeypatch, refused)
    await ui._refresh_live_catalog(state, source)
    assert "Live catalog refresh failed" in state["notice"]
    assert "HTTP `401`" in state["notice"]
    assert "showing cached models" in state["notice"]
    assert "worked" not in state["notice"]
    assert ui._provider(state)["catalog_refreshed"] is False


def _switch_runner(*, stored_session_overrides, session_overrides):
    """Fake gateway runner: real _commit_model_switch, stubbed resolver."""
    from gateway.slash_commands_model import GatewayModelCommandsMixin
    from hermes_cli.model_switch import ModelSwitchResult

    runner = object.__new__(GatewayModelCommandsMixin)
    runner._session_model_overrides = dict(session_overrides)
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._evict_cached_agent = lambda _session_key: None
    runner._switch_cached_agent_model = lambda _result, _ctx, _picker: None

    class SessionStore:
        async def set_model_override(self, session_key, value):
            stored_session_overrides[session_key] = dict(value) if value is not None else None

    runner.async_session_store = SessionStore()
    runner._normalize_source_for_session_key = lambda value: value

    async def perform(_ctx, model, provider, _source):
        return ModelSwitchResult(
            success=True, new_model=model, target_provider=provider,
            provider_label=provider, base_url="", api_mode="", is_global=True,
        ), None

    async def confirm(_result, _ctx, *, one_turn, picker, global_error=None):
        assert not one_turn and picker
        return "Model route committed."

    runner._perform_model_switch = perform
    runner._model_switch_confirmation = confirm
    return runner


@pytest.mark.asyncio
async def test_remove_confirm_clears_stale_route_and_moves_profile_default(tmp_path, monkeypatch):
    """Removing the last credential of the current provider must move the route.

    The session override pointing at the removed provider is cleared and the
    profile-wide default in config.yaml moves to the first working provider —
    the session follows the new default (one durable authority, #100314), instead
    of keeping a stale override pointing at the removed provider.
    """
    import yaml

    from hermes_cli import auth_commands

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "default": "vendor/model-a", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_mode": "openai-chat-completions",
        },
        "agent": {"reasoning_effort": "high"},
    }), encoding="utf-8")
    openrouter_store: list[dict] = [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]
    nous_store: list[dict] = [{
        "id": "nous-1", "label": "Credential 1", "auth_type": "oauth",
        "source": "device_code", "active": True,
    }]
    stores = {"openrouter": openrouter_store, "nous": nous_store}

    def remove_credential(provider, target):
        stores[provider].clear()
        return {"removed": True}

    monkeypatch.setattr(auth_commands, "remove_credential_for_ui", remove_credential)
    adapter = FakeAdapter()
    stored_session_overrides: dict = {}
    runner = _switch_runner(
        stored_session_overrides=stored_session_overrides,
        session_overrides={"session-1": {
            "model": "vendor/model-a", "provider": "openrouter", "api_key": "",
            "base_url": "", "api_mode": "", "request_overrides": {}, "capabilities": {},
        }},
    )
    adapter.gateway_runner = runner
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda provider: list(stores[provider["slug"]]))
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: bool(stores[provider["slug"]]))
    source = Source()
    providers = [
        _provider(),
        _provider(
            slug="nous", name="Nous", configured=True, is_current=False,
            is_profile_default=False, profile_default_model="",
            models=["vendor/model-b"], total_models=1, default_model="vendor/model-b",
        ),
    ]
    await ui.open(source, providers, current_model="vendor/model-a", current_provider="openrouter",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:credentials")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-ask:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-confirm")
    state = _state(ui)

    assert ui._provider(state)["ready"] is False
    assert ui._provider(state)["configured"] is False
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "nous"
    assert saved["model"]["default"] == "vendor/model-b"
    # A --global switch has one durable authority (config.yaml): the session
    # override is dropped, not re-pointed (#100314) — the session follows the
    # new profile default.
    assert "session-1" not in runner._session_model_overrides
    assert stored_session_overrides["session-1"] is None
    assert state["current_provider"] == "nous"
    assert state["current_model"] == "vendor/model-b"
    final_card = _rich_text(adapter.requests[-1])
    assert "Profile default moved to nous / vendor/model-b" in final_card


@pytest.mark.asyncio
async def test_remove_confirm_without_working_provider_clears_session_override(tmp_path, monkeypatch):
    """With no working provider left, removal must still clear the stale override.

    The session override pointing at the removed provider is dropped (not left
    dangling), the profile default stays untouched, and the notice says no
    working provider is left to move it to.
    """
    import yaml

    from hermes_cli import auth_commands

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "default": "vendor/model-a", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_mode": "openai-chat-completions",
        },
    }), encoding="utf-8")
    openrouter_store: list[dict] = [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]

    def remove_credential(provider, target):
        openrouter_store.clear()
        return {"removed": True}

    monkeypatch.setattr(auth_commands, "remove_credential_for_ui", remove_credential)
    adapter = FakeAdapter()
    stored_session_overrides: dict = {}
    runner = _switch_runner(
        stored_session_overrides=stored_session_overrides,
        session_overrides={"session-1": {
            "model": "vendor/model-a", "provider": "openrouter", "api_key": "",
            "base_url": "", "api_mode": "", "request_overrides": {}, "capabilities": {},
        }},
    )
    adapter.gateway_runner = runner
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda provider: list(openrouter_store))
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: bool(openrouter_store))
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:credentials")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-ask:0")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:remove-confirm")
    state = _state(ui)

    assert ui._provider(state)["ready"] is False
    assert ui._provider(state)["configured"] is False
    assert "session-1" not in runner._session_model_overrides
    assert stored_session_overrides["session-1"] is None
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["provider"] == "openrouter"
    final_card = _rich_text(adapter.requests[-1])
    assert "No working provider is left to move the profile default to" in final_card


@pytest.mark.asyncio
async def test_share_toggle_is_single_row_and_turns_red_when_shared(tmp_path, monkeypatch):
    """The provider page has one share row for the adder: green to share, red to stop."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="Credential 1", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra["added_by"] = "123"
    pool.add_entry(entry)

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _provider: [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]
    ui._has_local_auth_material = lambda _provider: True
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)

    def share_buttons():
        _text, markup = ui._render(state)
        rows = [
            [button for button in row.buttons]
            for row in markup.rows
            if any(button.text in {"Share with others", "Stop sharing"} for button in row.buttons)
        ]
        return rows

    rows = share_buttons()
    assert len(rows) == 1
    assert len(rows[0]) == 1
    assert rows[0][0].text == "Share with others"
    assert rows[0][0].style.bg_success is True

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:share-toggle")
    state = _state(ui)
    rows = share_buttons()
    assert len(rows) == 1
    assert rows[0][0].text == "Stop sharing"
    assert rows[0][0].style.bg_danger is True
    # _edit consumes the notice into the rendered card.
    assert "Provider shared with the other users of this profile." in _rich_text(adapter.requests[-1])
    assert load_pool("openrouter").entries()[0].extra.get("shared") is True

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:share-toggle")
    state = _state(ui)
    rows = share_buttons()
    assert rows[0][0].text == "Share with others"
    assert "Sharing stopped. Only you can use this provider now." in _rich_text(adapter.requests[-1])
    assert load_pool("openrouter").entries()[0].extra.get("shared") is False


@pytest.mark.asyncio
async def test_share_button_hidden_for_non_adder(tmp_path, monkeypatch):
    """A user who didn't add the provider never sees the share button."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="Credential 1", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra["added_by"] = "123"
    pool.add_entry(entry)

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    ui._load_credentials = lambda _provider: [{
        "id": "abc123", "label": "Credential 1", "auth_type": "api_key",
        "source": "manual", "active": True,
    }]
    ui._has_local_auth_material = lambda _provider: True
    source = Source(user_id="999")
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="session-1", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    _text, markup = ui._render(state)
    assert not any(
        button.text in {"Share with others", "Stop sharing"}
        for row in markup.rows for button in row.buttons
    )


@pytest.mark.asyncio
async def test_make_default_for_all_persists_profile_route_with_hermes_writer(tmp_path, monkeypatch):
    import yaml
    from gateway.slash_commands_model import GatewayModelCommandsMixin
    from hermes_cli.model_switch import ModelSwitchResult

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "model": {
            "default": "vendor/old-model", "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1", "api_mode": "openai-chat-completions",
        },
        "agent": {"reasoning_effort": "high"},
    }), encoding="utf-8")
    adapter = FakeAdapter()
    runner = object.__new__(GatewayModelCommandsMixin)
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._evict_cached_agent = lambda _session_key: None
    runner._switch_cached_agent_model = lambda _result, _ctx, _picker: None
    stored_session_overrides = {}

    class SessionStore:
        async def set_model_override(self, session_key, value):
            stored_session_overrides[session_key] = dict(value) if value is not None else None

    runner.async_session_store = SessionStore()
    runner._normalize_source_for_session_key = lambda value: value

    async def perform(_ctx, model, provider, _source):
        return ModelSwitchResult(
            success=True, new_model=model, target_provider=provider,
            provider_label="OpenRouter", base_url="https://openrouter.ai/api/v1",
            api_mode="openai-chat-completions", is_global=True,
        ), None

    async def confirm(_result, _ctx, *, one_turn, picker, global_error=None):
        assert not one_turn and picker
        return "Model route committed."

    runner._perform_model_switch = perform
    runner._model_switch_confirmation = confirm
    adapter.gateway_runner = runner
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_load_credentials", lambda _provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda _provider: False)
    source = Source()
    await ui.open(
        source, [_provider(is_profile_default=False, profile_default_model="")],
        current_model="vendor/selected-model", current_provider="openrouter",
        session_key="session-1", profile_home=tmp_path, view="provider", selected_provider="openrouter",
    )
    state = _state(ui)
    from telethon import types
    labels = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert "Make default for all" in labels
    assert "Profile-wide default: not pinned" in _rich_text(adapter.requests[-1])

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:pin-default-all")
    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "vendor/selected-model"
    assert saved["model"]["provider"] == "openrouter"
    assert saved["model"]["base_url"] == "https://openrouter.ai/api/v1"
    assert saved["agent"]["reasoning_effort"] == "high"
    # A --global switch has one durable authority (config.yaml): the session
    # override is dropped, not kept (#100314) — the session follows the
    # newly pinned profile default.
    assert "session-1" not in runner._session_model_overrides
    assert stored_session_overrides["session-1"] is None
    final_card = _rich_text(adapter.requests[-1])
    assert "Profile-wide default saved as openrouter / vendor/selected-model" in final_card
    assert "Profile-wide default: openrouter / vendor/selected-model" in final_card
    assert state["providers"][0]["is_profile_default"] is True
    assert state["providers"][0]["profile_default_model"] == "vendor/selected-model"
    assert adapter.requests[-1].reply_markup.rows


@pytest.mark.asyncio
async def test_oauth_device_flow_relays_verification_and_accepts_deleted_private_code(tmp_path, monkeypatch):
    from hermes_cli import auth_commands

    received = []
    def fake_auth_add(args):
        args.on_authorize_url("https://login.example.test/authorize")
        received.append(args.input_fn("authorization code"))
    monkeypatch.setattr(auth_commands, "auth_add_command", fake_auth_add)
    provider = _provider(slug="openai-codex", name="OpenAI Codex", auth_type="oauth", api_key_env_vars=["OPENAI_API_KEY"])
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [provider], current_model="codex-model", current_provider="openai-codex",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:oauth")
    state = _state(ui)
    assert state["view"] == "oauth"
    for _ in range(50):
        if state.get("oauth_needs_code"):
            break
        await asyncio.sleep(0.01)
    assert state["oauth_needs_code"]
    assert "login.example.test/authorize" in _rich_text(adapter.requests[-1])
    assert await ui.handle_message(IncomingEvent(93), Source(message_id="93"), "approved-code")
    for _ in range(50):
        if state["view"] == "provider":
            break
        await asyncio.sleep(0.01)
    assert received == ["approved-code"]
    assert adapter.deleted[-1] == ("123", "93")
    final_card = _rich_text(adapter.requests[-1])
    assert "approved-code" not in final_card
    assert "login.example.test/authorize" not in final_card


@pytest.mark.asyncio
async def test_reasoning_model_buttons_persist_model_default_and_session_only(tmp_path, monkeypatch):
    from telethon import types

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(_apply_reasoning_selection=lambda *args, **kwargs: "set")
    ui = TelegramProviderManagementUI(adapter)
    saved = []
    monkeypatch.setattr(ui, "_save_reasoning_default", lambda state, model, level: saved.append((model, level)))
    source = Source()
    await ui.open(source, [_provider(default_reasoning="")], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reasoning")
    state = _state(ui)
    assert "Save model default" not in _rich_text(adapter.requests[-1])
    assert isinstance(adapter.requests[-1].reply_markup, types.ReplyInlineMarkup)
    reasoning_buttons = [button for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    default_button = next(button for button in reasoning_buttons if button.text == "Provider default")
    assert default_button.style.bg_success is True
    body_copies = [node.type.copy_text for node in _walk_tl_objects(adapter.requests[-1].rich_message, types)
                   if isinstance(node, types.TextButton)
                   and isinstance(node.type, types.InlineButtonTypeCopy)]
    assert body_copies == ["provider default"]
    assert not any(isinstance(node, types.TextButton)
                   and isinstance(node.type, types.InlineButtonTypeCallback)
                   for node in _walk_tl_objects(adapter.requests[-1].rich_message, types))
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reason:default.high")
    state = _state(ui)
    assert saved == [("vendor/model-a", "high")]
    assert state["providers"][0]["default_reasoning"] == "high"
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reasoning")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reason:default.inherit")
    state = _state(ui)
    assert saved == [("vendor/model-a", "high"), ("vendor/model-a", None)]
    assert state["providers"][0]["default_reasoning"] == ""
    assert "provider default will be used" in _rich_text(adapter.requests[-1])
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reasoning")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reason-session")
    state = _state(ui)
    assert state["view"] == "reasoning-session"
    assert "Session reasoning" in _rich_text(adapter.requests[-1])


@pytest.mark.asyncio
async def test_model_selection_uses_existing_gateway_route_and_persists(tmp_path, monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    async def select(_state, _source, model, provider):
        _state["current_model"] = model
        _state["current_provider"] = provider
        return f"Saved {provider}/{model}"
    monkeypatch.setattr(ui, "_select_model", select)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path, view="models", selected_provider="openrouter")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:select:1")
    state = _state(ui)
    assert state["current_model"] == "vendor/model-b"
    assert state["current_provider"] == "openrouter"
    assert "Saved openrouter/vendor/model-b" in _rich_text(adapter.requests[-1])


@pytest.mark.asyncio
async def test_custom_endpoint_is_added_from_standalone_row_after_live_validation(tmp_path, monkeypatch):
    from hermes_cli import auth_commands
    import hermes_cli.main_provider_setup as provider_setup
    from hermes_cli import model_switch_providers
    import hermes_cli.models as models_module

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    operations = []
    def fetch_live(data, api_key):
        operations.append(("fetch", dict(data), api_key, list(adapter.deleted)))
        return ["live-model-a", "live-model-b"]
    monkeypatch.setattr(ui, "_fetch_custom_model_ids", fetch_live)
    monkeypatch.setattr(provider_setup, "_save_custom_provider",
                        lambda *args: operations.append(("config", args)))
    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui",
                        lambda provider, secret, added_by=None: operations.append(("credential", provider, secret,
                                                                    list(adapter.deleted))))
    monkeypatch.setattr(model_switch_providers, "_save_discovered_models_to_config",
                        lambda *args: operations.append(("discovered", args, list(adapter.deleted))))
    monkeypatch.setattr(models_module, "cached_fetch_api_models",
                        lambda *args, **kwargs: operations.append(("cache", args, kwargs,
                                                                   list(adapter.deleted))))
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter", session_key="s", profile_home=tmp_path)
    state = _state(ui)
    markup = adapter.requests[-1].reply_markup
    custom_row = next(row for row in markup.rows
                      if any(button.text == "Add custom provider" for button in row.buttons))
    assert [button.text for button in custom_row.buttons] == ["Add custom provider"]
    assert "Model settings" not in [button.text for row in markup.rows for button in row.buttons]
    assert "Use /model to configure model settings." in adapter.requests[-1].rich_message.html
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    state = _state(ui)
    assert await ui.handle_message(IncomingEvent(61), Source(message_id="61"), "my endpoint")
    assert await ui.handle_message(IncomingEvent(62), Source(message_id="62"), "http://localhost:1234/v1")
    assert _state(ui)["view"] == "collect-custom-key"
    secret = "sk-custom-live-validation"
    assert await ui.handle_message(IncomingEvent(63), Source(message_id="63"), secret)
    state = _state(ui)
    assert state["view"] == "models"
    assert state["selected_provider"] == "custom:my-endpoint"
    provider = ui._provider(state)
    assert provider["models"] == ["live-model-a", "live-model-b"]
    model_buttons = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert "live-model-a" in model_buttons and "live-model-b" in model_buttons
    assert operations[0][0] == "fetch" and operations[0][1]["base_url"] == "http://localhost:1234/v1"
    assert operations[1][0] == "config"
    assert operations[2] == ("credential", "custom:my-endpoint", secret, [])
    assert adapter.deleted == [("123", "61"), ("123", "62"), ("123", "63")]
    assert operations[3][0] == "discovered" and operations[3][2] == [
        ("123", "61"), ("123", "62"), ("123", "63"),
    ]
    assert operations[4][0] == "cache" and operations[4][3] == [
        ("123", "61"), ("123", "62"), ("123", "63"),
    ]
    assert [item[0] for item in operations] == ["fetch", "config", "credential", "discovered", "cache"]
    selected = []

    async def select_model(_state, _source, model, provider_slug):
        selected.append((model, provider_slug))
        return "Model route updated."

    monkeypatch.setattr(ui, "_select_model", select_model)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:select:0")
    assert selected == [("live-model-a", "custom:my-endpoint")]
    assert secret not in repr(ui._states)
    assert secret not in repr(ui._pending_inputs)
    assert secret not in _rich_text(adapter.requests[-1])


@pytest.mark.asyncio
async def test_custom_endpoint_real_config_and_credential_storage_are_profile_scoped(tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    from hermes_cli.config import load_config

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_fetch_custom_model_ids", lambda _data, _key: ["profile-live-model"])
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter",
                  session_key="s", profile_home=profile_a)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    await ui.handle_message(IncomingEvent(81), Source(message_id="81"), "my endpoint")
    await ui.handle_message(IncomingEvent(82), Source(message_id="82"), "https://api.example.test/v1")
    secret = "sk-profile-a-custom-key"
    await ui.handle_message(IncomingEvent(83), Source(message_id="83"), secret)

    with ui._profile_scope({"profile_home": str(profile_a)}):
        config_a = load_config()
        pool_a = load_pool("custom:my-endpoint").entries()
        config_text = (profile_a / "config.yaml").read_text(encoding="utf-8")
    assert config_a["custom_providers"][0]["name"] == "my endpoint"
    assert config_a["custom_providers"][0]["models"] == {"profile-live-model": {}}
    assert secret not in config_text
    assert len(pool_a) == 1 and pool_a[0].access_token == secret

    with ui._profile_scope({"profile_home": str(profile_b)}):
        config_b = load_config()
        pool_b = load_pool("custom:my-endpoint").entries()
    assert not config_b.get("custom_providers")
    assert pool_b == []

    with ui._profile_scope({"profile_home": str(profile_a)}):
        config_a_again = load_config()
        pool_a_again = load_pool("custom:my-endpoint").entries()
    assert config_a_again["custom_providers"][0]["name"] == "my endpoint"
    assert len(pool_a_again) == 1 and pool_a_again[0].access_token == secret


@pytest.mark.asyncio
async def test_custom_endpoint_is_not_added_when_live_model_validation_fails(tmp_path, monkeypatch):
    from hermes_cli import auth_commands
    import hermes_cli.main_provider_setup as provider_setup

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    saved = []
    stored = []
    secret = "sk-invalid-endpoint-key"
    monkeypatch.setattr(ui, "_fetch_custom_model_ids",
                        lambda _data, key: (_ for _ in ()).throw(ValueError(f"no live models for {key}")))
    monkeypatch.setattr(provider_setup, "_save_custom_provider", lambda *args: saved.append(args))
    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui",
                        lambda *args, **kwargs: stored.append(args))
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    assert await ui.handle_message(IncomingEvent(71), Source(message_id="71"), "invalid endpoint")
    assert await ui.handle_message(IncomingEvent(72), Source(message_id="72"), "https://example.test/v1")
    assert await ui.handle_message(IncomingEvent(73), Source(message_id="73"), secret)
    state = _state(ui)
    assert state["view"] == "collect-custom-key"
    assert "validation failed" in _rich_text(adapter.requests[-2]).lower()
    assert "no live models" in _rich_text(adapter.requests[-1])
    assert "[redacted API key]" in _rich_text(adapter.requests[-1])
    assert saved == [] and stored == []
    assert adapter.deleted == [("123", "71"), ("123", "72"), ("123", "73")]
    assert not any(provider["slug"] == "custom:invalid-endpoint" for provider in state["providers"])
    assert secret not in repr(ui._states) and secret not in _rich_text(adapter.requests[-1])


@pytest.mark.asyncio
async def test_long_provider_error_is_complete_redacted_split_and_rich_only():
    from xml.etree import ElementTree

    adapter = FakeAdapter()
    adapter.MAX_MESSAGE_LENGTH = 600
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    secret = "sk-upstream-error-secret"
    error = f"Provider rejected key {secret}: <html> &\n" + ("upstream diagnostic detail; " * 120) + "FINAL DETAIL"

    await ui._send_full_provider_error(source, RuntimeError(error), secret)

    assert len(adapter.requests) > 1
    assert adapter.plain_sends == []
    extracted = []
    for request in adapter.requests:
        assert type(request.rich_message).__name__ == "InputRichMessageHTML"
        assert len(request.rich_message.html) <= adapter.MAX_MESSAGE_LENGTH
        root = ElementTree.fromstring(f"<body>{request.rich_message.html}</body>")
        extracted.append(root.find("pre").text or "")
    combined = "".join(extracted)
    expected = ui._redact_provider_error(error, secret)
    assert combined == expected
    assert "[redacted API key]" in combined
    assert secret not in combined
    assert combined.endswith("FINAL DETAIL")


@pytest.mark.asyncio
async def test_custom_input_cancel_deletes_every_captured_message(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter", session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    assert await ui.handle_message(IncomingEvent(81), Source(message_id="81"), "endpoint")
    assert await ui.handle_message(IncomingEvent(82), Source(message_id="82"), "https://api.example.test/v1")
    assert await ui.handle_message(IncomingEvent(83), Source(message_id="83"), "/cancel")
    assert adapter.deleted == [("123", "81"), ("123", "82"), ("123", "83")]


@pytest.mark.asyncio
async def test_expired_custom_input_deletes_all_captured_messages(tmp_path):
    import time

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    custom = _provider(slug="custom", name="Custom", auth_type="virtual", api_key_env_vars=[], is_user_defined=True)
    source = Source()
    await ui.open(source, [custom], current_model="", current_provider="openrouter", session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:custom-create")
    assert await ui.handle_message(IncomingEvent(91), Source(message_id="91"), "endpoint")
    assert await ui.handle_message(IncomingEvent(92), Source(message_id="92"), "https://api.example.test/v1")
    pending = ui._pending_inputs[ui._user_key(source)]
    pending["created_at"] = time.monotonic() - 601
    assert not await ui.handle_message(IncomingEvent(93), Source(message_id="93"), "unused")
    await asyncio.sleep(0)
    assert adapter.deleted == [("123", "91"), ("123", "92")]


def test_custom_endpoint_live_fetch_uses_key_and_requires_models(monkeypatch):
    import providers

    calls = []

    class FakeProfile:
        def fetch_models(self, **kwargs):
            calls.append(kwargs)
            return ["live-a", "live-a", "live-b"]

    monkeypatch.setattr(providers, "get_provider_profile", lambda name: FakeProfile() if name == "custom" else None)
    result = TelegramProviderManagementUI._fetch_custom_model_ids(
        {"base_url": "https://api.example.test/v1"}, "sk-validated",
    )
    assert result == ["live-a", "live-b"]
    assert calls == [{
        "api_key": "sk-validated", "base_url": "https://api.example.test/v1", "timeout": 8.0,
    }]

    class EmptyProfile:
        def fetch_models(self, **_kwargs):
            return []

    monkeypatch.setattr(providers, "get_provider_profile", lambda _name: EmptyProfile())
    with pytest.raises(ValueError, match="live model list"):
        TelegramProviderManagementUI._fetch_custom_model_ids({"base_url": "https://api.example.test/v1"})


@pytest.mark.asyncio
async def test_provider_callbacks_are_bound_to_opening_user_message_and_dm(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    foreign = Source(user_id="999")
    callback = CallbackEvent(55)
    assert await ui.handle_callback(callback, foreign, f"pv:{state['id']}:provider:0")
    assert "another account" in callback.answers[-1][0]
    assert len(adapter.requests) == 1
    group = Source(chat_id="-100", user_id="123", chat_type="group")
    assert await ui.handle_callback(CallbackEvent(55), group, f"pv:{state['id']}:provider:0")


@pytest.mark.asyncio
async def test_provider_callback_restores_omitted_topic_only_for_exact_card(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source(thread_id="77")
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)

    callback = CallbackEvent(55)
    assert await ui.handle_callback(callback, Source(), f"pv:{state['id']}:search")
    assert callback.answers[-1] == ("", False)
    assert ("123", "123", "77") in ui._pending_inputs
    state = _state(ui)
    request_count = len(adapter.requests)

    wrong_topic = Source(thread_id="88")
    rejected = CallbackEvent(55)
    assert await ui.handle_callback(rejected, wrong_topic, f"pv:{state['id']}:search")
    assert "another account" in rejected.answers[-1][0]
    assert len(adapter.requests) == request_count


@pytest.mark.asyncio
async def test_provider_ui_never_discloses_configured_status_in_group(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    group = Source(chat_id="-100", user_id="123", chat_type="group")
    assert await ui.open(group, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                         session_key="s", profile_home=tmp_path)
    assert len(adapter.requests) == 1
    assert "direct chat" in _rich_text(adapter.requests[0]).lower()
    assert adapter.requests[0].reply_markup is None
    assert not ui._states


@pytest.mark.asyncio
async def test_device_code_oauth_displays_private_verification_and_finishes_without_chat_secret(tmp_path, monkeypatch):
    import threading
    from hermes_cli import auth_commands

    approved = threading.Event()
    def fake_auth_add(args):
        args.on_verification("https://login.example.test/device", "ABCD-EFGH")
        if not approved.wait(3):
            raise TimeoutError("approval timed out")
    monkeypatch.setattr(auth_commands, "auth_add_command", fake_auth_add)
    provider = _provider(slug="openai-codex", name="OpenAI Codex", auth_type="oauth", api_key_env_vars=[])
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [provider], current_model="codex-model", current_provider="openai-codex",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:oauth")
    state = _state(ui)
    for _ in range(100):
        if state.get("oauth_code"):
            break
        await asyncio.sleep(0.01)
    assert state["oauth_code"] == "ABCD-EFGH"
    assert "login.example.test/device" in _rich_text(adapter.requests[-1])
    assert "Verification code: ABCD-EFGH" in _rich_text(adapter.requests[-1])
    assert not state.get("oauth_needs_code")
    approved.set()
    task = next(iter(ui._oauth_jobs.values()))
    await asyncio.wait_for(task, timeout=3)
    assert state["view"] == "provider"
    assert "ABCD-EFGH" not in _rich_text(adapter.requests[-1])


@pytest.mark.asyncio
async def test_secret_is_stored_even_if_telegram_cannot_clean_up_input_message(tmp_path, monkeypatch):
    from hermes_cli import auth_commands

    stored = []
    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui",
                        lambda provider, secret, added_by=None: stored.append((provider, secret)))
    adapter = FakeAdapter()
    adapter.delete_result = False
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:add-key")
    secret = "sk-cannot-delete-this"
    assert await ui.handle_message(IncomingEvent(94), Source(message_id="94"), secret)
    assert stored == [("openrouter", secret)]
    assert secret not in repr(ui._states)
    assert secret not in _rich_text(adapter.requests[-1])
    assert "could not delete" not in _rich_text(adapter.requests[-1]).lower()


@pytest.mark.asyncio
async def test_auth_store_failure_is_secret_safe_in_chat_and_logs(tmp_path, monkeypatch, caplog):
    from hermes_cli import auth_commands

    secret = "sk-storage-error-secret"
    def fail_store(_provider, _value, added_by=None):
        raise ValueError(f"rejected {secret}")
    monkeypatch.setattr(auth_commands, "add_api_key_credential_for_ui", fail_store)
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:add-key")
    assert await ui.handle_message(IncomingEvent(95), Source(message_id="95"), secret)
    assert secret not in repr(ui._states)
    assert secret not in _rich_text(adapter.requests[-1])
    assert secret not in caplog.text
    assert "ValueError" in _rich_text(adapter.requests[-1])
    assert adapter.deleted == []


@pytest.mark.asyncio
async def test_host_native_status_checks_hermes_presence_without_model_request(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda _slug: True)
    provider = _provider(slug="bedrock", name="AWS Bedrock", auth_type="aws_sdk", api_key_env_vars=[])
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [provider], current_model="anthropic.claude-3", current_provider="bedrock",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:keys")
    state = _state(ui)
    from telethon import types
    setup_buttons = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert "Check host credential status" in setup_buttons
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:local-check")
    card = _rich_text(adapter.requests[-1])
    assert "Hermes detects a configured host credential chain" in card
    assert "model request" in card
    assert "AWS_SECRET" not in card


def test_ui_auth_helpers_use_hermes_secure_pool_and_remove_only_the_selected_credential(tmp_path, monkeypatch):
    from agent.credential_pool import load_pool
    from hermes_cli.auth_commands import (
        add_api_key_credential_for_ui, remove_credential_for_ui,
        reset_credential_status_for_ui, set_credential_priority_for_ui,
    )

    home = tmp_path / "isolated-hermes-profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    secret = "sk-real-pool-integration-test"
    added = add_api_key_credential_for_ui("openrouter", secret)
    sibling = add_api_key_credential_for_ui("openrouter", "sk-sibling-integration-test")
    assert "label" not in added
    assert secret not in repr(added)
    pool = load_pool("openrouter")
    entries = pool.entries()
    assert len(entries) == 2
    assert {entry.access_token for entry in entries} == {secret, "sk-sibling-integration-test"}
    auth_file = home / "auth.json"
    assert auth_file.exists()
    assert auth_file.stat().st_mode & 0o077 == 0
    priority_result = set_credential_priority_for_ui("openrouter", added["id"], 1)
    assert "label" not in priority_result
    assert [entry.id for entry in load_pool("openrouter").entries()] == [sibling["id"], added["id"]]
    reset_result = reset_credential_status_for_ui("openrouter")
    assert reset_result["provider"] == "openrouter"
    assert reset_result["count"] >= 0
    removed = remove_credential_for_ui("openrouter", added["id"])
    assert "label" not in removed
    assert secret not in repr(removed)
    remaining = load_pool("openrouter").entries()
    assert len(remaining) == 1 and remaining[0].id == sibling["id"]


@pytest.mark.asyncio
async def test_credential_card_wires_priority_refresh_and_cooldown_cli_operations(tmp_path, monkeypatch):
    from hermes_cli import auth_commands

    calls = []
    monkeypatch.setattr(auth_commands, "set_credential_priority_for_ui",
                        lambda provider, target, priority: calls.append(("priority", provider, target, priority)) or {"priority": priority})
    monkeypatch.setattr(auth_commands, "reset_credential_status_for_ui",
                        lambda provider, target=None: calls.append(("reset", provider, target)) or {"count": 1})
    monkeypatch.setattr(auth_commands, "refresh_oauth_credential_for_ui",
                        lambda provider, target: calls.append(("refresh", provider, target)) or {"status": "ok"})

    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    state.update(view="credentials", selected_provider="openrouter")
    credentials = [
        {"id": "credential-a", "label": "Credential 1", "auth_type": "oauth", "source": "manual",
         "active": True, "priority": 0, "status": "exhausted", "refreshable": True},
        {"id": "credential-b", "label": "Credential 2", "auth_type": "api_key", "source": "manual",
         "active": False, "priority": 1, "status": "ok", "refreshable": False},
    ]
    ui._load_credentials = lambda _provider: credentials
    ui._refresh_provider_credentials(state, ui._provider(state))
    text, markup = ui._render(state)
    from telethon import types
    rich_buttons = [node for block in text.blocks for node in _walk_tl_objects(block, types)
                    if isinstance(node, types.TextButton)
                    and isinstance(node.type, types.InlineButtonTypeCallback)]
    button_texts = [button.text for row in markup.rows for button in row.buttons]
    assert [button.type.data.decode("utf-8") for button in rich_buttons] == [
        f"pv:{state['id']}:credential-status",
        f"pv:{state['id']}:credential-status:0",
        f"pv:{state['id']}:credential-status:1",
    ]
    assert {"Refresh OAuth", "Reset cooldown", "Move to first"} <= set(button_texts)
    assert "Reset all cooldowns" in button_texts
    assert "credential-a" not in _plain_rich_text(text) and "credential-b" not in _plain_rich_text(text)

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:priority:credential-b,0")
    state = _state(ui)
    assert "Credential priority updated" in _rich_text(adapter.requests[-1])
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:refresh-oauth:credential-a")
    state = _state(ui)
    assert "OAuth refresh finished" in _rich_text(adapter.requests[-1])
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reset-one:credential-a")
    state = _state(ui)
    assert "Reset cooldown status for 1 credential" in _rich_text(adapter.requests[-1])
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reset-all")
    assert calls == [
        ("priority", "openrouter", "credential-b", 0),
        ("refresh", "openrouter", "credential-a"),
        ("reset", "openrouter", "credential-a"),
        ("reset", "openrouter", None),
    ]
    assert "sk-" not in repr(ui._states)


def test_oauth_refresh_ui_wrapper_returns_only_status_metadata(monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import auth_commands

    secret = "refresh-token-never-return-this"
    matched = SimpleNamespace(id="safe-id", auth_type="oauth", refresh_token=secret,
                              source="manual:device_code")
    refreshed = SimpleNamespace(id="safe-id", last_status="ok")

    class Pool:
        def resolve_target(self, target):
            assert target == "safe-id"
            return 0, matched, None
        def try_refresh_matching(self, *, credential_id):
            assert credential_id == "safe-id"
            return refreshed

    monkeypatch.setattr(auth_commands, "load_pool", lambda _provider: Pool())
    result = auth_commands.refresh_oauth_credential_for_ui("openai-codex", "safe-id")
    assert result == {"provider": "openai-codex", "id": "safe-id", "status": "ok"}
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_reasoning_controls_are_hidden_and_rejected_when_model_disallows_them(tmp_path):
    provider = _provider(reasoning_supported=False, reasoning_efforts=[])
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [provider], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path)
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:provider:0")
    state = _state(ui)
    await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reasoning")
    state = _state(ui)
    from telethon import types
    buttons = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert buttons == ["Overview", "Browse models", "Setup / connect", "Credentials", "Reasoning",
                       "Back", "Close"]
    assert not any(isinstance(button, types.TextButton)
                   and isinstance(button.type, types.InlineButtonTypeCallback)
                   for button in _walk_tl_objects(adapter.requests[-1].rich_message, types))
    assert "does not support reasoning controls" in _rich_text(adapter.requests[-1])

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:reason:default.high")
    assert "ValueError" in _rich_text(adapter.requests[-1])
    assert not (tmp_path / "config.yaml").exists()


@pytest.mark.parametrize("view,selected", [("providers", None), ("models", None), ("models", "p0")])
def test_catalog_navigation_bounds_and_final_footer(view, selected, monkeypatch):
    from plugins.platforms.telegram.provider_management_ui import _PAGE_SIZE, _PROVIDER_PAGE_SIZE

    ui = TelegramProviderManagementUI(FakeAdapter())
    monkeypatch.setattr(ui, "_load_credentials", lambda provider: [])
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: False)
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}",
                           models=[f"model-{j}" for j in range(_PAGE_SIZE * 2 + 1)])
                 for i in range(_PROVIDER_PAGE_SIZE * 2 + 1)]
    state = ui._new_state(Source(), providers, current_model="model-0", current_provider="p0",
                          session_key="session-1", profile_home="/tmp/profile", view=view)
    state["selected_provider"] = selected
    total = len(providers[0]["models"]) if selected else len(providers)
    size = _PAGE_SIZE if selected else _PROVIDER_PAGE_SIZE
    pages = (total + size - 1) // size
    for requested in [-1, *range(pages), pages + 10]:
        state["page"] = requested
        rendered, markup = ui._render(state)
        page = min(max(requested, 0), pages - 1)
        assert state["page"] == page
        footer = f"Page {page + 1} of {pages} • total {total}"
        if rendered.lstrip().startswith("<h"):
            assert rendered.rstrip().endswith(f"<p><i>{footer}</i></p>")
        else:
            assert rendered.rstrip().endswith(footer)
        assert rendered.count("Page ") == 1
        payloads = [button.type.data.decode() for row in markup.rows for button in row.buttons]
        targets = [int(payload.rsplit(":", 1)[1]) for payload in payloads if ":page:" in payload]
        assert targets == ([page - 1] if page else []) + ([page + 1] if page + 1 < pages else [])
        assert all(0 <= target < pages for target in targets)


@pytest.mark.asyncio
async def test_model_catalog_notice_is_escaped_and_keeps_footer_last(monkeypatch):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    monkeypatch.setattr(ui, "_has_local_auth_material", lambda provider: False)
    assert await ui.open(Source(), [_provider()], current_model="vendor/model-a",
                         current_provider="openrouter", session_key="s", profile_home=None,
                         view="models", message_id=55)
    state = _state(ui)
    state["notice"] = "<script>not markup</script> & status"
    await ui._edit(state)
    rich = adapter.requests[-1].rich_message
    assert type(rich).__name__ == "InputRichMessage"
    rendered = _plain_rich_text(rich)
    assert "Notice: <script>not markup</script> & status" in rendered
    assert rendered.rfind("Notice:") < rendered.rfind("Page 1 of 1 • total 1")
    assert _plain_rich_text(rich.blocks[-1]).startswith("Page 1 of 1 • total 1")
    assert state.get("notice") is None
    assert all(type(request).__name__ == "EditMessageRequest" for request in adapter.requests)
    assert all(request.id == 55 for request in adapter.requests)

    async def fail_edit(_request):
        raise RuntimeError("simulated Telegram edit failure")

    monkeypatch.setattr(adapter, "_mtproto_rich_request", fail_edit)
    state["notice"] = "Action saved; retry card update"
    with pytest.raises(RuntimeError):
        await ui._edit(state)
    assert state["notice"] == "Action saved; retry card update"


@pytest.mark.asyncio
async def test_non_dm_provider_handoff_edits_privacy_notice_without_new_message():
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    assert await ui.open(Source(chat_type="group", chat_id="-100"), [_provider()],
                         current_model="vendor/model-a", current_provider="openrouter",
                         session_key="s", profile_home=None, message_id=55)
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert type(request).__name__ == "EditMessageRequest" and request.id == 55
    assert "Open a direct chat" in _rich_text(request)
    assert not ui._states


@pytest.mark.asyncio
async def test_close_deletes_only_card_and_exact_bound_invocation_after_authorization(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    assert await ui.open(
        source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=tmp_path, reply_to=888, invocation_message_id=777,
    )
    state = _state(ui)
    assert state["invocation_message_id"] == "777"
    assert state["invocation_chat_id"] == "123" and state["invocation_user_id"] == "123"

    assert await ui.handle_callback(CallbackEvent(55), Source(chat_id="124"), f"pv:{state['id']}:close")
    assert await ui.handle_callback(CallbackEvent(55), Source(user_id="999"), f"pv:{state['id']}:close")
    assert adapter.deleted == []
    assert state["id"] in ui._states

    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:close")
    assert adapter.deleted == [("123", "55"), ("123", "777")]
    assert not ui._states
    assert len(adapter.requests) == 1  # Close deletes the attached card, not an edited tombstone.


def test_standalone_models_close_and_model_dashboard_keeps_back_navigation():
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider()], current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models",
    )
    _, markup = ui._render(state)
    labels = [button.text for row in markup.rows for button in row.buttons]
    assert "Close" in labels and "Back" in labels
    assert any(button.type.data.decode().endswith(":back")
               for row in markup.rows for button in row.buttons)

    state["model_flow"] = True
    _, markup = ui._render(state)
    labels = [button.text for row in markup.rows for button in row.buttons]
    assert "Back" in labels and "Close" in labels
    assert any(button.type.data.decode().endswith(":back")
               for row in markup.rows for button in row.buttons)


def test_free_models_are_green_and_selection_markers_remain_distinct():
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    free_ids = ["vendor/model-a:free", "vendor/model-b:free", "vendor/model-c"]
    providers = [
        _provider(slug="openrouter", models=free_ids, total_models=3, is_current=True,
                  default_model=free_ids[0]),
        _provider(slug="other", name="Other", models=["other/model"], total_models=1,
                  is_current=False),
    ]
    state = ui._new_state(
        Source(), providers, current_model=free_ids[0], current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider=None,
    )
    catalogs, provider_markup = ui._render(state)
    catalog_buttons = [button for row in provider_markup.rows for button in row.buttons]
    other_button = next(button for button in catalog_buttons if button.type.data.decode().endswith(":models:1"))
    current_button = next(button for button in catalog_buttons if button.type.data.decode().endswith(":models:0"))
    assert "Other" in other_button.text
    assert other_button.style.bg_primary is not True
    assert "Current · OpenRouter" in current_button.text
    assert current_button.style.bg_success is True
    assert catalogs.count("vendor/model-a:free") >= 1
    assert "Green marks :free models" not in catalogs

    state.update(view="providers", selected_provider="other")
    _provider_picker_text, provider_picker_markup = ui._render(state)
    selected_provider_button = next(
        button for row in provider_picker_markup.rows for button in row.buttons
        if button.type.data.decode("utf-8").endswith(":provider:1")
    )
    assert "Selected · Other" in (selected_provider_button.text or "")
    # Provider names are regular keyboard buttons now; the selected one reads
    # blue, marked by its "Selected · " prefix.
    assert selected_provider_button.style.bg_primary is True

    state.update(view="models", selected_provider="openrouter")
    model_text, model_markup = ui._render(state)
    assert "vendor/model-a:free" in model_text
    assert "Green marks the current model and :free models" in model_text
    model_buttons = [button for row in model_markup.rows for button in row.buttons]
    selected_free = next(button for button in model_buttons if button.type.data.decode().endswith(":select:0"))
    free_button = next(button for button in model_buttons if button.type.data.decode().endswith(":select:1"))
    assert selected_free.text.startswith("Current ·") and selected_free.style.bg_success is True
    assert free_button.style.bg_success is True
    paid_button = next(button for button in model_buttons if button.type.data.decode().endswith(":select:2"))
    assert paid_button.style is None or paid_button.style.bg_success is not True


@pytest.mark.asyncio
async def test_refresh_marks_nous_guest_identity_configured(tmp_path, monkeypatch):
    """A Nous free-tier guest identity (anon_token only, no pool entry) is a
    working login: turns mint an invoke JWT from it. The provider page must
    not report it as Missing."""
    import json

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {"nous": {"anon_token": "anon_x", "auth_method": "anonymous", "account_tier": "free"}},
        "credential_pool": {},
    }))
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(slug="nous", name="Nous Portal")],
        current_model="", current_provider="nous",
        session_key="s", profile_home=profile, view="provider", selected_provider="nous",
    )
    ui._refresh_provider_credentials(state, state["providers"][0])
    row = state["providers"][0]
    assert row["credentials"] == []
    assert row["ready"] is True
    assert row["configured"] is True


@pytest.mark.asyncio
async def test_refresh_reads_credentials_from_profile_home_not_ambient(tmp_path, monkeypatch):
    """Credential reads run under the profile's scope (as the snapshot does):
    a pool entry under the profile home must be found even when the ambient
    home has no credentials."""
    import json

    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "auth.json").write_text(json.dumps({
        "version": 1,
        "credential_pool": {"nous": [{
            "id": "n1", "source": "device_code", "auth_type": "oauth",
            "access_token": "at", "refresh_token": "rt",
        }]},
    }))
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {}}))
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(slug="nous", name="Nous Portal")],
        current_model="", current_provider="nous",
        session_key="s", profile_home=profile, view="provider", selected_provider="nous",
    )
    ui._refresh_provider_credentials(state, state["providers"][0])
    row = state["providers"][0]
    assert len(row["credentials"]) == 1
    assert row["credentials"][0]["source"] == "device_code"
    assert row["ready"] is True


class _FakeSwitchRunner:
    """Gateway runner stub for _select_model: switch fails with a real reason."""

    def __init__(self, error):
        self._error = error
        self._session_model_overrides = {}

    def _normalize_source_for_session_key(self, source):
        return source

    async def _perform_model_switch(self, ctx, model, provider_slug, source):
        return None, self._error

    async def _commit_model_switch(self, result, ctx, source, picker=False):
        raise AssertionError("commit must not run after a failed switch")


@pytest.mark.asyncio
async def test_select_model_surfaces_real_switch_error_not_bare_runtime_error(tmp_path):
    """A failed switch must carry its reason: the card used to show only
    "failed safely (RuntimeError)" for every non-Nous failure."""
    adapter = FakeAdapter()
    adapter.gateway_runner = _FakeSwitchRunner(
        "Groq is not connected: no API key or login was found for it. "
        "Add one with `hermes auth add groq`."
    )
    ui = TelegramProviderManagementUI(adapter)
    state = ui._new_state(
        Source(), [_provider(slug="groq", name="Groq")],
        current_model="nous/welcome", current_provider="nous",
        session_key="s", profile_home=tmp_path, view="models", selected_provider="groq",
    )
    with pytest.raises(RuntimeError) as excinfo:
        await ui._select_model(state, Source(), "llama-3.3-70b-instruct", "groq")
    assert "Groq is not connected" in str(excinfo.value)
    assert "hermes auth add groq" in str(excinfo.value)


@pytest.mark.asyncio
async def test_failed_model_select_shows_reason_on_card(tmp_path, monkeypatch):
    """The models card must show WHY a selection failed, not the generic
    "failed safely (RuntimeError)" notice."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)

    async def failing_select(_state, _source, model, provider_slug):
        raise RuntimeError(
            "Anthropic is not connected: no API key or login was found for it. "
            "Add one with `hermes auth add anthropic`."
        )

    monkeypatch.setattr(ui, "_select_model", failing_select)
    source = Source()
    await ui.open(source, [_provider()], current_model="vendor/model-a", current_provider="openrouter",
                  session_key="s", profile_home=tmp_path, view="models", selected_provider="openrouter")
    state = _state(ui)
    assert await ui.handle_callback(CallbackEvent(55), source, f"pv:{state['id']}:select:1")
    assert "Anthropic is not connected" in _rich_text(adapter.requests[-1])
    assert "hermes auth add anthropic" in _rich_text(adapter.requests[-1])
    assert "failed safely (RuntimeError)" not in _rich_text(adapter.requests[-1])


def test_current_model_button_is_green_in_models_view():
    """The selected/current model button must be green (success), like the
    picker, the provider list, and the current-route buttons — not blue."""
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    state = ui._new_state(
        Source(),
        [_provider(models=["vendor/model-a", "vendor/model-b"], total_models=2, is_current=True)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    _, markup = ui._render(state)
    buttons = [button for row in markup.rows for button in row.buttons]
    current = next(button for button in buttons if button.type.data.decode().endswith(":select:0"))
    assert current.text.startswith("Current ·")
    assert current.style.bg_success is True
    other = next(button for button in buttons if button.type.data.decode().endswith(":select:1"))
    assert other.style is None or other.style.bg_success is not True


def test_models_view_current_route_values_are_highlighted():
    """The current route on the models screens must be highlighted (bold),
    not plain backticked text."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(models=["vendor/model-a", "vendor/model-b"], total_models=2)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider=None,
    )
    catalogs, _ = ui._render(state)
    assert "**`openrouter / vendor/model-a`**" in catalogs
    assert "Providers with models: **1**" in catalogs

    state.update(view="models", selected_provider="openrouter")
    model_text, _ = ui._render(state)
    assert "**`openrouter / vendor/model-a`**" in model_text
    assert "Entries: **2**" in model_text


def test_models_view_has_back_to_catalog_list():
    """A per-provider model list must offer a way back to the catalog list."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(
        Source(), [_provider(models=["vendor/model-a", "vendor/model-b"], total_models=2)],
        current_model="vendor/model-a", current_provider="openrouter",
        session_key="s", profile_home=None, view="models", selected_provider="openrouter",
    )
    _, markup = ui._render(state)
    assert any(button.type.data.decode().endswith(":all-models")
               for row in markup.rows for button in row.buttons)


def test_providers_list_uses_heading_filter_button_only_and_no_credential_line():
    """Providers list: Current route is a heading, the selected filter lives
    only on its keyboard button (not duplicated in body text), the credential
    disclaimer is gone, and hints are the last block before the footer."""
    from xml.etree import ElementTree

    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0)
                 for i in range(9)]
    state = ui._new_state(Source(), providers, current_model="vendor/model",
                          current_provider="p0", session_key="s", profile_home=None,
                          view="providers")
    state.update(filter="all", page=0)
    rendered, markup = ui._render(state)
    body = ElementTree.fromstring("<body>" + rendered + "</body>")
    text = "".join(body.itertext())
    assert "local presence only" not in text
    assert "no live auth validation" not in text
    assert [child.text for child in body if child.tag == "h2"] == ["Current route"]
    assert "<b>Current route</b>" not in rendered
    assert "All •" not in text
    assert "Filter:" not in text
    assert "9 matching providers" in text
    children = list(body)
    assert [child.tag for child in children[-3:]] == ["p", "hr", "p"]
    assert "Hints:" in "".join(children[-3].itertext())
    assert children[-1].find("i").text == "Page 1 of 2 • total 9"
    keyboard = [button for row in markup.rows for button in row.buttons]
    all_buttons = [button for button in keyboard if button.text == "All"]
    assert len(all_buttons) == 1
    assert all_buttons[0].style.bg_primary is True


def test_provider_list_buttons_are_full_width_with_complete_labels():
    """Provider list buttons are one full-width button per row, so the current
    provider's label is never cut off mid-word."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0,
                           ready=True) for i in range(9)]
    state = ui._new_state(Source(), providers, current_model="vendor/model",
                          current_provider="p0", session_key="s", profile_home=None,
                          view="providers")
    state.update(filter="all", page=0)
    _rendered, markup = ui._render(state)
    provider_rows = [
        [button for button in row.buttons if ":provider:" in button.type.data.decode("utf-8")]
        for row in markup.rows
    ]
    provider_rows = [row for row in provider_rows if row]
    assert len(provider_rows) == 8
    assert all(len(row) == 1 for row in provider_rows)
    current = next(button for row in provider_rows for button in row
                   if button.type.data.decode("utf-8").endswith(":provider:0"))
    assert current.text == "Current · Provider 0 · credential"
    assert current.style.bg_success is True


def test_custom_form_fields_are_on_separate_lines_with_plain_note():
    """Add-custom screen: Name and Base URL are on separate lines, and the
    explanatory note is short and plain."""
    ui = TelegramProviderManagementUI(FakeAdapter())
    state = ui._new_state(Source(), [_provider()], current_model="vendor/model",
                          current_provider="openrouter", session_key="s",
                          profile_home=None, view="custom-form")
    state.update(custom_form={}, custom_step="name")
    rendered, _markup = ui._render(state)
    assert "Hermes checks the endpoint's live model list before adding it" not in rendered
    assert "secure credential pool" not in rendered
    assert "The model list is checked before adding." in rendered
    assert "Keys are stored securely." in rendered
    paragraphs = [part.strip() for part in rendered.split("\n\n") if part.strip()]
    assert any(part.startswith("Name:") for part in paragraphs)
    assert any(part.startswith("Base URL:") for part in paragraphs)
    assert not any("Name:" in part and "Base URL:" in part for part in paragraphs)
    assert "Send the endpoint name in your next direct message." in rendered


def test_model_flow_providers_list_has_no_credential_line_or_duplicate_filter():
    """Model-flow providers list: no credential disclaimer, and the selected
    filter is not duplicated in the body."""
    from xml.etree import ElementTree

    ui = TelegramProviderManagementUI(FakeAdapter())
    providers = [_provider(slug=f"p{i}", name=f"Provider {i}", is_current=i == 0)
                 for i in range(9)]
    state = ui._new_state(Source(), providers, current_model="vendor/model",
                          current_provider="p0", session_key="s", profile_home=None,
                          view="providers")
    state.update(filter="all", page=0, model_flow=True)
    rendered, _markup = ui._render(state)
    body = ElementTree.fromstring("<body>" + rendered + "</body>")
    text = "".join(body.itertext())
    assert "local presence only" not in text
    assert "no live auth validation" not in text
    assert "Filter:" not in text
    assert "9 matching providers" in text
    assert "Hints:" in text
