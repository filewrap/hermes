from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from gateway.telegram_personal_settings import load_personal_settings
from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI


@dataclass(frozen=True)
class Source:
    chat_id: str = "123"
    user_id: str = "123"
    user_name: str = "Test User"
    thread_id: str | None = None
    chat_type: str = "dm"
    profile: str | None = None


class CallbackEvent:
    def __init__(self, message_id):
        self.message_id = message_id
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text or "")


class FakeAdapter:
    def __init__(self):
        self._client = object()
        self.edits = []
        self._next_message_id = 100

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.edits.append(request)
        self._next_message_id += 1
        return SimpleNamespace(id=self._next_message_id)

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


def _bind_profile_home(adapter, home):
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: home,
    )


@pytest.mark.asyncio
async def test_settings_callback_token_is_one_shot_and_old_button_cannot_toggle_again(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    old_id = state["id"]

    first = CallbackEvent(55)
    assert await ui.handle_callback(first, source, f"ps:{old_id}:memory-off")
    assert load_personal_settings(tmp_path, source.user_id)["memory_enabled"] is False
    assert state["id"] != old_id
    assert len(adapter.edits) == 1

    duplicate = CallbackEvent(55)
    assert await ui.handle_callback(duplicate, source, f"ps:{old_id}:memory-off")
    assert load_personal_settings(tmp_path, source.user_id)["memory_enabled"] is False
    assert len(adapter.edits) == 1
    assert "expired" in duplicate.answers[-1].lower()


@pytest.mark.asyncio
async def test_settings_cards_and_mcp_callbacks_work_in_each_owner_dm_topic(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    topics = ["77", "88", "99", "1"]  # dany, skills, avisha, and General/main

    for topic in topics:
        await ui.open(Source(thread_id=topic), reply_to=200 + int(topic))

    states = {state["thread_id"]: state for state in ui._states.values()}
    assert set(states) == set(topics)
    assert len({state["message_id"] for state in states.values()}) == len(topics)
    rendered = []

    async def show_mcp(state, page=0, note=""):
        rendered.append((state["thread_id"], page))

    ui._show_mcp = show_mcp
    for topic in topics:
        state = states[topic]
        callback = CallbackEvent(state["message_id"])
        assert await ui.handle_callback(callback, Source(thread_id=topic), f"ps:{state['id']}:mcp")
        assert callback.answers == [""]

    assert rendered == [(topic, 0) for topic in topics]


@pytest.mark.asyncio
async def test_personal_settings_restores_omitted_topic_only_for_exact_card(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    owner_topic = Source(thread_id="77")
    state = ui._new_state(owner_topic, tmp_path, message_id=55)
    state["user_name"] = owner_topic.user_name
    rendered = []
    callback_topics = []

    async def show_mcp(state, page=0, note=""):
        rendered.append(state["thread_id"])

    def push_frame(source, _state):
        callback_topics.append(source.thread_id)
        return False

    ui._show_mcp = show_mcp
    ui._push_control_frame = push_frame
    omitted_topic = CallbackEvent(55)
    assert await ui.handle_callback(omitted_topic, Source(), f"ps:{state['id']}:mcp")
    assert omitted_topic.answers == [""]
    assert rendered == ["77"]
    assert callback_topics == ["77"]

    # Same owner and message ID are insufficient when Telegram provides another topic.
    wrong_topic = CallbackEvent(55)
    assert await ui.handle_callback(wrong_topic, Source(thread_id="88"), f"ps:{state['id']}:mcp")
    assert "expired" in wrong_topic.answers[-1].lower()
    assert rendered == ["77"]


@pytest.mark.asyncio
async def test_settings_callback_fails_closed_if_active_profile_home_changes(tmp_path):
    adapter = FakeAdapter()
    active_home = {"path": tmp_path / "profile-a"}
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: active_home["path"],
    )
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source(thread_id="77", profile="default")
    state = ui._new_state(source, active_home["path"], message_id=55)
    state["user_name"] = source.user_name
    active_home["path"] = tmp_path / "profile-b"

    callback = CallbackEvent(55)
    assert await ui.handle_callback(callback, source, f"ps:{state['id']}:mcp")
    assert "expired" in callback.answers[-1].lower()
    assert state["id"] in ui._states


@pytest.mark.asyncio
async def test_reopening_settings_treats_message_not_modified_as_success(tmp_path):
    from telethon.errors import MessageNotModifiedError

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(
        _resolve_profile_home_for_source=lambda _source: tmp_path,
    )

    async def unchanged(request):
        adapter.edits.append(request)
        raise MessageNotModifiedError(request)

    adapter._mtproto_rich_request = unchanged
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    await ui.open(source)

    assert len(adapter.edits) == 1


def test_custom_mcp_rejects_plaintext_credentials_in_urls_and_arguments():
    from gateway.telegram_personal_settings import validate_personal_mcp_config

    with pytest.raises(ValueError, match="query"):
        validate_personal_mcp_config("attock-search", {"url": "https://example.test/mcp?api_key=plaintext"})
    with pytest.raises(ValueError, match="embedded"):
        validate_personal_mcp_config("attock-search", {"url": "https://user:password@example.test/mcp"})
    with pytest.raises(ValueError, match="command arguments"):
        validate_personal_mcp_config("attock-search", {"command": "node", "args": ["--token", "plaintext"]})
    with pytest.raises(ValueError, match="credential headers"):
        validate_personal_mcp_config("attock-search", {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer plaintext"}})
    with pytest.raises(ValueError, match="header arguments"):
        validate_personal_mcp_config("attock-search", {
            "command": "node", "args": ["--header", "Authorization: Bearer plaintext"],
        })
    validate_personal_mcp_config("attock-search", {
        "command": "node", "args": ["--header", "Authorization: Bearer ${PROFILE_TOKEN}"],
        "env": {"NODE_ENV": "production", "API_TOKEN": "${PROFILE_TOKEN}"},
    })


def test_preferences_export_redacts_legacy_mcp_credentials_and_keeps_safe_env_references():
    import json
    from gateway.telegram_personal_settings import public_personal_settings

    settings = {
        "personal_mcp_servers": {
            "legacy": {
                "url": "https://user:password@example.test/mcp?api_key=endpoint-secret",
                "headers": {"Authorization": "Bearer header-secret", "X-Trace": "possible-secret"},
                "args": ["--token", "argument-secret", "--api-key=inline-secret", "--header",
                         "Authorization: Bearer argument-header-secret"],
                "env": {"API_TOKEN": "env-secret", "SAFE_REF": "${PROFILE_TOKEN}"},
                "nested": {"client_secret": "nested-secret"},
            },
            "safe": {
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${PROFILE_TOKEN}"},
            },
        }
    }

    exported = public_personal_settings(settings)
    encoded = json.dumps(exported, sort_keys=True)
    for secret in ("password", "endpoint-secret", "header-secret", "possible-secret", "argument-secret",
                   "inline-secret", "argument-header-secret", "env-secret", "nested-secret"):
        assert secret not in encoded
    assert "[REDACTED credential-bearing endpoint]" in encoded
    assert "${PROFILE_TOKEN}" in encoded
    assert exported["personal_mcp_servers"]["legacy"]["args"] == [
        "--token", "[REDACTED]", "--api-key=[REDACTED]", "--header", "Authorization: [REDACTED]",
    ]


def _buttons_by_text(markup):
    return {
        button.text: button
        for row in markup.rows for wrapped in row.buttons
        for button in [getattr(wrapped, "button", wrapped)]
    }


def test_personal_settings_and_learning_views_use_bold_tables_and_single_row_toggles():
    from plugins.platforms.telegram.rich_ui import (
        export_confirm_view,
        export_menu_view,
        import_prompt_view,
        learning_overview_view,
        mcp_servers_view,
        personal_settings_view,
        self_learning_view,
    )

    settings = {
        "memory_enabled": False,
        "self_learning_enabled": True,
        "general_instructions": "Be concise",
        "search_instructions": "Prefer official sources",
        "selected_skills": ["research"],
        "selected_mcp_servers": [],
        "updated_at": "2026-10-14T07:56:34+00:00",
    }
    text, markup = personal_settings_view(settings, "Kailash", 7)
    assert text.startswith("<h1>Personal settings</h1>")
    assert text.endswith("<p><i>Page 1 of 1 • total 11</i></p>")
    assert "<th>Setting</th><th>Value</th><th>Actions</th>" in text
    assert "<td><b>Name</b></td><td>Kailash</td>" in text
    assert "<td><b>Memory</b></td><td>Off</td>" in text
    assert "<td><b>Self-learning</b></td><td>On</td>" in text
    assert "<td><b>Turn activity trail</b></td><td>Off</td>" in text
    assert "OFF" not in text and "ON" not in text
    assert '<tg-button type="callback_data" style="danger" data="ps:7:remove-general">Remove</tg-button>' in text
    assert '<tg-button type="callback_data" style="danger" data="ps:7:remove-search">Remove</tg-button>' in text
    assert '<tg-button type="callback_data" style="success" data="ps:7:memory-toggle">Turn on</tg-button>' in text
    assert '<tg-button type="callback_data" style="danger" data="ps:7:learning-toggle">Turn off</tg-button>' in text
    assert '<tg-button type="callback_data" style="success" data="ps:7:activity-trail-toggle">Turn on</tg-button>' in text
    assert text.count('data="ps:7:memory-toggle"') == 1
    assert text.count('data="ps:7:learning-toggle"') == 1
    buttons = _buttons_by_text(markup)
    assert buttons["Close"].style.bg_danger is True
    assert "Learning status" in buttons and "Learning overview" in buttons

    learning_text, _ = self_learning_view(settings, 7)
    overview_text, _ = learning_overview_view(settings, 7)
    mcp_text, _ = mcp_servers_view([("search", True)], 7)
    assert "<h1>Learning settings</h1>" in learning_text and "<td><b>Self-learning</b></td><td>On</td>" in learning_text
    assert "<h1>Learning overview</h1>" in overview_text and "<td><b>Personal memory</b></td><td>Off</td>" in overview_text
    assert "<p><i>Page 1 of 1 • total 4</i></p>" in learning_text
    assert "<p><i>Page 1 of 1 • total 6</i></p>" in overview_text
    assert "| **search** | Selected |" in mcp_text

    export_text, export_markup = export_menu_view(7)
    assert "| **Scope** | **Includes** |" in export_text
    assert "Profile credentials" in export_text and "other users' data" in export_text
    assert _buttons_by_text(export_markup)["Cancel"].style.bg_danger is True
    confirm_text, confirm_markup = export_confirm_view(7, "all")
    assert "# Confirm export" in confirm_text and "other users' data" in confirm_text
    assert _buttons_by_text(confirm_markup)["Cancel"].style.bg_danger is True
    import_text, import_markup = import_prompt_view(7)
    assert "| **File** | **Effect** |" in import_text
    assert _buttons_by_text(import_markup)["Cancel"].style.bg_danger is True


def test_personal_mcp_list_paginates_with_global_indices_and_page_totals():
    from plugins.platforms.telegram.rich_ui import mcp_servers_view

    servers = [(f"mcp-{index}", index == 8) for index in range(10)]
    text, markup = mcp_servers_view(servers, 7, page=1)

    assert "Page 2 of 2 • total 10" in text
    assert "mcp-8" in text and "mcp-0" not in text
    buttons = _buttons_by_text(markup)
    callbacks = [
        getattr(wrapped, "button", wrapped).type.data.decode()
        for row in markup.rows for wrapped in row.buttons
    ]
    assert any(value.endswith(":mcp-toggle:8") for value in callbacks)
    assert any(value.endswith(":mcp-toggle:9") for value in callbacks)
    assert any(value.endswith(":mcp-page:0") for value in callbacks)
    assert buttons["‹ Previous"].style.bg_primary is True
    assert buttons["Back to settings"].style.bg_primary is True


@pytest.mark.asyncio
async def test_single_row_memory_and_learning_toggles_flip_independently(tmp_path):
    adapter = FakeAdapter()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)

    for action, field in (("memory-toggle", "memory_enabled"),
                          ("learning-toggle", "self_learning_enabled")):
        await ui._dispatch_action(state, source, CallbackEvent(55), action)
        assert load_personal_settings(tmp_path, source.user_id)[field] is False
        await ui._dispatch_action(state, source, CallbackEvent(55), action)
        assert load_personal_settings(tmp_path, source.user_id)[field] is True


@pytest.mark.asyncio
async def test_remove_instructions_clears_only_that_field_and_rerenders_home(tmp_path):
    from gateway.telegram_personal_settings import save_personal_settings

    adapter = FakeAdapter()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    save_personal_settings(tmp_path, source.user_id, {
        "general_instructions": "Be concise",
        "search_instructions": "Prefer official sources",
    })
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    await ui._dispatch_action(state, source, CallbackEvent(55), "remove-general")
    settings = load_personal_settings(tmp_path, source.user_id)
    assert settings["general_instructions"] == ""
    assert settings["search_instructions"] == "Prefer official sources"
    rendered = adapter.edits[-1].message
    assert "General instructions removed." in rendered
    assert 'data="ps:' in rendered and "remove-general" not in rendered
    assert 'data="ps:' in rendered and "remove-search" in rendered

    await ui._dispatch_action(state, source, CallbackEvent(55), "remove-search")
    settings = load_personal_settings(tmp_path, source.user_id)
    assert settings["search_instructions"] == ""
    assert "Search instructions removed." in adapter.edits[-1].message


@pytest.mark.asyncio
async def test_activity_trail_toggle_persists_per_user_without_touching_profile_config(tmp_path):
    from gateway.run import _load_gateway_config
    from gateway.telegram_personal_settings import save_personal_settings

    profile_config = tmp_path / "config.yaml"
    profile_config.write_text("display:\n  platforms:\n    telegram:\n      tool_progress: off\n", encoding="utf-8")
    before_config = profile_config.read_text(encoding="utf-8")
    owner = Source()
    other = Source(user_id="456", chat_id="456")
    save_personal_settings(tmp_path, other.user_id, {"memory_enabled": False})
    ui = TelegramPersonalSettingsUI(FakeAdapter())
    state = ui._new_state(owner, tmp_path, message_id=55)
    state["user_name"] = owner.user_name
    denied = CallbackEvent(55)
    assert await ui.handle_callback(denied, other, f"ps:{state['id']}:activity-trail-toggle")
    assert load_personal_settings(tmp_path, owner.user_id)["activity_trail_enabled"] is False
    assert load_personal_settings(tmp_path, other.user_id)["activity_trail_enabled"] is False

    await ui._dispatch_action(state, owner, CallbackEvent(55), "activity-trail-toggle")
    assert load_personal_settings(tmp_path, owner.user_id)["activity_trail_enabled"] is True
    assert load_personal_settings(tmp_path, other.user_id)["activity_trail_enabled"] is False
    assert load_personal_settings(tmp_path, other.user_id)["memory_enabled"] is False
    assert profile_config.read_text(encoding="utf-8") == before_config
    assert _load_gateway_config(profile_config).get("display", {}).get("platforms", {}).get("telegram", {}).get("tool_progress") is False

    await ui._render_home(state)
    assert "<td><b>Turn activity trail</b></td><td>On</td>" in ui.adapter.edits[-1].message
    callbacks = [
        getattr(wrapped, "button", wrapped).type.data.decode()
        for row in ui.adapter.edits[-1].reply_markup.rows for wrapped in row.buttons
    ]
    import re
    callbacks.extend(re.findall(r'data="([^"]+)"', ui.adapter.edits[-1].message))
    assert any(payload.endswith(":activity-trail-toggle") for payload in callbacks)


@pytest.mark.asyncio
async def test_attachment_settings_are_private_and_expose_manual_download_choices(tmp_path):
    from gateway.telegram_personal_settings import (
        ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES,
        ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES,
    )

    adapter = FakeAdapter()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    await ui._dispatch_action(state, source, CallbackEvent(55), "attachments")
    assert "File download settings" in adapter.edits[-1].message
    assert "Effective automatic-download size" in adapter.edits[-1].message
    assert "Effective explicit-download limit" in adapter.edits[-1].message
    callback_payloads = [
        getattr(wrapped, "button", wrapped).type.data.decode()
        for row in adapter.edits[-1].reply_markup.rows for wrapped in row.buttons
    ]
    assert any(payload.endswith("download-mode-explicit") for payload in callback_payloads)
    assert any(payload.endswith("download-threshold:200") for payload in callback_payloads)
    assert any(payload.endswith("explicit-limit:4096") for payload in callback_payloads)

    await ui._dispatch_action(state, source, CallbackEvent(55), "download-mode-explicit")
    await ui._dispatch_action(state, source, CallbackEvent(55), "download-threshold:100")
    await ui._dispatch_action(state, source, CallbackEvent(55), "explicit-limit:512")
    await ui._dispatch_action(state, source, CallbackEvent(55), "text-reactions-toggle")
    saved = load_personal_settings(tmp_path, source.user_id)
    assert saved["attachment_download_mode"] == "explicit"
    assert saved["attachment_auto_download_threshold_bytes"] == ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES[1]
    assert saved["attachment_explicit_download_limit_bytes"] == ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES[1]
    assert saved["reactions_enabled"] is True
    assert saved["text_reactions_enabled"] is True

    await ui._render_home(state)
    home_callbacks = [
        getattr(wrapped, "button", wrapped).type.data.decode()
        for row in adapter.edits[-1].reply_markup.rows for wrapped in row.buttons
    ]
    assert any(payload.endswith(":attachments") for payload in home_callbacks)



def test_personal_instructions_accept_more_than_2000_characters(tmp_path):
    from gateway.telegram_personal_settings import save_personal_settings

    text = "private guidance " * 220
    saved = save_personal_settings(tmp_path, "123", {"general_instructions": text})
    assert len(saved["general_instructions"]) == len(text.strip())
    assert len(load_personal_settings(tmp_path, "123")["general_instructions"]) == len(text.strip())


@pytest.mark.asyncio
async def test_personal_default_model_picker_sends_native_keyboard(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from hermes_cli import model_switch_providers
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.telethon_adapter import TelethonAdapter

    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda **_kwargs: {
        "model": {"provider": "openrouter", "default": "z-ai/glm-5.2"},
        "providers": {}, "custom_providers": [], "model_catalog": {},
    })
    monkeypatch.setattr(model_switch_providers, "list_picker_providers", lambda **_kwargs: [
        {"slug": "nous", "name": "Nous", "models": ["minningno"]},
    ])
    adapter = TelethonAdapter(PlatformConfig())
    calls = []

    class Client:
        async def __call__(self, request):
            calls.append(request)
            return SimpleNamespace(id=101)

    adapter._client = Client()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)

    await ui._open_personal_model_picker(source, state)

    assert len(calls) == 1
    assert type(calls[0]).__name__ == "SendMessageRequest"
    assert calls[0].reply_markup.rows
    assert load_personal_settings(tmp_path, source.user_id)["default_model"] == ""

def test_personal_model_defaults_overlay_without_mutating_the_shared_profile():
    from gateway.telegram_personal_settings import apply_personal_settings_to_user_config

    shared = {"model": {"provider": "openrouter", "default": "z-ai/glm-5.2"}}
    personal = apply_personal_settings_to_user_config(shared, {
        "default_provider": "nous", "default_model": "minningno",
    })
    assert personal["model"] == {"provider": "nous", "default": "minningno"}
    assert shared["model"] == {"provider": "openrouter", "default": "z-ai/glm-5.2"}


def test_personal_settings_home_has_complete_table_and_footer_last(tmp_path):
    from gateway.telegram_personal_settings import save_personal_settings

    adapter = FakeAdapter()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    save_personal_settings(tmp_path, source.user_id, {
        "general_instructions": "Use concise answers",
        "search_instructions": "Prefer primary sources",
        "selected_skills": ["research"],
        "selected_mcp_servers": ["search"],
        "text_reactions_enabled": True,
        "attachment_download_mode": "explicit",
    })

    text, markup = ui._home_view(state)

    assert text.startswith("<h1>Personal settings</h1>")
    assert "These preferences apply only to your Telegram account in this profile." in text
    assert "<th>Setting</th><th>Value</th><th>Actions</th>" in text
    assert "<td><b>General instructions</b></td><td>Use concise answers</td>" in text
    assert "<td><b>Search instructions</b></td><td>Prefer primary sources</td>" in text
    assert "<td><b>Private skill library</b></td><td>0 skills</td>" in text
    assert "<td><b>Auto-load skills</b></td><td>1 selected (profile defaults + personal additions)</td>" in text
    assert "<td><b>MCP servers</b></td><td>Profile defaults</td>" in text
    assert "<td><b>Optional text reactions (heuristic)</b></td><td>On</td>" in text
    assert "<td><b>Turn activity trail</b></td><td>Off</td>" in text
    assert "<td><b>File downloads</b></td><td>Explicit only;" in text
    assert 'data="ps:' in text and "remove-general" in text and "remove-search" in text
    assert text.endswith("<p><i>Page 1 of 1 • total 14</i></p>")
    labels = [getattr(button, "button", button).text
              for row in markup.rows for button in row.buttons]
    assert labels[-1] == "Close"


def test_personal_settings_home_name_is_profile_button_and_library_has_view(tmp_path):
    """The Name row carries a rich inline user (profile) button, and the
    Private skill library row carries a View button into the skills UI."""
    adapter = FakeAdapter()
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name

    text, _markup = ui._home_view(state)

    assert '<tg-button type="url" url="tg://user?id=123">Test User</tg-button>' in text
    assert '<tg-button type="callback_data" style="primary" data="ps:' in text
    assert text.count(f'data="ps:{state["id"]}:skills"') == 2
    assert ">View</tg-button>" in text
    assert ">Manage</tg-button>" in text


@pytest.mark.asyncio
async def test_file_download_card_is_complete_and_close_deletes_only_bound_messages(tmp_path):
    from unittest.mock import AsyncMock

    adapter = FakeAdapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["invocation_message_id"] = "44"

    await ui._dispatch_action(state, source, CallbackEvent(55), "attachments")
    request = adapter.edits[-1]
    text = request.message
    assert text.startswith("<h1>File download settings</h1>")
    assert "These file download preferences apply only to your Telegram account in this profile." in text
    assert "<td><b>Mode</b></td><td>Automatic under threshold</td>" in text
    assert "<td><b>Effective automatic-download size</b></td>" in text
    assert "<td><b>Effective explicit-download limit</b></td>" in text
    assert "operator-configured inbound media cap" in text
    assert "outgoing uploads remain subject to bot/provider limits" in text
    assert text.endswith("<p><i>Page 1 of 1 • total 8</i></p>")
    labels = [getattr(button, "button", button).text
              for row in request.reply_markup.rows for button in row.buttons]
    assert labels[-1] == "Close"

    callback = f"ps:{state['id']}:close"
    wrong_user = CallbackEvent(55)
    await ui.handle_callback(wrong_user, Source(user_id="456"), callback)
    wrong_chat = CallbackEvent(55)
    await ui.handle_callback(wrong_chat, Source(chat_id="999"), callback)
    adapter._client.delete_messages.assert_not_awaited()

    assert await ui.handle_callback(CallbackEvent(55), source, callback)
    adapter._client.delete_messages.assert_awaited_once_with("123", [55, 44])


@pytest.mark.asyncio
async def test_help_entry_never_inherits_an_unrelated_old_settings_invocation(tmp_path):
    from unittest.mock import AsyncMock

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _: tmp_path)
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    await ui.open(source, message_id=55, invocation_message_id=44)
    await ui.open(source, message_id=66, help_origin=("home", 0))
    state = next(iter(ui._states.values()))
    assert state["invocation_message_id"] is None
    await ui.handle_callback(CallbackEvent(66), source, f"ps:{state['id']}:close")
    adapter._client.delete_messages.assert_awaited_once_with("123", [66])


@pytest.mark.asyncio
async def test_failed_settings_close_refreshes_a_bound_retryable_card(tmp_path):
    from unittest.mock import AsyncMock

    adapter = FakeAdapter()
    adapter.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _: tmp_path)
    delete = AsyncMock(side_effect=RuntimeError("temporary transport failure"))
    adapter._client = SimpleNamespace(delete_messages=delete)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    await ui.open(source, message_id=55, invocation_message_id=44)
    state = next(iter(ui._states.values()))
    await ui.handle_callback(CallbackEvent(55), source, f"ps:{state['id']}:close")
    assert state["id"] in ui._states
    delete.side_effect = None
    await ui.handle_callback(CallbackEvent(55), source, f"ps:{state['id']}:close")
    assert delete.await_args.args == ("123", [55, 44])
    assert not ui._states


@pytest.mark.asyncio
async def test_private_mcp_actions_save_only_to_profile_user_store(tmp_path):
    from gateway.telegram_personal_settings import (
        apply_personal_settings_to_user_config, load_personal_settings, save_personal_settings,
    )

    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    shared_path = tmp_path / "config.yaml"
    shared_path.write_text(
        "mcp_servers:\n  profile-search:\n    url: https://profile.example.test/mcp\n    enabled: true\n",
        encoding="utf-8",
    )
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    state["mcp_names"] = ["profile-search"]

    async def show_mcp(_state, note="", *, page=None):
        return None

    ui._show_mcp = show_mcp
    await ui._toggle_mcp(state, "0")
    assert load_personal_settings(tmp_path, source.user_id)["selected_mcp_servers"] == ["profile-search"]

    state["pending_mcp"] = {
        "name": "custom-server",
        "config": {"url": "https://example.test/mcp"},
    }
    await ui._save_mcp(state, source)
    settings = load_personal_settings(tmp_path, source.user_id)
    assert settings["mcp_mode"] == "selected"
    assert settings["selected_mcp_servers"] == ["custom-server", "profile-search"]
    assert settings["personal_mcp_servers"]["custom-server"]["url"] == "https://example.test/mcp"
    assert shared_path.read_text(encoding="utf-8").startswith("mcp_servers:")

    shared = {"mcp_servers": {"profile-search": {"url": "https://profile.example.test/mcp"}}}
    effective = apply_personal_settings_to_user_config(shared, settings)
    assert set(effective["mcp_servers"]) == {"profile-search", "custom-server"}
    assert set(shared["mcp_servers"]) == {"profile-search"}

    # The generic settings writer keeps the guard; only the dedicated MCP path can switch mode.
    with pytest.raises(ValueError, match="cannot change mcp_mode"):
        save_personal_settings(tmp_path, source.user_id, {"mcp_mode": "selected"})


def test_private_mcp_settings_are_isolated_by_profile_and_telegram_user(tmp_path):
    from gateway.telegram_personal_settings import load_personal_settings, save_personal_mcp_settings

    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    save_personal_mcp_settings(home_a, "123", {
        "mcp_mode": "selected", "selected_mcp_servers": ["private-a"],
        "personal_mcp_servers": {"private-a": {"url": "https://a.example.test/mcp"}},
    })
    save_personal_mcp_settings(home_a, "456", {
        "mcp_mode": "selected", "selected_mcp_servers": ["private-b"],
        "personal_mcp_servers": {"private-b": {"url": "https://b.example.test/mcp"}},
    })
    save_personal_mcp_settings(home_b, "123", {
        "mcp_mode": "selected", "selected_mcp_servers": ["private-c"],
        "personal_mcp_servers": {"private-c": {"url": "https://c.example.test/mcp"}},
    })

    assert load_personal_settings(home_a, "123")["selected_mcp_servers"] == ["private-a"]
    assert load_personal_settings(home_a, "456")["selected_mcp_servers"] == ["private-b"]
    assert load_personal_settings(home_b, "123")["selected_mcp_servers"] == ["private-c"]


def test_private_mcp_writer_reuses_secret_validation_and_accepts_no_shared_fields(tmp_path):
    from gateway.telegram_personal_settings import save_personal_mcp_settings

    with pytest.raises(ValueError, match="credential headers"):
        save_personal_mcp_settings(tmp_path, "123", {
            "personal_mcp_servers": {
                "bad-server": {
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer plaintext-secret"},
                },
            },
        })
    with pytest.raises(ValueError, match="Unsupported private MCP setting"):
        save_personal_mcp_settings(tmp_path, "123", {"default_provider": "other"})
    assert not (tmp_path / "config.yaml").exists()


def test_private_mcp_writer_uses_profile_scoped_mongo_principal(tmp_path, monkeypatch):
    import copy
    from pathlib import Path
    from agent import storage_mode
    from gateway import mongo_multiuser
    from gateway.telegram_personal_settings import load_personal_settings, save_personal_mcp_settings

    class Store:
        def __init__(self):
            self.records = {}

        def get(self, actor, collection, key):
            return copy.deepcopy(self.records.get((actor, collection, key)))

        def put(self, actor, collection, key, value):
            self.records[(actor, collection, key)] = copy.deepcopy(value)

    stores = {}

    def store_for_home(home):
        return stores.setdefault(str(Path(home).resolve()), Store())

    monkeypatch.setattr(storage_mode, "mongo_primary_enabled", lambda _home: True)
    monkeypatch.setattr(mongo_multiuser, "store_for_home", store_for_home)
    monkeypatch.setattr(mongo_multiuser, "principal", lambda user_id: f"telegram:{user_id}")

    profile_a, profile_b = tmp_path / "a", tmp_path / "b"
    save_personal_mcp_settings(profile_a, "123", {
        "mcp_mode": "selected", "selected_mcp_servers": ["private-a"],
    })
    save_personal_mcp_settings(profile_b, "123", {
        "mcp_mode": "selected", "selected_mcp_servers": ["private-b"],
    })

    assert load_personal_settings(profile_a, "123")["selected_mcp_servers"] == ["private-a"]
    assert load_personal_settings(profile_b, "123")["selected_mcp_servers"] == ["private-b"]
    assert stores[str(profile_a.resolve())].records["telegram:123", "settings", "personal"]["selected_mcp_servers"] == ["private-a"]
    assert stores[str(profile_b.resolve())].records["telegram:123", "settings", "personal"]["selected_mcp_servers"] == ["private-b"]


@pytest.mark.asyncio
async def test_open_custom_mcp_starts_existing_private_entry_flow(tmp_path):
    from unittest.mock import AsyncMock

    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    ui._start_mcp_add = AsyncMock()
    source = Source()

    assert await ui.open_custom_mcp(source, message_id=55)
    state = next(iter(ui._states.values()))
    ui._start_mcp_add.assert_awaited_once_with(state, source)
    assert state["message_id"] == "55"


@pytest.mark.asyncio
async def test_personal_settings_home_has_divider_above_italic_page_footer(tmp_path):
    adapter = FakeAdapter()
    _bind_profile_home(adapter, tmp_path)
    ui = TelegramPersonalSettingsUI(adapter)
    source = Source()
    state = ui._new_state(source, tmp_path, message_id=55)
    state["user_name"] = source.user_name
    text, _markup = ui._home_view(state)
    assert "<hr" in text
    assert text.index("<hr") < text.index("<p><i>Page 1 of 1")
    assert "<p><i>Page 1 of 1 • total" in text
