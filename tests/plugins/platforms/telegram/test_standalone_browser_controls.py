from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from plugins.platforms.telegram.standalone_ui import TelegramStandaloneUI


class _Adapter:
    def __init__(self, homes):
        self.homes = homes
        self.gateway_runner = SimpleNamespace(_resolve_profile_home_for_source=self._resolve_profile_home_for_source)
        self.sent = []
        self.edited = []
        self.pushed = []

    def _resolve_profile_home_for_source(self, source):
        return str(self.homes[source.profile])

    def _is_owner_user(self, user_id, _source=None):
        return str(user_id) == "100"

    def _authorized(self, _source):
        return True

    def _push_control_frame(self, source, message_id, frame):
        self.pushed.append((source, message_id, frame))
        return True

    async def _send_rich_ui(self, _source, text, markup=None, reply_to=None):
        self.sent.append((text, markup))
        return SimpleNamespace(id=77)

    async def _edit_rich_ui_message(self, _event, _source, text, markup=None):
        self.edited.append((text, markup))
        return SimpleNamespace(id=77)

    @staticmethod
    def _sent_message_id(result):
        return getattr(result, "id", None)


class _Event:
    def __init__(self):
        self.query = SimpleNamespace(message=SimpleNamespace(id=77))
        self.answered = []

    async def answer(self, text=""):
        self.answered.append(text)


def _source(profile="default"):
    return SimpleNamespace(user_id="100", chat_id="100", chat_type="dm", thread_id=None,
                           profile=profile, is_bot=False)


def _payload(markup, label):
    for row in markup.rows:
        for button in row.buttons:
            raw = getattr(button, "button", button)
            if raw.text == label:
                data = getattr(raw, "data", None)
                if data is None:
                    data = raw.type.data
                return data.decode() if isinstance(data, bytes) else data
    raise AssertionError(f"button not found: {label}")


@pytest.mark.asyncio
async def test_profile_browser_selectors_confirm_and_preserve_unrelated_config_a_to_b_to_a(
    tmp_path, monkeypatch,
):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config, save_config
    import agent.browser_registry as registry
    from hermes_cli import plugins

    root = tmp_path / "hermes"
    home_b = root / "profiles" / "b"
    home_b.mkdir(parents=True)
    homes = {"default": root, "b": home_b}
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    for home, engine in ((root, "chrome"), (home_b, "chrome")):
        with _profile_runtime_scope(home):
            save_config({"browser": {"engine": engine, "command_timeout": 19},
                         "model": {"default": "keep-model"}, "custom": {"keep": "value"}},
                        merge_existing=True)

    # The selector consumes the canonical profile registry API; this test provider
    # has no credentials and is only a registered option, never a claimed live session.
    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: None)
    monkeypatch.setattr(registry, "list_providers", lambda: [SimpleNamespace(name="test-cloud")])
    adapter = _Adapter(homes)
    ui = TelegramStandaloneUI(adapter)
    source = _source("b")

    assert await ui.handle_command(source, "browser", "")
    browser_text, browser_markup = adapter.sent[-1]
    assert "Backend selects" in browser_text and "CDP attachment" in browser_text
    assert "Telegram phone or PC is a different machine" in browser_text
    assert "--remote-debugging-port=9222" in browser_text
    await ui.handle_callback(_Event(), source, _payload(browser_markup, "Engine…"))
    engine_text, engine_markup = adapter.edited[-1]
    assert engine_text.count("<i>Page") == 1
    assert engine_text.endswith("<p><i>Page 1 of 1 • total 3</i></p>")
    assert "← Back" not in engine_text
    assert _payload(engine_markup, "← Back")
    await ui.handle_callback(_Event(), source, _payload(engine_markup, "Lightpanda"))
    confirm_text, confirm_markup = adapter.edited[-1]
    assert "Confirm browser change" in confirm_text
    assert "saves config only" in confirm_text
    assert confirm_text.count("<i>Page") == 1
    assert confirm_text.endswith("<p><i>Page 1 of 1 • total 2</i></p>")
    assert "← Back" not in confirm_text
    assert _payload(confirm_markup, "← Back")
    assert adapter.pushed[-1][2] == {"kind": "standalone", "view": "browser-engines"}
    await ui.handle_callback(_Event(), source, _payload(confirm_markup, "Confirm change"))
    assert "saved lightpanda" in adapter.edited[-1][0]

    # Provider options track the current registry and are confirmed like all other settings.
    selector_payload = _payload(adapter.edited[-1][1], "← Back")
    await ui.handle_callback(_Event(), source, selector_payload)
    browser_markup = adapter.edited[-1][1]
    await ui.handle_callback(_Event(), source, _payload(browser_markup, "Provider…"))
    provider_markup = adapter.edited[-1][1]
    assert _payload(provider_markup, "test-cloud")
    await ui.handle_callback(_Event(), source, _payload(provider_markup, "test-cloud"))
    provider_confirm = adapter.edited[-1][1]
    assert "does not create credentials or sign in" in adapter.edited[-1][0]
    await ui.handle_callback(_Event(), source, _payload(provider_confirm, "Confirm change"))

    with _profile_runtime_scope(home_b):
        cfg_b = load_config()
        assert cfg_b["browser"]["engine"] == "lightpanda"
        assert cfg_b["browser"]["cloud_provider"] == "test-cloud"
        assert cfg_b["browser"]["command_timeout"] == 19
        assert cfg_b["model"]["default"] == "keep-model"
        assert cfg_b["custom"]["keep"] == "value"
    with _profile_runtime_scope(root):
        cfg_a = load_config()
        assert cfg_a["browser"]["engine"] == "chrome"
        assert "cloud_provider" not in cfg_a["browser"]
        assert cfg_a["custom"]["keep"] == "value"


@pytest.mark.asyncio
async def test_dashboard_buttons_require_confirm_then_run_only_the_confirmed_action(tmp_path):
    adapter = _Adapter({"default": tmp_path})
    ui = TelegramStandaloneUI(adapter)
    source = _source()
    action = Mock(return_value="confirmed result")
    ui._run_action = action

    assert await ui.handle_command(source, "dashboard", "")
    dashboard_markup = adapter.sent[-1][1]
    await ui.handle_callback(_Event(), source, _payload(dashboard_markup, "Start local WebUI"))
    confirm_text, confirm_markup = adapter.edited[-1]
    assert "Confirm WebUI start" in confirm_text
    assert "only after you confirm" in confirm_text
    action.assert_not_called()
    assert adapter.pushed[-1][2] == {"kind": "standalone", "view": "dashboard"}

    await ui.handle_callback(_Event(), source, _payload(confirm_markup, "← Back"))
    assert "No dashboard action was taken" in adapter.edited[-1][0]
    action.assert_not_called()
    dashboard_markup = adapter.edited[-1][1]
    await ui.handle_callback(_Event(), source, _payload(dashboard_markup, "Stop this profile's WebUI"))
    stop_text, stop_markup = adapter.edited[-1]
    assert "Confirm WebUI stop" in stop_text
    action.assert_not_called()
    await ui.handle_callback(_Event(), source, _payload(stop_markup, "Stop WebUI"))
    action.assert_called_once()
    assert action.call_args.args[0] == "dashboard-stop"
    assert "confirmed result" in adapter.edited[-1][0]


def test_cdp_failure_names_exact_sanitized_endpoint_without_revealing_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr("hermes_cli.browser_connect.is_browser_debug_ready", lambda _url, timeout: False)
    note = TelegramStandaloneUI._browser_connect("127.0.0.1:9333/devtools/browser/test-id")
    assert "http://127.0.0.1:9333/devtools/browser/test-id" in note
    assert "gateway host" in note
    assert "secret" not in note


def test_provider_registry_options_have_no_fabricated_provider(tmp_path, monkeypatch):
    import agent.browser_registry as registry
    from hermes_cli import plugins

    monkeypatch.setattr(plugins, "_ensure_plugins_discovered", lambda: None)
    monkeypatch.setattr(registry, "list_providers", lambda: [SimpleNamespace(name="RegisteredOnly")])
    assert TelegramStandaloneUI._browser_provider_options() == (
        ("auto", "Automatic (runtime default)"),
        ("local", "Local browser (no cloud provider)"),
        ("RegisteredOnly", "RegisteredOnly"),
    )


@pytest.mark.asyncio
async def test_backend_selector_confirms_real_backend_and_auto_restores_unset(tmp_path, monkeypatch):
    from gateway.run import _profile_runtime_scope
    from hermes_cli.config import load_config, read_raw_config, save_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with _profile_runtime_scope(tmp_path):
        save_config({"browser": {"engine": "chrome"}, "custom": {"keep": "yes"}},
                    merge_existing=True)
    adapter = _Adapter({"default": tmp_path})
    ui = TelegramStandaloneUI(adapter)
    source = _source()

    assert await ui.handle_command(source, "browser", "")
    await ui.handle_callback(_Event(), source, _payload(adapter.sent[-1][1], "Backend…"))
    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "Browser Use CLI"))
    assert "hermes tools" in adapter.edited[-1][0]
    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "Confirm change"))
    with _profile_runtime_scope(tmp_path):
        assert load_config()["browser"]["backend"] == "browser-use"
        assert load_config()["custom"]["keep"] == "yes"

    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "← Back"))
    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "Backend…"))
    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "Built-in browser tools"))
    await ui.handle_callback(_Event(), source, _payload(adapter.edited[-1][1], "Confirm change"))
    with _profile_runtime_scope(tmp_path):
        assert load_config()["browser"]["backend"] == "off"
        ui._save_browser_selection({"key": "backend", "value": "auto"})
        raw = read_raw_config()
        assert "backend" not in raw["browser"]
        assert raw["browser"]["engine"] == "chrome"
        assert raw["custom"]["keep"] == "yes"


def test_cdp_connect_reuses_configured_endpoint_without_changing_preferences(monkeypatch):
    from hermes_cli import browser_connect
    config = {"browser": {"backend": "off", "cdp_url": "http://127.0.0.1:9333"}}
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(TelegramStandaloneUI, "_load_config", staticmethod(lambda: config))
    attempts = []
    monkeypatch.setattr(browser_connect, "is_browser_debug_ready",
                        lambda endpoint, timeout: attempts.append(endpoint) or False)
    monkeypatch.setattr(TelegramStandaloneUI, "_save_setting",
                        staticmethod(lambda *_args: pytest.fail("Failed CDP checks must not save config")))
    message = TelegramStandaloneUI._browser_connect(None)
    assert attempts == ["http://127.0.0.1:9333"]
    assert "http://127.0.0.1:9333" in message
    assert "gateway host" in message and "localhost" in message
    assert config["browser"]["backend"] == "off"
