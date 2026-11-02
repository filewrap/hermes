from __future__ import annotations
import asyncio
import os
from types import SimpleNamespace
import pytest
from plugins.platforms.telegram import standalone_ui
from plugins.platforms.telegram.standalone_ui import TelegramStandaloneUI


class _Adapter:
    def __init__(self, home: str):
        self.home = home
        self.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=lambda _source: home)
        self.sent = []
        self.edited = []
        self._client = object()

    def _is_owner_user(self, user_id, source=None):
        return str(user_id) == "100" and not getattr(source, "is_bot", False)

    def _authorized(self, source):
        return not getattr(source, "denied", False)

    async def _send_rich_ui(self, source, text, markup=None, reply_to=None):
        self.sent.append((source, text, markup))
        return SimpleNamespace(id=77)

    async def _edit_rich_ui_message(self, event, source, text, markup=None):
        self.edited.append((event, source, text, markup))
        return SimpleNamespace(id=getattr(event, "message_id", None) or 77)

    @staticmethod
    def _sent_message_id(result):
        return getattr(result, "id", None)


class _Event:
    def __init__(self, message_id=77):
        self.query = SimpleNamespace(message=SimpleNamespace(id=message_id))
        self.answered = []

    async def answer(self, text=""):
        self.answered.append(text)


def _source(user_id="100", chat_type="dm", profile="default"):
    return SimpleNamespace(
        user_id=user_id, chat_id="100", chat_type=chat_type,
        thread_id=None, profile=profile, is_bot=False,
    )


def _button_data(markup, label):
    for row in markup.rows:
        for button in row.buttons:
            raw = getattr(button, "button", button)
            if raw.text == label:
                payload = getattr(raw, "data", None)
                if payload is None:
                    payload = raw.type.data
                return payload.decode() if isinstance(payload, bytes) else payload
    raise AssertionError(f"missing button {label}")


def test_standalone_selector_and_confirmation_footers_keep_back_in_keyboard_only(monkeypatch, tmp_path):
    ui = TelegramStandaloneUI(_Adapter(str(tmp_path)))
    monkeypatch.setattr(ui, "_load_config", lambda: {"browser": {"engine": "chrome"}})
    monkeypatch.setattr(ui, "_browser_provider_options", lambda: (("auto", "Automatic"),))

    views = [
        ("browser-engines", {}, ui._browser_selector_view,
         "<p><i>Page 1 of 1 • total 3</i></p>", "← Back"),
        ("browser-providers", {}, ui._browser_selector_view,
         "<p><i>Page 1 of 1 • total 1</i></p>", "← Back"),
        ("browser-confirm", {"pending_browser_setting": {"key": "engine", "value": "lightpanda"}},
         ui._browser_confirmation_view,
         "<p><i>Page 1 of 1 • total 2</i></p>", "← Back"),
        ("dashboard-confirm", {"pending_dashboard_action": {"operation": "dashboard-start"}},
         ui._dashboard_confirmation_view,
         "<p><i>Page 1 of 1 • total 2</i></p>", "← Back"),
    ]
    for view, extra, render, footer, back_label in views:
        state = {"id": 1, "view": view, **extra}
        text, markup = render(state, "Status remains visible")
        assert text.count("<i>Page") == 1
        assert text.endswith(footer)
        assert "← Back" not in text
        assert "Status remains visible" in text
        assert _button_data(markup, back_label).startswith("hm:standalone:")


def test_standalone_controls_bind_callback_to_owner_private_message_and_consume_once(monkeypatch, tmp_path):
    adapter = _Adapter(str(tmp_path))
    ui = TelegramStandaloneUI(adapter)
    source = _source()

    assert asyncio.run(ui.handle_command(source, "browser", "")) is True
    first_text, first_markup = adapter.sent[0][1:]
    assert "<h1>Browser service</h1>" in first_text
    payload = _button_data(first_markup, "Refresh")

    # A non-owner cannot use a copied callback even if it is in a private chat.
    denied = asyncio.run(ui.handle_callback(_Event(), _source("101"), payload))
    assert denied is True
    assert adapter.edited == []

    # Valid owner tap navigates and rotates callback state/message binding.
    event = _Event()
    assert asyncio.run(ui.handle_callback(event, source, payload)) is True
    assert len(adapter.edited) == 1
    assert "<h1>Browser service</h1>" in adapter.edited[0][2]
    assert event.answered == [""]

    # Old one-shot payload cannot execute again.
    replay = _Event()
    asyncio.run(ui.handle_callback(replay, source, payload))
    assert replay.answered == ["This control expired or belongs to another chat."]
    assert len(adapter.edited) == 1


def test_dashboard_theme_uses_real_profile_config_loader_and_whitelisted_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import load_config, save_config

    save_config({"dashboard": {"theme": "default"}}, merge_existing=True)
    adapter = _Adapter(str(tmp_path))
    ui = TelegramStandaloneUI(adapter)
    source = _source()
    state = ui._new_state(source, "themes", 77)

    note = ui._run_action("theme:midnight", state)
    assert note.startswith("Dashboard theme saved")
    assert load_config()["dashboard"]["theme"] == "midnight"
    with pytest.raises(ValueError):
        ui._save_dashboard_theme("http://attacker.invalid")


def test_dashboard_theme_mutation_isolated_a_to_b_to_a_with_real_profile_scopes(monkeypatch, tmp_path):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config, save_config

    root = tmp_path / "home"
    profile_b = root / "profiles" / "b"
    profile_b.mkdir(parents=True)
    homes = {"default": root, "b": profile_b}
    monkeypatch.setenv("HERMES_HOME", str(root))
    for home in homes.values():
        with _profile_runtime_scope(home):
            save_config({"dashboard": {"theme": "default"}}, merge_existing=True)

    adapter = _Adapter(str(root))
    adapter.gateway_runner._resolve_profile_home_for_source = lambda source: str(homes[source.profile])
    ui = TelegramStandaloneUI(adapter)
    source_b = _source(profile="b")
    asyncio.run(ui.handle_command(source_b, "dashboard", ""))
    _, dashboard_markup = adapter.sent[-1][1:]
    asyncio.run(ui.handle_callback(_Event(), source_b, _button_data(dashboard_markup, "Theme: default")))
    _, themes_markup = adapter.edited[-1][2:]
    asyncio.run(ui.handle_callback(_Event(), source_b, _button_data(themes_markup, "midnight")))

    with _profile_runtime_scope(root):
        assert load_config()["dashboard"]["theme"] == "default"
    with _profile_runtime_scope(profile_b):
        assert load_config()["dashboard"]["theme"] == "midnight"
    with _profile_runtime_scope(root):
        assert load_config()["dashboard"]["theme"] == "default"


def test_browser_connect_and_disconnect_use_profile_config_not_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    from hermes_cli.config import load_config

    monkeypatch.setattr("hermes_cli.browser_connect.is_browser_debug_ready", lambda _url, timeout: True)
    assert "Saved the profile" in TelegramStandaloneUI._browser_connect("127.0.0.1:9222")
    assert load_config()["browser"]["cdp_url"] == "http://127.0.0.1:9222"
    assert "Cleared the profile" in TelegramStandaloneUI._browser_disconnect()
    assert load_config()["browser"].get("cdp_url", "") == ""

    monkeypatch.setenv("BROWSER_CDP_URL", "http://127.0.0.1:9222")
    assert "process-wide" in TelegramStandaloneUI._browser_disconnect()
    assert __import__("os").environ["BROWSER_CDP_URL"] == "http://127.0.0.1:9222"


def test_loopback_cdp_url_validation_rejects_remote_and_secret_bearing_urls():
    assert TelegramStandaloneUI._local_cdp_endpoint(None) == "http://127.0.0.1:9222"
    assert TelegramStandaloneUI._local_cdp_endpoint("localhost:9333") == "http://localhost:9333"
    for value in (
        "https://example.com:9222",
        "http://user:password@127.0.0.1:9222",
        "http://127.0.0.1:9222/?token=secret",
    ):
        with pytest.raises(ValueError):
            TelegramStandaloneUI._local_cdp_endpoint(value)


@pytest.mark.asyncio
async def test_private_topic_callback_keeps_stored_thread_and_rejects_foreign_thread(tmp_path):
    ui = TelegramStandaloneUI(_Adapter(str(tmp_path)))
    source = _source()
    source.thread_id = "77"
    await ui.handle_command(source, "browser", "")
    payload = _button_data(ui.adapter.sent[-1][2], "Refresh")
    foreign = _source()
    foreign.thread_id = "88"
    await ui.handle_callback(_Event(), foreign, payload)
    assert not ui.adapter.edited
    await ui.handle_callback(_Event(), _source(), payload)
    assert ui.adapter.edited[-1][1].thread_id == "77"
    assert next(reversed(ui._states.values()))["thread_id"] == "77"


@pytest.mark.asyncio
async def test_malformed_service_commands_return_visible_usage_without_actions(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ui = TelegramStandaloneUI(_Adapter(str(tmp_path)))
    action = Mock(side_effect=AssertionError("No operation should execute"))
    monkeypatch.setattr(ui, "_run_action", action)
    for command, args in [("browser", "connection"), ("browser", "disconnect extra"),
                          ("webui", "start --host 0.0.0.0"), ("dashboard", "theme unknown")]:
        assert await ui.handle_command(_source(), command, args)
        assert "Usage:" in ui.adapter.sent[-1][1]
    action.assert_not_called()


@pytest.mark.asyncio
async def test_browser_config_reaches_shared_runtime_reader_a_to_b_to_a(tmp_path, monkeypatch):
    import os
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import save_config
    from hermes_cli.config_effective import load_user_config_effective
    from tools.browser_session_cdp import get_cdp_override_raw
    root = tmp_path / "home"
    secondary = root / "profiles" / "b"
    secondary.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    homes = {"default": root, "b": secondary}
    for home in homes.values():
        with _profile_runtime_scope(home):
            save_config({"browser": {"backend": "local", "cdp_url": "", "command_timeout": 19},
                         "model": {"default": "unchanged-model"}, "custom": {"keep": "value"}},
                        merge_existing=True)
    monkeypatch.setattr("hermes_cli.browser_connect.is_browser_debug_ready", lambda _url, timeout: True)
    adapter = _Adapter(str(root))
    adapter.gateway_runner._resolve_profile_home_for_source = lambda source: homes[source.profile]
    ui = TelegramStandaloneUI(adapter)
    before = os.environ.get("BROWSER_CDP_URL")
    assert await ui.handle_command(_source(profile="b"), "browser", "connect localhost:9333")
    with _profile_runtime_scope(root):
        assert get_cdp_override_raw() == ""
    with _profile_runtime_scope(secondary):
        assert get_cdp_override_raw() == "http://localhost:9333"
        effective = load_user_config_effective(fail_closed=True)
        assert effective["browser"]["backend"] == "local"
        assert effective["browser"]["command_timeout"] == 19
        assert effective["model"]["default"] == "unchanged-model"
        assert effective["custom"]["keep"] == "value"
    with _profile_runtime_scope(root):
        assert get_cdp_override_raw() == ""
    assert os.environ.get("BROWSER_CDP_URL") == before
    assert "http://localhost:9333" not in "".join(text for _, text, _ in adapter.sent)
    assert "http://localhost:9333" not in "".join(text for _, _, text, _ in adapter.edited)


def test_dashboard_launch_uses_isolated_profile_scoped_child_env_and_loopback(tmp_path, monkeypatch):
    import os
    from unittest.mock import Mock
    from gateway.run import _profile_runtime_scope
    root = tmp_path / "home"
    home = root / "profiles" / "b"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("dashboard:\n  theme: default\n")
    (home / ".env").write_text("OPENAI_API_KEY=profile-b-test-value\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("OPENAI_API_KEY", "launch-profile-test-value")
    ui = TelegramStandaloneUI(_Adapter(str(home)))
    records = [[], [(123, "dashboard", 9119, "127.0.0.1")]]
    monkeypatch.setattr(ui, "_dashboard_status", Mock(side_effect=records))
    monkeypatch.setattr("hermes_cli.main_dashboard._dashboard_listening", Mock(side_effect=[False, True]))
    popen = Mock(return_value=SimpleNamespace(pid=123, poll=lambda: None))
    monkeypatch.setattr(standalone_ui.subprocess, "Popen", popen)
    with _profile_runtime_scope(home):
        result = ui._dashboard_start({"home": str(home)})
    assert "isolated WebUI" in result
    argv = popen.call_args.args[0]
    assert argv[argv.index("-p") + 1] == "b"
    assert "--isolated" in argv and "--no-open" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--insecure" not in argv
    env = popen.call_args.kwargs["env"]
    assert env["HERMES_HOME"] == str(home)
    assert env["OPENAI_API_KEY"] == "profile-b-test-value"
    assert os.environ["OPENAI_API_KEY"] == "launch-profile-test-value"


def test_dashboard_start_does_not_spawn_when_another_service_holds_port(tmp_path, monkeypatch):
    from unittest.mock import Mock
    ui = TelegramStandaloneUI(_Adapter(str(tmp_path)))
    monkeypatch.setattr(ui, "_dashboard_status", lambda _home: [])
    monkeypatch.setattr("hermes_cli.main_dashboard._dashboard_listening", lambda _host, _port: True)
    spawn = Mock()
    monkeypatch.setattr(standalone_ui.subprocess, "Popen", spawn)
    assert "left unchanged" in ui._dashboard_start({"home": str(tmp_path)})
    spawn.assert_not_called()


def test_dashboard_stop_uses_existing_lifecycle_with_only_owning_home(tmp_path, monkeypatch):
    from unittest.mock import Mock
    stop = Mock(return_value={"killed": [123], "failed": []})
    monkeypatch.setattr("hermes_cli.dashboard_procs._kill_stale_dashboard_processes", stop)
    ui = TelegramStandaloneUI(_Adapter(str(tmp_path)))
    assert "Stopped 1" in ui._dashboard_stop({"home": str(tmp_path)})
    assert stop.call_args.kwargs["scope_home"] == str(tmp_path)


@pytest.mark.asyncio
async def test_dashboard_auth_command_and_button_delegate_to_native_auth_ui(tmp_path):
    from unittest.mock import AsyncMock
    adapter = _Adapter(str(tmp_path))
    adapter._dispatch_control_command = AsyncMock()
    ui = TelegramStandaloneUI(adapter)
    source = _source()
    assert await ui.handle_command(source, "webui", "auth", reply_to=12)
    adapter._dispatch_control_command.assert_awaited_once_with(source, "webauth", reply_to=12)
    adapter._dispatch_control_command.reset_mock()
    assert await ui.handle_command(source, "dashboard", "")
    payload = _button_data(adapter.sent[-1][2], "Authentication")
    await ui.handle_callback(_Event(), source, payload)
    adapter._dispatch_control_command.assert_awaited_once()
    assert adapter._dispatch_control_command.await_args.args[1] == "webauth"
    assert adapter._dispatch_control_command.await_args.kwargs["reply_to"] == "77"
    # Dashboard remains usable with a fresh, message-bound keyboard after opening auth.
    fresh_payload = _button_data(adapter.edited[-1][3], "Authentication")
    assert fresh_payload != payload


def test_isolated_webui_child_uses_only_owning_profile_auth_environment(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from gateway.run import _profile_runtime_scope
    from agent import secret_scope
    launch = tmp_path / "launch"
    secondary = launch / "profiles" / "b"
    secondary.mkdir(parents=True)
    (secondary / ".env").write_text(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=profile-b\n"
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=profile-b-hash\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "launch-user")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "launch-password-not-a-live-secret")
    monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "launch-signing-secret-not-real")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    ui = TelegramStandaloneUI(_Adapter(str(launch)))
    monkeypatch.setattr(ui, "_dashboard_status", Mock(side_effect=[[], [(8471, "dashboard", 9119, "127.0.0.1")]]))
    monkeypatch.setattr("hermes_cli.main_dashboard._dashboard_listening", Mock(side_effect=[False, True]))
    spawn = Mock(return_value=SimpleNamespace(pid=8471, poll=lambda: None))
    monkeypatch.setattr("plugins.platforms.telegram.standalone_ui.subprocess.Popen", spawn)
    with _profile_runtime_scope(secondary):
        assert "isolated WebUI" in ui._dashboard_start({"home": str(secondary)})
    env = spawn.call_args.kwargs["env"]
    assert env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] == "profile-b"
    assert env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"] == "profile-b-hash"
    assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" not in env
    assert "HERMES_DASHBOARD_BASIC_AUTH_SECRET" not in env
    assert os.environ["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] == "launch-user"
    assert os.environ["HERMES_HOME"] == str(launch)
