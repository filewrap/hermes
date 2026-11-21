from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.platforms.telegram.setup_ui import TelegramSetupUI


class _Adapter:
    def __init__(self, home: Path):
        self.home = home
        self._client = object()
        self.sent = []
        self.requests = []
        self.gateway_runner = SimpleNamespace(
            _resolve_profile_home_for_source=lambda _source: self.home,
        )

    def _authorized(self, source):
        return not getattr(source, "denied", False)

    def _is_owner_user(self, user_id, *, source=None):
        return str(user_id) == "101" and source is not None

    def _peer_for(self, chat_id):
        return chat_id

    async def _mtproto_rich_request(self, request):
        self.requests.append(request)
        return SimpleNamespace(id=900 + len(self.requests))

    def _sent_message_id(self, result, **_kwargs):
        return getattr(result, "id", None)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _Event:
    def __init__(self, message_id="901"):
        self.message_id = message_id
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def _source(*, chat_type="dm", user_id="101", chat_id="101", profile="alpha", thread_id=None):
    return SimpleNamespace(chat_type=chat_type, user_id=user_id, chat_id=chat_id,
                           profile=profile, thread_id=thread_id)


@pytest.mark.asyncio
async def test_setup_command_refuses_non_private_or_non_owner(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)

    await ui.handle_command(_source(chat_type="group"))
    await ui.handle_command(_source(user_id="202"))

    assert len(adapter.sent) == 2
    assert not adapter.requests
    assert not ui._states


@pytest.mark.asyncio
async def test_html_setup_home_escapes_profile_and_binds_callback_card(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    source = _source(profile="<alpha>")

    await ui.handle_command(source)

    state = next(iter(ui._states.values()))
    request = adapter.requests[0]
    assert state["message_id"] == "901"
    assert "<h1>Hermes setup</h1>" in request.rich_message.html
    assert "<table>" in request.rich_message.html
    assert "&lt;alpha&gt;" in request.rich_message.html
    from plugins.platforms.telegram.setup_fields import HOME_ITEMS
    assert f"total {len(HOME_ITEMS)}</i></p>" in request.rich_message.html
    assert request.rich_message.html.rstrip().endswith("</p>")

    foreign_card = _Event(message_id="902")
    handled = await ui.handle_callback(
        foreign_card, source, f"hm:setup:{state['id']}:section:terminal")
    assert handled is True
    assert foreign_card.answers[0][1].get("alert") is True
    assert state["section"] == "home"

    wrong_actor = _Event(message_id="901")
    handled = await ui.handle_callback(
        wrong_actor, _source(user_id="202"), f"hm:setup:{state['id']}:section:terminal")
    assert handled is True
    assert state["section"] == "home"


@pytest.mark.asyncio
async def test_typed_input_validates_and_persists_to_active_profile_config(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "browser:\n  backend: browser-use\n  command_timeout: 19\ncompression:\n  threshold: 0.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    adapter = _Adapter(home)
    ui = TelegramSetupUI(adapter)
    source = _source(profile="alpha")

    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    callback = _Event(message_id=state["message_id"])
    assert await ui.handle_callback(callback, source, f"hm:setup:{state['id']}:section:compression")
    assert await ui.handle_callback(callback, source, f"hm:setup:{state['id']}:edit:compression.threshold")

    invalid = await ui.handle_message(_Event(message_id="901"), source, "nan")
    assert invalid is True
    assert state["pending_field"] == "compression.threshold"
    assert "finite number" in adapter.requests[-1].rich_message.html

    valid = await ui.handle_message(_Event(message_id="902"), source, "0.68")
    assert valid is True
    assert state["pending_field"] is None

    from hermes_cli.config_effective import load_user_config_effective
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home):
        config = load_user_config_effective(fail_closed=True)
    assert config["compression"]["threshold"] == pytest.approx(0.68)
    assert config["browser"]["backend"] == "browser-use"
    assert config["browser"]["command_timeout"] == 19
    assert "Readback: 0.68" in adapter.requests[-1].rich_message.html


@pytest.mark.asyncio
async def test_profile_scoped_effective_config_mutation_alternates_a_b_a(tmp_path, monkeypatch):
    launch_home = tmp_path / "launch-home"
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home, browser, model, threshold in (
        (home_a, "browser-use", "provider/model-a", 0.35),
        (home_b, "browserless", "provider/model-b", 0.45),
    ):
        home.mkdir()
        (home / "config.yaml").write_text(
            f"browser:\n  backend: {browser}\n  command_timeout: 19\n"
            f"model:\n  default: {model}\n"
            f"compression:\n  threshold: {threshold}\n"
            f"display:\n  platforms:\n    telegram:\n      streaming: {str(home is home_a).lower()}\n",
            encoding="utf-8",
        )
    launch_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    adapter = _Adapter(launch_home)
    ui = TelegramSetupUI(adapter)
    state_a = {"home": str(home_a)}
    state_b = {"home": str(home_b)}

    ui._save_value(state_a, "compression.threshold", 0.60)
    ui._save_value(state_a, "display.platforms.telegram.streaming", False)
    first_a = ui._load_config(state_a)
    ui._save_value(state_b, "compression.threshold", 0.70)
    ui._save_value(state_b, "display.platforms.telegram.streaming", True)
    config_b = ui._load_config(state_b)
    ui._save_value(state_a, "compression.threshold", 0.80)
    ui._save_value(state_a, "display.platforms.telegram.streaming", True)
    final_a = ui._load_config(state_a)

    assert first_a["compression"]["threshold"] == pytest.approx(0.60)
    assert first_a["display"]["platforms"]["telegram"]["streaming"] is False
    assert config_b["compression"]["threshold"] == pytest.approx(0.70)
    assert config_b["display"]["platforms"]["telegram"]["streaming"] is True
    assert final_a["compression"]["threshold"] == pytest.approx(0.80)
    assert final_a["display"]["platforms"]["telegram"]["streaming"] is True
    for config, browser, model in (
        (final_a, "browser-use", "provider/model-a"),
        (config_b, "browserless", "provider/model-b"),
    ):
        assert config["browser"]["backend"] == browser
        assert config["browser"]["command_timeout"] == 19
        assert config["model"]["default"] == model
    assert os.environ["HERMES_HOME"] == str(launch_home)


@pytest.mark.asyncio
async def test_pending_input_is_bound_to_actor_chat_profile_and_thread(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    source = _source(thread_id="77")
    state = ui._new_state(source, adapter.home, "terminal")
    state["message_id"] = "333"
    state["pending_field"] = "terminal.timeout"
    state["pending_expires_at"] = 9999999999
    identity = state["identity"]
    ui._pending[identity] = {
        "state_id": state["id"], "field": "terminal.timeout", "expires_at": 9999999999,
    }

    assert await ui.handle_message(_Event(), _source(thread_id="88"), "60") is False
    assert await ui.handle_message(_Event(), _source(user_id="202", thread_id="77"), "60") is False
    assert ui._pending[identity]["field"] == "terminal.timeout"


@pytest.mark.asyncio
async def test_missing_thread_does_not_recover_pending_editor_from_private_topic(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    topic_source = _source(thread_id="77")
    state = ui._new_state(topic_source, adapter.home, "terminal")
    state["message_id"] = "333"
    state["pending_field"] = "terminal.timeout"
    state["pending_expires_at"] = 9999999999
    ui._pending[state["identity"]] = {
        "state_id": state["id"], "field": "terminal.timeout", "expires_at": 9999999999,
    }

    main_dm_source = _source(thread_id=None)
    assert await ui.handle_message(_Event(), main_dm_source, "60") is False
    assert ui._pending[state["identity"]]["field"] == "terminal.timeout"
    assert state["pending_field"] == "terminal.timeout"
    assert not adapter.requests


@pytest.mark.asyncio
async def test_toggle_callback_payload_is_one_shot(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("display:\n  show_reasoning: false\n", encoding="utf-8")
    adapter = _Adapter(home)
    ui = TelegramSetupUI(adapter)
    source = _source()
    await ui.handle_command(source)
    state = next(iter(ui._states.values()))
    callback = _Event(message_id=state["message_id"])
    assert await ui.handle_callback(
        callback, source, f"hm:setup:{state['id']}:section:display")

    state = next(iter(ui._states.values()))
    toggle_data = f"hm:setup:{state['id']}:toggle:display.show_reasoning"
    save = ui._save_value
    saved = []

    def count_save(*args, **kwargs):
        saved.append(args[2:])
        return save(*args, **kwargs)

    monkeypatch.setattr(ui, "_save_value", count_save)
    assert await ui.handle_callback(callback, source, toggle_data)
    assert await ui.handle_callback(callback, source, toggle_data)

    assert len(saved) == 1
    from hermes_cli.config_effective import load_user_config_effective
    from gateway.run import _profile_runtime_scope
    with _profile_runtime_scope(home):
        config = load_user_config_effective(fail_closed=True)
    assert config["display"]["show_reasoning"] is True
    assert callback.answers[-1][1].get("alert") is True


@pytest.mark.asyncio
async def test_navigation_hands_off_card_and_preserves_source_identity(tmp_path):
    from gateway.config import Platform
    from gateway.session import SessionSource

    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    originating_source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="101", user_id="101", thread_id="77",
        profile="alpha",
    )
    identity = object()
    transport_ref = object()
    authorization_home = tmp_path / "authorized-profile"
    originating_source._identity = identity
    originating_source._transport_adapter_ref = transport_ref
    originating_source._authorization_profile_home = authorization_home
    await ui.handle_command(originating_source)
    state = next(iter(ui._states.values()))
    old_id = state["id"]
    callback = _Event(message_id=state["message_id"])
    received = []

    async def dispatch(source, command, *, event=None):
        received.append((source, command, event, len(adapter.requests)))

    adapter._dispatch_control_command = dispatch
    # Some callback paths have the card id but no thread metadata on the source copy.
    callback_source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="101", user_id="101", profile="alpha",
    )
    callback_source._identity = identity
    callback_source._transport_adapter_ref = transport_ref
    callback_source._authorization_profile_home = authorization_home
    assert await ui.handle_callback(
        callback, callback_source, f"hm:setup:{old_id}:nav:browser")

    assert len(received) == 1
    routed_source, command, event, request_count = received[0]
    assert command == "browser" and event is callback
    assert request_count == 1  # dispatcher receives the callback without a hub refresh
    assert routed_source is not callback_source
    assert routed_source.thread_id == "77"
    assert routed_source._identity is identity
    assert routed_source._transport_adapter_ref is transport_ref
    assert routed_source._authorization_profile_home == authorization_home
    assert state["id"] != old_id
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_home_menu_paginates_with_accurate_counts_and_one_shot_callbacks(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    source = _source()
    await ui.handle_command(source)

    state = next(iter(ui._states.values()))
    html = adapter.requests[-1].rich_message.html
    from plugins.platforms.telegram.setup_fields import HOME_ITEMS, HOME_PAGE_SIZE
    pages = (len(HOME_ITEMS) + HOME_PAGE_SIZE - 1) // HOME_PAGE_SIZE
    assert f"Page 1 of {pages} • total {len(HOME_ITEMS)}" in html
    assert "Terminal" in html and "Voice" in html
    next_payload = next(
        button.type.data.decode()
        for row in adapter.requests[-1].reply_markup.rows for button in row.buttons
        if getattr(button.type, "data", b"").decode().endswith(":page:2")
    )
    callback = _Event(message_id=state["message_id"])
    assert await ui.handle_callback(callback, source, next_payload)

    assert f"Page 2 of {pages} • total {len(HOME_ITEMS)}" in adapter.requests[-1].rich_message.html
    assert "Personal settings and memory" in adapter.requests[-1].rich_message.html
    assert "Models" in adapter.requests[-1].rich_message.html
    labels = [button.text for row in adapter.requests[-1].reply_markup.rows for button in row.buttons]
    assert f"Next · 3/{pages}" in labels
    assert f"Previous · 1/{pages}" in labels


@pytest.mark.asyncio
async def test_option_callback_persists_and_reads_back_supported_agent_modes(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("agent:\n  tool_use_enforcement: auto\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    adapter = _Adapter(home)
    ui = TelegramSetupUI(adapter)
    source = _source()
    await ui.handle_command(source, "agent")

    state = next(iter(ui._states.values()))
    callback = _Event(message_id=state["message_id"])
    assert await ui.handle_callback(callback, source, f"hm:setup:{state['id']}:cycle:agent.tool_use_enforcement")
    assert "does not change configuration" in adapter.requests[-1].rich_message.html
    assert ui._load_config(state)["agent"]["tool_use_enforcement"] == "auto"

    select_payload = next(
        button.type.data.decode()
        for row in adapter.requests[-1].reply_markup.rows for button in row.buttons
        if button.text == "Always on"
    )
    assert await ui.handle_callback(callback, source, select_payload)
    assert "Saved and read back: Always on" in adapter.requests[-1].rich_message.html
    assert ui._load_config(state)["agent"]["tool_use_enforcement"] == "true"


def test_setup_input_validation_rejects_malformed_language_voice_and_bounds(tmp_path):
    from plugins.platforms.telegram.setup_fields import FIELDS

    parse = TelegramSetupUI._parse_value
    assert parse(FIELDS["stt.language"], "en-US") == "en-US"
    assert parse(FIELDS["stt.language"], "AUTO") == "auto"
    assert parse(FIELDS["tts.edge.voice"], "en-US-AriaNeural") == "en-US-AriaNeural"
    with pytest.raises(ValueError, match="language tag"):
        parse(FIELDS["stt.language"], "not a language")
    with pytest.raises(ValueError, match="voice name"):
        parse(FIELDS["tts.edge.voice"], "bad voice")
    with pytest.raises(ValueError, match="whole number"):
        parse(FIELDS["terminal.timeout"], "1.5")


@pytest.mark.asyncio
async def test_pending_editor_cancels_for_slash_commands_without_consuming_them(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    source = _source()
    state = ui._new_state(source, adapter.home, "terminal")
    state["message_id"] = "333"
    state["pending_field"] = "terminal.timeout"
    state["pending_expires_at"] = 9999999999
    ui._pending[state["identity"]] = {
        "state_id": state["id"], "field": "terminal.timeout", "expires_at": 9999999999,
    }

    assert await ui.handle_message(_Event(), source, "/help") is False
    assert state["pending_field"] is None
    assert not ui._pending
    assert "Input canceled because a command was received." in adapter.requests[-1].rich_message.html

    state["pending_field"] = "terminal.timeout"
    state["pending_expires_at"] = 9999999999
    ui._pending[state["identity"]] = {
        "state_id": state["id"], "field": "terminal.timeout", "expires_at": 9999999999,
    }
    assert await ui.handle_message(_Event(), source, "/cancel") is True
    assert state["pending_field"] is None
    assert not ui._pending
    assert "Input canceled. No setting was changed." in adapter.requests[-1].rich_message.html


@pytest.mark.asyncio
async def test_reopening_setup_reuses_card_and_live_actions_never_send_replacements(tmp_path):
    from telethon import functions

    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)
    source = _source()
    await ui.handle_command(source)
    original = next(iter(ui._states.values()))["message_id"]
    await ui.handle_command(source, "agent")
    state = next(iter(ui._states.values()))
    assert state["message_id"] == original
    assert isinstance(adapter.requests[0], functions.messages.SendMessageRequest)
    assert all(isinstance(request, functions.messages.EditMessageRequest) and str(request.id) == original
               for request in adapter.requests[1:])
    callback = _Event(message_id=original)
    await ui.handle_callback(callback, source, f"hm:setup:{state['id']}:edit:agent.max_turns")
    assert await ui.handle_message(_Event(), source, "120")
    assert "Readback: 120" in adapter.requests[-1].rich_message.html
    assert all(isinstance(request, functions.messages.EditMessageRequest) and str(request.id) == original
               for request in adapter.requests[1:])
    assert not adapter.sent


def test_expanded_fields_use_existing_config_and_preserve_auth_across_profiles(tmp_path):
    import yaml
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from plugins.platforms.telegram.setup_fields import FIELDS
    from plugins.platforms.telegram.setup_ui import _MISSING

    for key in FIELDS:
        if key == "display.platforms.telegram.tool_progress":
            from gateway.display_config import resolve_tool_progress
            assert TelegramSetupUI._value({}, key) == resolve_tool_progress({}, "telegram")[0]
        else:
            assert TelegramSetupUI._nested_value(DEFAULT_CONFIG, key) is not _MISSING
    homes = [tmp_path / "a", tmp_path / "b"]
    auth = {"username": "owner-test", "password_hash": "test-only-hash"}
    for home in homes:
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump({
            "dashboard": {"basic_auth": auth, "require_auth_on_loopback": True},
            "model": {"default": "test/model"},
        }))
        (home / ".env").write_text("TEST_SECRET=not-a-live-value\n")
    ui = TelegramSetupUI(_Adapter(homes[0]))
    for home, voice, turns in [(homes[0], "echo", 120), (homes[1], "nova", 70), (homes[0], "sage", 150)]:
        state = {"home": str(home)}
        assert ui._save_value(state, "tts.openai.voice", voice) == voice
        assert ui._save_value(state, "agent.max_turns", turns) == turns
        assert ui._save_value(state, "display.platforms.telegram.tool_progress", "new") == "new"
        raw = yaml.safe_load((home / "config.yaml").read_text())
        assert raw["dashboard"] == {"basic_auth": auth, "require_auth_on_loopback": True}
        assert raw["model"]["default"] == "test/model"
        assert (home / ".env").read_text() == "TEST_SECRET=not-a-live-value\n"
    assert ui._load_config({"home": str(homes[1])})["tts"]["openai"]["voice"] == "nova"
    assert ui._load_config({"home": str(homes[0])})["tts"]["openai"]["voice"] == "sage"


def test_toggle_confirm_does_not_describe_turning_off_when_turning_on(tmp_path):
    adapter = _Adapter(tmp_path / "profile")
    ui = TelegramSetupUI(adapter)

    on_text, _ = ui._toggle_confirm_view(
        {"id": "s1", "confirm_toggle": ("memory.write_approval", True)})
    assert "Turning approval off" not in on_text
    assert "On" in on_text

    off_text, _ = ui._toggle_confirm_view(
        {"id": "s1", "confirm_toggle": ("memory.write_approval", False)})
    assert "Turning approval off" in off_text
