from types import SimpleNamespace

import pytest

from gateway.model_ui import settings_payload
from gateway.session import SessionSource
from gateway.config import Platform
from plugins.platforms.telegram.model_settings_ui import TelegramModelSettingsUI
from datetime import UTC


class _Runner:
    def __init__(self):
        self.calls = []
        self.ui = None
        self.settings = {"temperature": 0.5, "max_tokens": 2048}

    async def _handle_model_command(self, event):
        self.calls.append(event)
        if self.ui and not (event.get_command_args() or "").strip():
            dashboard = {"current_model": "m-x", "current_provider": "openrouter",
                         "provider_name": "OpenRouter", "provider_configured": True,
                         "model_catalog_count": 3, "reasoning_supported": False,
                         "session_settings": dict(self.settings), "reasoning_effort": "provider default"}
            await self.ui.open_dashboard(event.source, dashboard, [], "m-x", "openrouter",
                                         "session-A", "/tmp/profile-a")

    async def _handle_msettings_command(self, event):
        self.calls.append(event)
        if self.ui:
            parts = (event.get_command_args() or "").split()
            page = "sampling"
            if len(parts) == 2 and parts[0] == "page":
                page = {"1": "sampling", "2": "output", "3": "capabilities"}.get(parts[1], parts[1])
            elif len(parts) == 3 and parts[0] == "set":
                self.settings[parts[1]] = int(parts[2]) if parts[1] == "max_tokens" else float(parts[2])
                page = "output" if parts[1] == "max_tokens" else "sampling"
            elif len(parts) == 2 and parts[0] == "reset":
                self.settings.pop(parts[1], None)
            payload = settings_payload(settings=dict(self.settings), page=page,
                                       reasoning_supported=False)
            payload.update(current_model="m-x", current_provider="openrouter",
                           reasoning_supported=False, reasoning_options=[])
            await self.ui.open_settings(event.source, payload, "session-A")


class _Adapter:
    def __init__(self):
        self._client = object()
        self.gateway_runner = _Runner()
        self.requests = []
        self.owner_ids = {"alpha": "42"}

    def _provider_ui(self):
        if not hasattr(self, "_telegram_provider_ui"):
            from plugins.platforms.telegram.provider_management_ui import TelegramProviderManagementUI
            self._telegram_provider_ui = TelegramProviderManagementUI(self)
        return self._telegram_provider_ui

    def _is_owner_user(self, user_id, source=None):
        profile = str(getattr(source, "profile", "") or "")
        expected_owner = self.owner_ids.get(profile)
        return expected_owner is not None and str(user_id or "") == expected_owner

    def _peer_for(self, chat_id):
        return ("peer", str(chat_id))

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=901 + len(self.requests))

    @staticmethod
    def _sent_message_id(response):
        return str(response.id)


class _Callback:
    _next_id = 0

    def __init__(self, message_id, query_id=None):
        type(self)._next_id += 1
        self.query_id = query_id if query_id is not None else type(self)._next_id
        self.message_id = message_id
        self.message = SimpleNamespace(id=message_id)
        self.answers = []

    async def answer(self, text="", alert=False):
        self.answers.append((text, alert))


def _source(*, user="42", chat="42", profile="alpha", thread=None, chat_type="dm"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat, user_id=user,
                         chat_type=chat_type, profile=profile, thread_id=thread)


def _walk_tl(node):
    from telethon import types

    if isinstance(node, (list, tuple)):
        for child in node:
            yield from _walk_tl(child)
    elif isinstance(node, types.TLObject):
        yield node
        for child in vars(node).values():
            if isinstance(child, (types.TLObject, list, tuple)):
                yield from _walk_tl(child)


@pytest.mark.asyncio
async def test_dashboard_uses_rich_message_and_dispatches_existing_model_handler():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    dashboard = {"current_model": "m-x", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 3, "reasoning_supported": False,
                 "session_settings": {}, "reasoning_effort": "provider default"}
    assert await ui.open_dashboard(source, dashboard, [{"slug": "openrouter"}], "m-x",
                                   "openrouter", "session-A", "/tmp/profile-a", 77)
    request = adapter.requests[0]
    assert request.rich_message is not None
    assert request.reply_to.reply_to_msg_id == 77
    from telethon import types
    assert isinstance(request.rich_message, types.InputRichMessage)
    rendered = _rich_plain_text(request.rich_message.blocks)
    assert "Model dashboard" in rendered
    assert "OpenRouter" in rendered and "configured" in rendered
    assert "Currently provider default" in rendered
    assert "3 models" in rendered
    assert "OpenRouter" in request.message and "configured" in request.message
    pills = [node for node in _walk_tl(request.rich_message)
             if isinstance(node, types.TextButton)
             and isinstance(node.type, types.InlineButtonTypeCallback)]
    assert {node.type.data.decode("utf-8").rsplit(":", 1)[-1] for node in pills} >= {
        "provider", "configured", "model", "catalog", "reasoning", "support", "settings",
    }
    assert len(request.rich_message._bytes()) > 0
    state = next(iter(ui._states.values()))
    assert state["message_id"] == "902"
    assert state["identity"][0] == "/tmp/profile-a"
    assert state["identity"][-1] == "session-A"
    _text, markup = ui._dashboard_view(state)
    assert [button.text for button in markup.rows[0].buttons] == ["Providers", "List/change"]
    models_callback = markup.rows[0].buttons[1].type.data.decode("utf-8")
    provider_state = next(iter(adapter._provider_ui()._states.values()))
    assert models_callback == f"pv:{provider_state['id']}:models:0"
    assert not models_callback.startswith(f"ms:{state['id']}:models")
    assert [button.text for button in markup.rows[1].buttons] == ["Model settings"]
    assert [button.text for button in markup.rows[2].buttons] == ["Close"]

    callback = _Callback(902)
    assert await ui.handle_callback(callback, source, f"ms:{state['id']}:providers")
    assert adapter.gateway_runner.calls[0].text == "/model providers"
    assert adapter.gateway_runner.calls[0].source is source
    assert adapter.gateway_runner.calls[0].is_command()


@pytest.mark.asyncio
async def test_callbacks_are_bound_to_profile_user_thread_and_message():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source(thread="17")
    await ui.open_dashboard(source, {"current_model": "m", "current_provider": "p"}, [], "m", "p",
                            "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))

    wrong_source = _source(user="99", thread="17")
    callback = _Callback(state["message_id"])
    assert await ui.handle_callback(callback, wrong_source, f"ms:{state['id']}:providers")
    assert callback.answers[-1][1] is True
    assert not adapter.gateway_runner.calls

    wrong_thread = _source(thread="18")
    callback = _Callback(state["message_id"])
    await ui.handle_callback(callback, wrong_thread, f"ms:{state['id']}:providers")
    assert callback.answers[-1][1] is True

    callback = _Callback("different-message")
    await ui.handle_callback(callback, source, f"ms:{state['id']}:providers")
    assert callback.answers[-1][1] is True
    assert not adapter.gateway_runner.calls


@pytest.mark.asyncio
async def test_dashboard_rich_value_callback_restores_omitted_topic_for_bound_card():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    adapter.gateway_runner._normalize_source_for_session_key = lambda source: source
    adapter.gateway_runner._session_key_for_source = lambda source: str(source.thread_id or "")
    source = _source(thread="17")
    await ui.open_dashboard(
        source, {"current_model": "topic-model", "provider_name": "OpenRouter"}, [],
        "topic-model", "openrouter", "17", "/tmp/profile-a",
    )
    state = next(iter(ui._states.values()))
    callback_source = _source(thread=None)
    callback = _Callback(state["message_id"])

    assert await ui.handle_callback(callback, callback_source, f"ms:{state['id']}:value:model")
    assert callback.answers == [("Current model: topic-model", True)]


@pytest.mark.asyncio
async def test_settings_numeric_entry_is_direct_and_provider_capabilities_are_not_guessed():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    adapter.gateway_runner.ui = ui
    source = _source()
    payload = settings_payload(settings={"temperature": 0.5}, page="sampling",
                               reasoning_supported=None)
    payload["current_model"] = "m"
    assert await ui.open_settings(source, payload, "session-A")
    request = adapter.requests[0]
    assert request.rich_message is not None
    # Values are taken from the live page payload, not a missing global settings map.
    state = next(iter(ui._states.values()))
    text, _markup = ui._settings_view(state)
    assert "0.5" in text
    assert f"ms:{state['id']}:input:temperature" in text

    callback = _Callback(state["message_id"])
    await ui.handle_callback(callback, source, f"ms:{state['id']}:input:temperature")
    assert state["identity"] in ui._pending
    assert not adapter.gateway_runner.calls

    assert await ui.handle_message(SimpleNamespace(), source, "0.85")
    assert adapter.gateway_runner.calls[-1].text == "/msettings set temperature 0.85"
    assert adapter.gateway_runner.calls[-1].source is source
    assert not hasattr(adapter.gateway_runner, "handle_message")


@pytest.mark.asyncio
async def test_invalid_numeric_input_stays_in_direct_settings_flow():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    adapter.gateway_runner.ui = ui
    source = _source()
    payload = settings_payload(settings={}, page="output")
    await ui.open_settings(source, payload, "session-A")
    state = next(iter(ui._states.values()))
    await ui.handle_callback(_Callback(state["message_id"]), source,
                             f"ms:{state['id']}:input:max_tokens")
    assert await ui.handle_message(SimpleNamespace(), source, "2.2")
    assert not adapter.gateway_runner.calls
    assert state["identity"] in ui._pending
    assert "integer" in adapter.requests[-1].rich_message.html.lower() or "number" in adapter.requests[-1].rich_message.html.lower()


@pytest.mark.asyncio
async def test_pending_value_does_not_swallow_new_session_command_or_cross_boundary():
    adapter = _Adapter()
    active = [SimpleNamespace(conversation=object())]
    adapter.gateway_runner._peek_session_state = lambda _key: active[0]
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_settings(source, settings_payload(settings={}), "session-A")
    state = next(iter(ui._states.values()))
    await ui.handle_callback(_Callback(state["message_id"]), source, f"ms:{state['id']}:input:temperature")
    assert await ui.handle_message(SimpleNamespace(), source, "/new") is False
    assert not ui._pending
    await ui.handle_callback(_Callback(state["message_id"]), source, f"ms:{state['id']}:input:temperature")
    active[0] = SimpleNamespace(conversation=object())
    assert await ui.handle_message(SimpleNamespace(), source, "0.9") is False
    assert not adapter.gateway_runner.calls


@pytest.mark.asyncio
async def test_settings_page_value_and_back_callbacks_edit_the_original_message():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    adapter.gateway_runner.ui = ui
    source = _source()
    dashboard = {"current_model": "m-x", "current_provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_configured": True,
                 "model_catalog_count": 3, "session_settings": {}}
    await ui.open_dashboard(source, dashboard, [], "m-x", "openrouter", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    original_id = state["message_id"]

    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:settings")
    assert state["view"] == "settings"
    assert state["message_id"] == original_id
    assert adapter.requests[-1].id == int(original_id)
    assert "Page 1 of 3" in adapter.requests[-1].rich_message.html

    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:page:output")
    assert state["page"] == "output"
    assert state["message_id"] == original_id
    assert adapter.requests[-1].id == int(original_id)
    assert "Page 2 of 3" in adapter.requests[-1].rich_message.html

    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:input:max_tokens")
    assert state["message_id"] == original_id
    assert adapter.requests[-1].id == int(original_id)
    assert "Send a number" in adapter.requests[-1].rich_message.html
    assert await ui.handle_message(SimpleNamespace(), source, "4096")
    assert adapter.gateway_runner.calls[-1].text == "/msettings set max_tokens 4096"
    assert adapter.requests[-1].id == int(original_id)
    assert "4096" in adapter.requests[-1].rich_message.html

    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:back")
    assert state["view"] == "settings"
    assert state["page"] == "sampling"
    assert state["message_id"] == original_id
    assert adapter.requests[-1].id == int(original_id)
    assert "Page 1 of 3" in adapter.requests[-1].rich_message.html

    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:back")
    assert state["view"] == "dashboard"
    assert state["message_id"] == original_id
    assert adapter.requests[-1].id == int(original_id)
    assert "Page 1 of 1 • total 1" in _rich_plain_text(adapter.requests[-1].rich_message.blocks)
    assert len(ui._states) == 1


@pytest.mark.asyncio
async def test_true_owner_group_settings_callback_alerts_to_dm_without_mutation():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source(chat="-1007", chat_type="group")
    await ui.open_dashboard(source, {"current_model": "m", "current_provider": "p"}, [],
                           "m", "p", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    request_count = len(adapter.requests)
    callback = _Callback(state["message_id"])

    assert await ui.handle_callback(callback, source, f"ms:{state['id']}:settings")
    assert callback.answers[-1][1] is True
    assert "Telegram DM" in callback.answers[-1][0]
    assert len(adapter.requests) == request_count
    assert not adapter.gateway_runner.calls
    assert state["view"] == "dashboard"


@pytest.mark.asyncio
async def test_non_owner_cannot_open_settings_even_when_authorized_for_model_dashboard():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    non_owner = _source(user="99", chat="99")
    await ui.open_dashboard(non_owner, {"current_model": "m", "current_provider": "p"}, [],
                           "m", "p", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    callback = _Callback(state["message_id"])

    assert await ui.handle_callback(callback, non_owner, f"ms:{state['id']}:settings")
    assert callback.answers[-1][1] is True
    assert "configured profile owner" in callback.answers[-1][0]
    assert state["view"] == "dashboard"
    assert not adapter.gateway_runner.calls


@pytest.mark.asyncio
async def test_duplicate_callback_query_is_suppressed_without_repeating_dispatch():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_dashboard(source, {"current_model": "m", "current_provider": "p"}, [],
                           "m", "p", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    data = f"ms:{state['id']}:providers"
    first = _Callback(state["message_id"], query_id=555)
    second = _Callback(state["message_id"], query_id=555)

    await ui.handle_callback(first, source, data)
    await ui.handle_callback(second, source, data)
    assert len(adapter.gateway_runner.calls) == 1
    assert second.answers


@pytest.mark.asyncio
async def test_rich_layout_footer_and_parameter_prompt_contract():
    from xml.etree import ElementTree

    def parse_html(html):
        return ElementTree.fromstring("<body>" + html.replace("<br>", "<br/>") + "</body>")

    def text(element):
        return "".join(element.itertext())
    from gateway.model_ui import MODEL_SETTINGS, SETTINGS_PAGES

    adapter = _Adapter()
    adapter.gateway_runner.ui = ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_dashboard(source, {"current_model": "m", "current_provider": "p"}, [],
                            "m", "p", "session-A", "/tmp/profile-a")
    state = next(iter(ui._states.values()))
    dashboard_rich, _ = ui._dashboard_view(state)
    from telethon import types
    assert isinstance(dashboard_rich, types.InputRichMessage)
    dashboard_text = _rich_plain_text(dashboard_rich.blocks)
    assert "Page 1 of 1 • total 1" in dashboard_text
    assert "model-specific support is unconfirmed" in dashboard_text
    for page_index, (page, _label) in enumerate(SETTINGS_PAGES, 1):
        await ui.handle_callback(_Callback(state["message_id"]), source,
                                 f"ms:{state['id']}:settings" if page_index == 1 else f"ms:{state['id']}:page:{page}")
        html = ui._settings_view(state)[0]
        soup = parse_html(html)
        total = sum(f.page == page for f in MODEL_SETTINGS) if page != "capabilities" else 1
        footer = f"Page {page_index} of {len(SETTINGS_PAGES)} • total {total}"
        assert text(soup[-1]) == footer
        assert text(soup).count("Page ") == 1
        if page != "capabilities":
            assert soup.find("blockquote") is None
        for field in (f for f in MODEL_SETTINGS if f.page == page):
            await ui.handle_callback(_Callback(state["message_id"]), source,
                                     f"ms:{state['id']}:input:{field.key}")
            request = adapter.requests[-1]
            assert request.id == int(state["message_id"])
            prompt = parse_html(request.rich_message.html)
            quote = prompt.findall("blockquote")[-1]
            assert text(quote.find("b")) == f"Enter {field.label}"
            assert "UI validation, not guaranteed provider/model limits" in text(quote)
            assert "10 minutes" in text(quote)
            assert quote in list(prompt)  # Not attached to a paragraph or another quote.
            assert text(prompt[-1]) == footer
            purpose = {
                "temperature": "randomness", "top_p": "combined probability",
                "frequency_penalty": "how often", "presence_penalty": "at all",
                "max_tokens": "output tokens",
            }[field.key]
            assert purpose in text(quote)
            assert purpose in text(soup)
            await ui.handle_message(SimpleNamespace(), source, "invalid")
            feedback = parse_html(adapter.requests[-1].rich_message.html)
            assert text(feedback[-1]) == footer
            old_pending = ui._pending[state["identity"]]
            await ui.handle_callback(_Callback(state["message_id"]), source,
                                     f"ms:{state['id']}:input:{field.key}")
            replacement = ui._pending[state["identity"]]
            assert replacement is not old_pending
            assert old_pending["timer"].cancelled()
            ui._expire_pending(state["identity"], old_pending)
            assert ui._pending[state["identity"]] is replacement
            ui._clear_pending(state["identity"])


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "accepted", "invalid", "unrelated", "wrong_reply", "old_message", "other_user",
    "other_chat", "other_thread", "other_profile", "group", "owner_changed",
    "expired", "timer_expired", "cancel", "navigate", "replaced", "stale_parameter",
    "new_conversation", "route_changed", "session_changed", "expires_during_resolution",
    "rejected", "missing_handler", "unconfirmed_same_value", "mismatched_refresh", "delete_failure",
])
async def test_pending_input_is_scoped_expiring_and_deletes_only_accepted_reply(scenario, monkeypatch):
    import asyncio
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from plugins.platforms.telegram import model_settings_ui as module

    adapter = _Adapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    adapter.gateway_runner.ui = ui = TelegramModelSettingsUI(adapter)
    clock = [1000.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    active = [SimpleNamespace(conversation=object())]
    adapter.gateway_runner._peek_session_state = lambda key: active[0]
    source = _source(thread="17")
    await ui.open_settings(source, settings_payload(settings={}), "session-A")
    state = next(iter(ui._states.values()))
    original_id = state["message_id"]
    await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:input:temperature")
    pending = ui._pending[state["identity"]]
    assert pending["expires_at"] == clock[0] + 600
    assert 599 <= pending["timer"].when() - asyncio.get_running_loop().time() <= 600
    event = SimpleNamespace(message=SimpleNamespace(id=1234, date=datetime.fromtimestamp(clock[0], UTC)))
    text = "2"
    input_source = source
    if scenario == "invalid":
        text = "3"
    elif scenario == "unrelated":
        text = "hello there"
    elif scenario == "wrong_reply":
        event.message.reply_to = SimpleNamespace(reply_to_msg_id=999)
    elif scenario == "old_message":
        event.message.date = datetime.fromtimestamp(clock[0] - 5, UTC)
    elif scenario == "other_user":
        input_source = _source(user="99", thread="17")
    elif scenario == "other_chat":
        input_source = _source(chat="99", thread="17")
    elif scenario == "other_thread":
        input_source = _source(thread="18")
    elif scenario == "other_profile":
        input_source = _source(profile="beta", thread="17")
    elif scenario == "group":
        input_source = _source(thread="17", chat_type="group")
    elif scenario == "owner_changed":
        adapter.owner_ids["alpha"] = "99"
    elif scenario in {"expired", "timer_expired"}:
        clock[0] = pending["expires_at"]
        if scenario == "timer_expired":
            pending["timer"]._run()
            assert not ui._pending
            assert "pending_field" not in state
    elif scenario == "cancel":
        assert await ui.handle_message(SimpleNamespace(), source, "/cancel")
        assert not ui._pending
        assert "Enter Temperature" not in adapter.requests[-1].rich_message.html
    elif scenario == "navigate":
        await ui.handle_callback(_Callback(original_id), source, f"ms:{state['id']}:page:output")
        assert not ui._pending
    elif scenario == "replaced":
        await ui.open_settings(source, settings_payload(settings={}), "session-A")
        assert not ui._pending
    elif scenario == "stale_parameter":
        state["pending_field"] = "top_p"
    elif scenario == "new_conversation":
        active[0] = SimpleNamespace(conversation=object())
    elif scenario == "route_changed":
        adapter.gateway_runner._session_model_overrides = {"session-A": {"model": "another-model"}}
    elif scenario in {"session_changed", "expires_during_resolution"}:
        def normalize(src):
            if scenario == "expires_during_resolution":
                clock[0] += 600
            return src
        adapter.gateway_runner._normalize_source_for_session_key = normalize
        adapter.gateway_runner._session_key_for_source = lambda src: "other-session" if scenario == "session_changed" else "session-A"
    elif scenario == "rejected":
        adapter.gateway_runner._handle_msettings_command = AsyncMock(return_value="Setting rejected")
    elif scenario == "missing_handler":
        adapter.gateway_runner._handle_msettings_command = None
    elif scenario == "unconfirmed_same_value":
        text = "0.5"
        state["context"]["settings"]["fields"][0]["value"] = 0.5
        adapter.gateway_runner._handle_msettings_command = AsyncMock(return_value=None)
    elif scenario == "mismatched_refresh":
        async def refresh_wrong_value(event):
            await ui.open_settings(event.source, settings_payload(settings={"temperature": 0.7}), "session-A")
        adapter.gateway_runner._handle_msettings_command = refresh_wrong_value
    elif scenario == "delete_failure":
        adapter._client.delete_messages.side_effect = RuntimeError("cannot delete")

    before = len(adapter.requests)
    await ui.handle_message(event, input_source, text)
    if ui._expiry_tasks:
        await asyncio.gather(*ui._expiry_tasks)
    if scenario in {"expired", "timer_expired", "expires_during_resolution"}:
        assert not ui._pending
        assert "pending_field" not in state
        assert "Value prompt expired" in adapter.requests[-1].rich_message.html
        assert "<b>Enter Temperature</b>" not in adapter.requests[-1].rich_message.html
        assert adapter.requests[-1].id == int(original_id)
        event.message.reply_to = SimpleNamespace(reply_to_msg_id=int(original_id))
        assert await ui.handle_message(event, source, text)
    if scenario in {"accepted", "delete_failure"}:
        assert adapter.gateway_runner.settings["temperature"] == 2
        assert adapter.gateway_runner.calls[-1].text == "/msettings set temperature 2.0"
        adapter._client.delete_messages.assert_awaited_once_with(("peer", source.chat_id), [1234], revoke=True)
        assert not ui._pending
        assert "pending_field" not in state
        assert len(adapter.requests) == before + 1
        assert adapter.requests[-1].id == int(original_id)
        assert state["message_id"] == original_id
        assert "Enter Temperature" not in adapter.requests[-1].rich_message.html
        # A duplicate delivery must not apply or delete a second time.
        assert not await ui.handle_message(event, source, text)
        adapter._client.delete_messages.assert_awaited_once()
    else:
        adapter._client.delete_messages.assert_not_awaited()
        assert not any(e.text.startswith("/msettings set") for e in adapter.gateway_runner.calls)
        assert "temperature" not in adapter.gateway_runner.settings or adapter.gateway_runner.settings["temperature"] == 0.5
        if scenario in {"invalid", "unrelated", "wrong_reply", "old_message", "other_user", "other_chat", "other_thread", "other_profile"}:
            assert ui._pending[state["identity"]] is pending
            assert pending["expires_at"] == 1600  # Invalid messages cannot extend the deadline.
    for key in list(ui._pending):
        ui._clear_pending(key)


def _rich_body(request):
    from xml.etree import ElementTree
    from telethon.tl import types

    assert isinstance(request.rich_message, types.InputRichMessageHTML)
    return ElementTree.fromstring("<body>" + request.rich_message.html.replace("<br>", "<br/>") + "</body>")


def _walk_rich(value):
    from telethon import types

    stack = list(value) if isinstance(value, (list, tuple)) else [value]
    while stack:
        node = stack.pop()
        if isinstance(node, types.TLObject):
            yield node
            if isinstance(node, types.TextButton):
                stack.append(node.text)
                continue
            children = []
            for child in vars(node).values():
                if isinstance(child, (list, tuple)):
                    children.extend(child)
                elif isinstance(child, types.TLObject):
                    children.append(child)
            stack.extend(reversed(children))


def _rich_plain_text(value):
    from telethon import types

    if isinstance(value, str):
        return value
    if isinstance(value, types.TextPlain):
        return value.text
    if isinstance(value, types.TextButton):
        return _rich_plain_text(value.text)
    if isinstance(value, types.TextConcat):
        return "".join(_rich_plain_text(child) for child in value.texts)
    if isinstance(value, types.TLObject):
        if hasattr(value, "text"):
            return _rich_plain_text(value.text)
        if hasattr(value, "texts"):
            return "".join(_rich_plain_text(child) for child in value.texts)
        if hasattr(value, "blocks"):
            return "\n".join(_rich_plain_text(child) for child in value.blocks)
    if isinstance(value, (list, tuple)):
        return "\n".join(_rich_plain_text(child) for child in value)
    return ""


@pytest.mark.asyncio
@pytest.mark.parametrize("page,index,total", [("sampling", 1, 4), ("output", 2, 1), ("capabilities", 3, 1)])
@pytest.mark.parametrize("supported", [True, False, None])
async def test_actual_settings_richmessage_section_hierarchy(page, index, total, supported):
    from plugins.platforms.telegram.model_settings_ui import _PARAMETER_HELP, _RANGE_NOTE

    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    values = {"temperature": 0.0, "top_p": 0.0, "frequency_penalty": -2.0,
              "presence_penalty": -2.0, "max_tokens": 4096}
    payload = settings_payload(settings=values, page=page, reasoning_effort="high",
                               supported_reasoning_efforts=["low", "high"], reasoning_supported=supported)
    await ui.open_settings(_source(), payload, "session-A")
    body = _rich_body(adapter.requests[-1])
    children = list(body)
    assert [e.tag for e in children[:4]] == ["h1", "p", "ul", "hr"]
    assert len(body.findall("hr")) == 2
    assert children[0].text == f"Model settings — {payload['page_label']}"
    assert children[1].text == "Changes apply only to this conversation's API requests."
    assert children[2].find("li").text == _RANGE_NOTE
    state = next(iter(ui._states.values()))
    for offset, field in enumerate(payload["fields"]):
        heading, current, explanation, controls = children[4 + offset * 4:8 + offset * 4]
        assert [e.tag for e in (heading, current, explanation, controls)] == ["h2", "p", "ul", "tg-button-row"]
        assert heading.text == field["label"]
        assert "".join(current.itertext()) == f"Currently {values[field['key']]}"
        assert current.find("b") is not None
        assert current.find("mark") is None
        assert explanation.find("li").text == (
            f"Input range {field['minimum']:g}–{field['maximum']:g}. {_PARAMETER_HELP[field['key']]}"
        )
        # The adjust controls live in the message under their setting, not in
        # a keyboard row: + / − / clear (×) / type a value (Type).
        control_buttons = controls.findall("tg-button")
        assert [button.text for button in control_buttons] == ["+", "−", "×", "Type"]
        assert [button.get("data") for button in control_buttons] == [
            f"ms:{state['id']}:inc:{field['key']}",
            f"ms:{state['id']}:dec:{field['key']}",
            f"ms:{state['id']}:reset:{field['key']}",
            f"ms:{state['id']}:input:{field['key']}",
        ]
    if page == "capabilities":
        assert children[4].tag == "h2" and children[4].text == "Reasoning effort"
        assert "".join(children[5].itertext()) == "Currently high"
        assert children[5].find("b") is not None
        assert children[5].find("mark") is None
        assert "tokens or time" in children[6].find("li").text
        assert "does not guarantee support" in "".join(body.find("blockquote").itertext())
        assert ("Provider-reported values" in "".join(body.itertext())) is (supported is True)
    assert children[-2].tag == "hr"
    assert children[-1].tag == "p"
    # The footer total is not bolded: <b> nested inside <i> is dropped by the
    # HTML→rich round-trip, so plain text is the honest contract here.
    assert "".join(children[-1].find("i").itertext()) == f"Page {index} of 3 • total {total}"
    assert len(body.findall("h1")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens"])
@pytest.mark.parametrize("pending_input", [False, True])
async def test_left_parameter_names_are_inert_and_preserve_pending_input(key, pending_input):
    import copy

    adapter = _Adapter()
    adapter.gateway_runner.ui = ui = TelegramModelSettingsUI(adapter)
    source = _source()
    page = "output" if key == "max_tokens" else "sampling"
    await ui.open_settings(source, settings_payload(settings={}, page=page), "session-A")
    state = next(iter(ui._states.values()))
    text, _ = ui._settings_view(state)
    from xml.etree import ElementTree

    settings_body = ElementTree.fromstring("<body>" + text + "</body>")
    controls = [
        (button.text, button.get("data"))
        for row in settings_body.findall(".//tg-button-row")
        for button in row.findall("tg-button")
        if (button.get("data") or "").endswith(f":{key}")
    ]
    assert [label for label, _ in controls] == ["+", "−", "×", "Type"]
    assert [data for _, data in controls] == [
        f"ms:{state['id']}:inc:{key}",
        f"ms:{state['id']}:dec:{key}",
        f"ms:{state['id']}:reset:{key}",
        f"ms:{state['id']}:input:{key}",
    ]
    if pending_input:
        await ui.handle_callback(_Callback(state["message_id"]), source, f"ms:{state['id']}:input:{key}")
    pending = ui._pending.get(state["identity"])
    before_state = dict(state)
    before_context = copy.deepcopy(state["context"])
    before_requests = len(adapter.requests)
    before_calls = list(adapter.gateway_runner.calls)
    before_seen = dict(ui._seen_callbacks)
    callback = _Callback(state["message_id"])
    # The label itself is gone, but a noop callback sent directly must still
    # be inert (and still enforce settings authorization).
    assert await ui.handle_callback(callback, source, f"ms:{state['id']}:noop:{key}")
    assert callback.answers == [("", False)]
    assert state == before_state and state["context"] == before_context
    assert len(adapter.requests) == before_requests
    assert adapter.gateway_runner.calls == before_calls
    assert ui._seen_callbacks == before_seen
    assert ui._pending.get(state["identity"]) is pending
    if pending:
        assert not pending["timer"].cancelled()
        ui._clear_pending(state["identity"])


@pytest.mark.asyncio
async def test_dashboard_rich_message_keeps_metadata_literal_and_shows_active_model_total():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    await ui.open_dashboard(_source(), {"current_model": "<model>", "provider_name": "A&B",
                           "model_catalog_count": 25}, [], "<model>", "p", "session-A", "/tmp/profile-a")
    from telethon import types
    body = adapter.requests[-1].rich_message
    assert isinstance(body, types.InputRichMessage)
    rendered = _rich_plain_text(body.blocks)
    assert "Provider" in rendered
    assert "A&B" in rendered
    assert "<model>" in rendered
    assert "25 models" in rendered
    assert "Page 1 of 1 • total 1" in rendered
    assert "A&B" in adapter.requests[-1].message
    assert "<model>" in adapter.requests[-1].message
    assert len(body._bytes()) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("inc", 0.6), ("dec", 0.4), ("reset", None)])
async def test_separate_adjustment_controls_use_displayed_value(action, expected):
    adapter = _Adapter()
    adapter.gateway_runner.ui = ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_settings(source, settings_payload(settings={"temperature": 0.5}), "session-A")
    state = next(iter(ui._states.values()))
    await ui.handle_callback(_Callback(state["message_id"]), source, f"ms:{state['id']}:{action}:temperature")
    assert adapter.gateway_runner.settings.get("temperature") == expected
    assert adapter.requests[-1].id == int(state["message_id"])
    assert adapter.requests[-1].rich_message.html.endswith("<p><i>Page 1 of 3 • total 4</i></p>")


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["user", "group", "owner_changed"])
async def test_inert_label_still_enforces_settings_authorization(boundary):
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_settings(source, settings_payload(settings={}), "session-A")
    state = next(iter(ui._states.values()))
    before = len(adapter.requests)
    if boundary == "user":
        source = _source(user="99")
    elif boundary == "group":
        source = _source(chat_type="group")
    else:
        adapter.owner_ids["alpha"] = "99"
    callback = _Callback(state["message_id"])
    await ui.handle_callback(callback, source, f"ms:{state['id']}:noop:temperature")
    assert callback.answers[-1][1] is True
    assert len(adapter.requests) == before
    assert not adapter.gateway_runner.calls


@pytest.mark.asyncio
async def test_sampling_exactly_two_dividers_with_prompt_and_final_footer():
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    await ui.open_settings(source, settings_payload(settings={}), "session-A")
    state = next(iter(ui._states.values()))
    await ui.handle_callback(_Callback(state["message_id"]), source,
                             f"ms:{state['id']}:input:temperature")
    body = _rich_body(adapter.requests[-1])
    children = list(body)
    assert len(body.findall("hr")) == 2
    sections = [e.text for e in body.findall("h2")]
    assert sections == ["Temperature", "Top-p", "Frequency penalty", "Presence penalty"]
    first_divider, last_divider = [i for i, e in enumerate(children) if e.tag == "hr"]
    assert [e.tag for e in children[first_divider + 1:last_divider]] == ["h2", "p", "ul", "tg-button-row"] * 4
    assert children[last_divider + 1].tag == "blockquote"
    assert children[last_divider + 1].find("b").text == "Enter Temperature"
    assert children[-1].find("i").text == "Page 1 of 3 • total 4"
    ui._clear_pending(state["identity"])


@pytest.mark.asyncio
async def test_model_layout_does_not_mutate_shared_transport_or_unrelated_help():
    from plugins.platforms.telegram.mtproto_rich import input_rich_message
    from plugins.platforms.telegram.rich_ui import help_view

    before_help = help_view(skill_commands={})
    markdown = "# Unrelated heading\n\nAn unrelated feature.\n\n- One\n- Two"
    before_rich = input_rich_message(markdown).to_dict()
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    await ui.open_settings(_source(), settings_payload(settings={}), "session-A")
    await ui.open_dashboard(_source(), {"current_model": "m", "current_provider": "p"},
                            [], "m", "p", "session-A", "/tmp/profile-a")
    after_help = help_view(skill_commands={})
    assert before_help[0] == after_help[0]
    assert before_help[1].to_dict() == after_help[1].to_dict()
    assert input_rich_message(markdown).to_dict() == before_rich
    assert input_rich_message(markdown).markdown.startswith("# Unrelated heading")
    assert "<hr" not in input_rich_message(markdown).markdown


def test_offline_preview_uses_production_cards_without_active_controls():
    from pathlib import Path
    import runpy
    from xml.etree import ElementTree

    root = Path(__file__).resolve().parents[4]
    preview = runpy.run_path(str(root / "scripts" / "preview_telegram_model_ui.py"))
    html = preview["render_preview"]()
    assert html.endswith("</main></body></html>\n")
    assert "No message was sent and no setting was changed." in html
    # Inspect each production body, excluding HTML-only disabled keyboard buttons.
    import re
    articles = re.findall(r"<article>(.*?)</article>", html, flags=re.DOTALL)
    assert len(articles) == 2
    for article in articles:
        body = ElementTree.fromstring("<body>" + article + "</body>")
        assert len(body.findall("hr")) == 2
        assert len(body.findall("h2")) == 4
        assert list(body)[-1].find("i").text.startswith("Page 1 of ")
    assert "<button type=\"button\" disabled>" in html
    assert "<script" not in html and "<img" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["dashboard", "settings", "dashboard-to-settings"])
async def test_close_deletes_only_model_menu_and_exact_command_across_views(view):
    from unittest.mock import AsyncMock

    adapter = _Adapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    adapter.gateway_runner.ui = ui = TelegramModelSettingsUI(adapter)
    source = _source()
    if view == "settings":
        await ui.open_settings(source, settings_payload(settings={}), "session-A",
                               reply_to=88, invocation_message_id=77)
    else:
        await ui.open_dashboard(source, {"current_model": "m", "current_provider": "p"}, [],
                                "m", "p", "session-A", "/tmp/profile-a",
                                reply_to=88, invocation_message_id=77)
    state = next(iter(ui._states.values()))
    menu_id = int(state["message_id"])
    if view == "dashboard-to-settings":
        await ui.handle_callback(_Callback(menu_id), source, f"ms:{state['id']}:settings")
        assert state["view"] == "settings"
        assert state["invocation_message_id"] == 77
        await ui.handle_callback(_Callback(menu_id), source, f"ms:{state['id']}:input:temperature")
        assert ui._pending
    previous_requests = len(adapter.requests)
    close = f"ms:{state['id']}:close"
    await ui.handle_callback(_Callback(menu_id), source, close)
    adapter._client.delete_messages.assert_awaited_once_with(
        ("peer", "42"), [menu_id, 77], revoke=True)
    assert len(adapter.requests) == previous_requests  # Never render a closed placeholder.
    assert not ui._states and not ui._owners and not ui._pending
    await ui.handle_callback(_Callback(menu_id), source, close)
    assert adapter._client.delete_messages.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "other_user", "other_chat", "other_profile", "other_thread", "other_message",
    "owner_revoked", "no_invocation", "invalid_invocation", "delete_failure",
])
async def test_model_close_is_feature_scoped_and_never_guesses_a_command(scenario):
    from unittest.mock import AsyncMock

    adapter = _Adapter()
    adapter._client = SimpleNamespace(delete_messages=AsyncMock())
    ui = TelegramModelSettingsUI(adapter)
    source = _source()
    invocation = None if scenario == "no_invocation" else (
        "invalid" if scenario == "invalid_invocation" else 77)
    await ui.open_settings(source, settings_payload(settings={}), "session-A",
                           reply_to=88, invocation_message_id=invocation)
    state = next(iter(ui._states.values()))
    menu_id = int(state["message_id"])
    callback_id = menu_id
    callback_source = source
    if scenario == "other_user":
        callback_source = _source(user="99")
    elif scenario == "other_chat":
        callback_source = _source(chat="99")
    elif scenario == "other_profile":
        callback_source = _source(profile="beta")
    elif scenario == "other_thread":
        callback_source = _source(thread="another")
    elif scenario == "other_message":
        callback_id = menu_id + 1
    elif scenario == "owner_revoked":
        adapter.owner_ids.clear()
    elif scenario == "delete_failure":
        adapter._client.delete_messages.side_effect = RuntimeError("temporary transport failure")
    callback = _Callback(callback_id)
    await ui.handle_callback(callback, callback_source, f"ms:{state['id']}:close")
    if scenario in {"no_invocation", "invalid_invocation"}:
        adapter._client.delete_messages.assert_awaited_once_with(
            ("peer", "42"), [menu_id], revoke=True)
        assert not ui._states
    elif scenario == "delete_failure":
        assert ui._states[state["id"]] is state
        assert callback.answers[-1][1] is True
        adapter._client.delete_messages.side_effect = None
        await ui.handle_callback(_Callback(menu_id), source, f"ms:{state['id']}:close")
        assert not ui._states
        assert adapter._client.delete_messages.await_args.args[1] == [menu_id, 77]
    else:
        adapter._client.delete_messages.assert_not_awaited()
        assert ui._states[state["id"]] is state
        assert callback.answers[-1][1] is True


def _has_emoji(text):
    """Characters in the Unicode emoji ranges (incl. Dingbats)."""
    for ch in str(text):
        code = ord(ch)
        if (0x1F000 <= code <= 0x1FAFF or 0x2600 <= code <= 0x27BF
                or code in (0xFE0F, 0x200D)
                or 0x2190 <= code <= 0x21FF or 0x2300 <= code <= 0x23FF
                or 0x2B00 <= code <= 0x2BFF or 0x1F100 <= code <= 0x1F1FF):
            return True
    return False


@pytest.mark.asyncio
async def test_settings_view_has_no_emoji():
    """0 emojis on the /msettings screens, in text or buttons."""
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    payload = settings_payload(settings={"temperature": 0.5, "max_tokens": 2048},
                               page="sampling", reasoning_effort="high",
                               supported_reasoning_efforts=["low", "high"],
                               reasoning_supported=True)
    await ui.open_settings(_source(), payload, "session-A")
    state = next(iter(ui._states.values()))
    text, markup = ui._settings_view(state)
    assert not _has_emoji(text)
    for row in markup.rows:
        for button in row.buttons:
            assert not _has_emoji(button.text)
    await ui.handle_callback(_Callback(state["message_id"]), _source(), f"ms:{state['id']}:page:capabilities")
    text, markup = ui._settings_view(state)
    assert not _has_emoji(text)


@pytest.mark.asyncio
async def test_settings_current_values_are_bold_highlighted():
    """Important values on the settings pages are highlighted: the current
    value of each field and the reasoning effort are bold, and the dashboard
    footer total is bold in the rich message itself."""
    adapter = _Adapter()
    ui = TelegramModelSettingsUI(adapter)
    payload = settings_payload(settings={"temperature": 0.5, "max_tokens": 2048},
                               page="sampling", reasoning_effort="high",
                               supported_reasoning_efforts=["low", "high"],
                               reasoning_supported=True)
    await ui.open_settings(_source(), payload, "session-A")
    state = next(iter(ui._states.values()))
    text, _ = ui._settings_view(state)
    from xml.etree import ElementTree

    body = ElementTree.fromstring("<body>" + text + "</body>")
    paragraphs = [element for element in body if element.tag == "p"]
    current_lines = [element for element in paragraphs
                     if "".join(element.itertext()).startswith("Currently ")]
    assert current_lines
    for element in current_lines:
        assert element.find("b") is not None
    # The settings footer total stays plain: <b> inside <i> is dropped by the
    # HTML→rich round-trip. The dashboard footer is built as Telethon types,
    # where TextConcat/TextBold survives, so its total is genuinely bold.
    await ui.open_dashboard(_source(), {"current_model": "m", "current_provider": "p"}, [],
                            "m", "p", "session-A", "/tmp/profile-a")
    dashboard_state = list(ui._states.values())[-1]
    dashboard_rich, _ = ui._dashboard_view(dashboard_state)
    from telethon import types
    footer = dashboard_rich.blocks[-1]
    assert isinstance(footer.text, types.TextItalic)
    concat = footer.text.text
    assert isinstance(concat, types.TextConcat)
    assert any(isinstance(part, types.TextBold) and part.text.text == "1"
               for part in concat.texts)
