"""OAuth adds from a group must save to the global provider store.

Sync means sync: a Codex OAuth sub added in a group lands in the launch
home's credential store, not the group profile's. Minimal local fakes
(mirroring test_provider_management_ui.py) so this file stands alone.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

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
    profile: str | None = None


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
        "extra_headers": {"Authorization": "Bearer <redacted>"},
    }
    row.update(updates)
    return row


def _state(ui):
    return next(iter(ui._states.values()))


@pytest.mark.asyncio
async def test_oauth_add_from_group_runs_under_global_store_scope(tmp_path, monkeypatch):
    """A Codex OAuth sub added in a group must save to the launch-home store."""
    import gateway.run as gateway_run
    from hermes_cli import auth as auth_mod
    from hermes_cli import auth_commands

    launch = tmp_path / "launch"
    gc = launch / "profiles" / "gc-999"
    launch.mkdir()
    gc.mkdir(parents=True)
    (launch / ".env").write_text("OWNER_TELEGRAM_ID=111\n", encoding="utf-8")
    (launch / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    (gc / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(gateway_run, "_hermes_home", launch)

    seen_auth_dirs = []

    def fake_auth_add(args):
        seen_auth_dirs.append(auth_mod._auth_file_path().parent)
        args.on_authorize_url("https://login.example.test/authorize")
        args.input_fn("authorization code")

    monkeypatch.setattr(auth_commands, "auth_add_command", fake_auth_add)
    provider = _provider(slug="openai-codex", name="OpenAI Codex", auth_type="oauth", api_key_env_vars=["OPENAI_API_KEY"])
    adapter = FakeAdapter()
    ui = TelegramProviderManagementUI(adapter)
    source = Source()
    await ui.open(source, [provider], current_model="codex-model", current_provider="openai-codex",
                  session_key="s", profile_home=gc)
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
    assert await ui.handle_message(IncomingEvent(93), Source(message_id="93"), "approved-code")
    for _ in range(50):
        if state["view"] == "provider":
            break
        await asyncio.sleep(0.01)
    assert state["view"] == "provider"
    assert seen_auth_dirs == [launch]
