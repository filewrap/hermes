"""Focused tests for Telegram provider, model, and Dashboard management menus."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.platforms.telegram import telegram_management as management


def test_provider_catalog_includes_custom_endpoints_without_retaining_secrets(monkeypatch):
    from hermes_cli import config
    from hermes_cli import provider_catalog
    from hermes_cli.web_routers import oauth

    monkeypatch.setattr(config, "load_config", lambda: {
        "providers": {
            "my-local-model": {
                "name": "My Local Model",
                "base_url": "https://models.example.test/v1?token=do-not-retain",
                "model": "local-chat",
                "discover_models": True,
                "models": {"local-chat": {}},
                "api_key": "never-retain-this-key",
                "key_env": "HERMES_CUSTOM_MY_LOCAL_MODEL_API_KEY",
            },
        },
    })
    monkeypatch.setattr(provider_catalog, "provider_catalog", list)
    monkeypatch.setattr(oauth, "_build_oauth_catalog", list)

    items = management._provider_catalog_sync()
    item = next(row for row in items if row["slug"] == "my-local-model")

    assert item["custom_endpoint"] is True
    assert item["configured_base_url"] is True
    assert item["configured_inline_key"] is True
    assert item["env_vars"] == ["HERMES_CUSTOM_MY_LOCAL_MODEL_API_KEY"]
    serialized = repr(item)
    assert "do-not-retain" not in serialized
    assert "never-retain-this-key" not in serialized
    assert management._provider_is_configured(None, {
        "slug": "oauth-endpoint", "pool_configured": False,
        "oauth_status_loaded": True, "oauth_logged_in": False,
        "configured_base_url": True,
    }) is True


def test_provider_list_uses_structured_html_and_preserves_inline_controls():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    text, markup = management._provider_list_view(
        "state123",
        [{"slug": "alpha", "name": "Alpha", "configured": True}],
        0,
        heading="Providers & models",
    )
    rich_message = input_rich_message(text)
    buttons = [button for row in markup.rows for button in row.buttons]

    assert type(rich_message).__name__ == "InputRichMessageHTML"
    assert "<h1>Providers &amp; models</h1>" in text
    assert "<p>Browse providers Hermes supports." in text
    assert "<hr />" in text and "Page 1 of 1 · 1 providers" in text
    assert [button.text for button in buttons] == [
        "✓ Alpha",
        "Models",
        "Dashboard",
        "Add custom endpoint",
        "Close",
    ]
    assert buttons[0].type.data.decode("utf-8") == "hm:providers:view:state123:0"
    assert buttons[-1].type.data.decode("utf-8") == "hm:management:close"


def test_provider_list_escapes_search_heading_and_no_match_message():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message

    text, _markup = management._provider_list_view(
        "state123",
        [],
        0,
        heading="Providers matching A & B",
        empty_message="No match for <A&B>.",
    )

    assert type(input_rich_message(text)).__name__ == "InputRichMessageHTML"
    assert "<h1>Providers matching A &amp; B</h1>" in text
    assert "<p>No match for &lt;A&amp;B&gt;.</p>" in text


def test_miniapp_menu_links_to_dashboard_sections_and_preserves_reverse_proxy_prefix():
    text, markup = management._miniapp_view("https://dashboard.example.test/hermes")

    assert "Dashboard" in text
    assert "separately registered Telegram Web App is not configured" in text
    links = {
        button.text: button.type.url
        for row in markup.rows
        for button in row.buttons
        if hasattr(button.type, "url")
    }
    assert links["Chat"] == "https://dashboard.example.test/hermes/chat"
    assert links["Models"] == "https://dashboard.example.test/hermes/models"
    assert links["OAuth accounts"] == "https://dashboard.example.test/hermes/env#section-oauth"
    assert links["API keys"] == "https://dashboard.example.test/hermes/env#section-providers"
    assert links["Config"] == "https://dashboard.example.test/hermes/config"


def test_provider_credentials_view_exposes_presence_only_status_and_private_actions():
    item = {
        "slug": "demo", "name": "Demo Provider", "auth_type": "api_key",
        "env_vars": ["DEMO_API_KEY"], "env_status": {"DEMO_API_KEY": True},
        "setup_vars": ["AWS_PROFILE"], "setup_status": {"AWS_PROFILE": False},
        "pool_entries": [{"label": "pool-one", "status": "ready"}],
        "oauth_flow": "device_code", "oauth_logged_in": False,
    }
    text, markup = management._provider_credentials_view(
        item, can_manage=True, in_dm=True, token="state123", index=2,
    )
    labels = [button.text for row in markup.rows for button in row.buttons]

    assert "DEMO_API_KEY` — set" in text
    assert "AWS_PROFILE` — not set" in text
    assert "Pooled credentials: **1**" in text
    assert "device" not in text.lower() or "Account sign-in" in text
    assert "Set DEMOAPIKEY" in labels
    assert "Add pooled API key" in labels
    assert "Connect account" in labels
    assert "‹ Provider" in labels and "Close" in labels
    assert "secret-value" not in text


def test_endpoint_url_validation_rejects_unsafe_or_remote_http_urls():
    assert management._is_safe_endpoint_url("https://api.example.test/v1") is True
    assert management._is_safe_endpoint_url("http://127.0.0.1:8080/v1") is True
    assert management._is_safe_endpoint_url("http://api.example.test/v1") is False
    assert management._is_safe_endpoint_url("https://user:pass@api.example.test/v1") is False
    assert management._is_safe_endpoint_url("https://api.example.test/v1?token=secret") is False
    assert management._is_safe_endpoint_url("https://api.example.test:99999/v1") is False


@pytest.mark.asyncio
async def test_custom_endpoint_key_is_deleted_before_using_validated_writer(monkeypatch):
    operations = []

    class Client:
        async def delete_messages(self, _peer, message_ids, *, revoke):
            operations.append(("delete", list(message_ids), revoke))

    async def send(_source, text, *_args, **_kwargs):
        operations.append(("reply", text))

    def save_endpoint(body, *, profile=None):
        operations.append(("save", body, profile))
        return {"ok": True}

    from hermes_cli.web_routers import config_env
    monkeypatch.setattr(config_env, "upsert_custom_endpoint", save_endpoint)

    adapter = SimpleNamespace(
        _client=Client(), _peer_for=lambda _chat: "peer",
        _is_owner_user=lambda _user_id: True, _send_rich_ui=send,
        _pending_custom_endpoint={
            "7": {
                "created": time.monotonic(), "chat_id": "7", "step": "api_key",
                "id": "local-llm", "name": "Local LLM",
                "base_url": "https://llm.example.test/v1", "model": "chat-model",
            },
        },
    )
    source = SimpleNamespace(user_id="7", chat_id="7", chat_type="dm", profile="work")
    secret = "local-endpoint-secret-123"

    consumed = await management.handle_pending_provider_key(
        adapter, source, SimpleNamespace(id=44), secret,
    )

    assert consumed is True
    assert [op[0] for op in operations] == ["delete", "save", "reply"]
    body = operations[1][1]
    assert body.id == "local-llm"
    assert body.name == "Local LLM"
    assert body.base_url == "https://llm.example.test/v1"
    assert body.model == "chat-model"
    assert body.api_key == secret
    assert operations[1][2] == "work"
    assert secret not in operations[2][1]
    assert "7" not in adapter._pending_custom_endpoint


@pytest.mark.asyncio
async def test_custom_endpoint_wizard_deletes_each_step_and_saves_without_activating(monkeypatch):
    operations = []

    class Client:
        async def delete_messages(self, _peer, message_ids, *, revoke):
            operations.append(("delete", list(message_ids), revoke))

    async def send(_source, text, *_args, **_kwargs):
        operations.append(("reply", text))

    def save_endpoint(body, *, profile=None):
        operations.append(("save", body, profile))
        return {"ok": True}

    from hermes_cli.web_routers import config_env
    monkeypatch.setattr(config_env, "upsert_custom_endpoint", save_endpoint)

    adapter = SimpleNamespace(
        _client=Client(), _peer_for=lambda _chat: "peer",
        _is_owner_user=lambda _user_id: True, _send_rich_ui=send,
    )
    source = SimpleNamespace(user_id="8", chat_id="8", chat_type="dm", profile="work")
    event = SimpleNamespace(answer=AsyncMock())
    await management._start_custom_endpoint_wizard(adapter, event, source)

    messages = ["Local Test", "https://llm.example.test/v1", "test-chat-3", "/skip"]
    for message_id, value in enumerate(messages, start=50):
        consumed = await management.handle_pending_provider_key(
            adapter, source, SimpleNamespace(id=message_id), value,
        )
        assert consumed is True

    kinds = [operation[0] for operation in operations]
    assert kinds.count("delete") == 4
    assert kinds.index("delete") < kinds.index("save") < max(
        index for index, kind in enumerate(kinds) if kind == "reply"
    )
    body = next(operation[1] for operation in operations if operation[0] == "save")
    assert body.name == "Local Test"
    assert body.base_url == "https://llm.example.test/v1"
    assert body.model == "test-chat-3"
    assert body.api_key is None
    assert body.make_default is False
    assert all("test-chat-3" not in operation[1] for operation in operations if operation[0] == "reply")


@pytest.mark.asyncio
async def test_models_filter_matches_provider_name_without_truncating_catalog(monkeypatch):
    monkeypatch.setattr(management, "_list_models", AsyncMock(return_value=[{
        "slug": "sample-provider", "name": "Sample Provider", "is_current": False,
        "models": [f"model-{index}" for index in range(1, 18)],
    }]))
    adapter = SimpleNamespace(
        gateway_runner=SimpleNamespace(_check_slash_access=lambda *_args: None),
        _send_rich_ui=AsyncMock(),
    )
    source = SimpleNamespace(user_id="3", chat_id="3", chat_type="dm")

    handled = await management.handle_management_command(adapter, source, "models", "sample-provider")

    assert handled is True
    text, markup = adapter._send_rich_ui.await_args.args[1:3]
    assert "Models matching sample-provider" in text
    assert any("17" in button.text for row in markup.rows for button in row.buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["providers", "miniapp"])
async def test_sensitive_provider_and_dashboard_menus_require_private_explicit_admin(command):
    adapter = SimpleNamespace(
        gateway_runner=SimpleNamespace(_check_slash_access=lambda *_args: None),
        _is_explicitly_allowed=lambda _source: False,
        _send_rich_ui=AsyncMock(),
    )
    source = SimpleNamespace(user_id="4", chat_id="-1004", chat_type="group")

    handled = await management.handle_management_command(adapter, source, command)

    assert handled is True
    text = adapter._send_rich_ui.await_args.args[1]
    assert "private chat" in text
    assert "explicitly allowed" in text


@pytest.mark.asyncio
async def test_rich_ui_bad_request_keeps_native_payload_and_never_uses_bot_api():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter
    from plugins.platforms.telegram.mtproto_rich import inline_markup

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._peer_for = lambda chat_id: f"peer:{chat_id}"
    adapter._client = AsyncMock(side_effect=RuntimeError("MTProto bad request"))
    adapter._send_bot_api_rich = AsyncMock(return_value="99")
    source = SimpleNamespace(chat_id="-1008", thread_id=321)
    text = "# Full menu\n\nKeep this rich copy intact."
    markup = inline_markup([[('Open', 'hm:test:open', 'primary'), ('Close', 'hm:test:close', 'danger')]])

    with pytest.raises(RuntimeError, match="MTProto bad request"):
        await adapter._send_rich_ui(source, text, markup, reply_to=123)
    request = adapter._client.await_args.args[0]
    assert request.rich_message.markdown == text
    assert request.reply_to.reply_to_msg_id == 123
    assert request.reply_to.top_msg_id == 321
    assert request.reply_markup is markup
    adapter._send_bot_api_rich.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_ui_does_not_use_bot_api_fallback_for_other_mtproto_errors(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    adapter = TelethonAdapter(PlatformConfig(extra={}))
    adapter._peer_for = lambda chat_id: f"peer:{chat_id}"
    adapter._client = AsyncMock(side_effect=RuntimeError("network error"))
    adapter._send_bot_api_rich = AsyncMock()

    with pytest.raises(RuntimeError, match="network error"):
        await adapter._send_rich_ui(SimpleNamespace(chat_id="-1009", thread_id=None), "Rich body")

    adapter._send_bot_api_rich.assert_not_awaited()



def test_provider_console_markup_has_back_and_close_navigation():
    markup = management._url_markup(
        "Open provider console", "https://openrouter.ai/settings/keys",
        back_callback="hm:providers:view:token:0",
    )
    buttons = [button for row in markup.rows for button in row.buttons]

    assert any(button.text == "Open provider console" and button.type.url for button in buttons)
    assert any(button.text == "‹ Provider" and button.type.data == b"hm:providers:view:token:0" for button in buttons)
    assert any(button.text == "Close" and button.type.data == b"hm:management:close" for button in buttons)


@pytest.mark.asyncio
async def test_close_callback_deletes_menu_instead_of_replacing_it_with_notice():
    adapter = SimpleNamespace(_delete_rich_ui_message=AsyncMock())
    source = SimpleNamespace(chat_id="77", chat_type="dm")
    event = SimpleNamespace(message_id=31, answer=AsyncMock())

    handled = await management.handle_management_callback(
        adapter, event, source, "hm:management:close",
    )

    assert handled is True
    event.answer.assert_awaited_once_with("Closed.")
    adapter._delete_rich_ui_message.assert_awaited_once_with(event, source)


@pytest.mark.asyncio
async def test_menu_edit_failure_sends_replacement_message():
    adapter = SimpleNamespace(
        _client=AsyncMock(side_effect=RuntimeError("edit unavailable")),
        _peer_for=lambda chat_id: f"peer:{chat_id}",
        _send_rich_ui=AsyncMock(),
    )
    event = SimpleNamespace(message_id=31)
    source = SimpleNamespace(chat_id="77")
    markup = management._url_markup("Open", "https://openrouter.ai/settings/keys")

    await management._edit(adapter, event, source, "# Provider console", markup)

    adapter._send_rich_ui.assert_awaited_once_with(source, "# Provider console", markup, reply_to=None)
