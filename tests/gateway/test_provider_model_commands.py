from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command


@pytest.mark.parametrize("name", ["models", "providers", "mcp"])
def test_provider_catalog_commands_are_registered_independently(name):
    command = resolve_command(name)
    assert command is not None
    assert command.name == name
    assert name in GATEWAY_KNOWN_COMMANDS


def test_model_and_models_are_not_aliases():
    model = resolve_command("model")
    models = resolve_command("models")
    assert model is not None and models is not None
    assert model.name == "model"
    assert models.name == "models"
    assert "models" not in model.aliases


def test_idle_dispatch_has_separate_provider_and_catalog_handlers():
    runner = object.__new__(GatewayRunner)
    handlers = runner._gateway_idle_command_handlers()
    assert callable(handlers["providers"])
    assert callable(handlers["models"])
    assert callable(handlers["mcp"])
    assert handlers["providers"].__name__ == "_handle_providers_command"
    assert handlers["models"].__name__ == "_handle_models_command"
    assert handlers["mcp"].__name__ == "_handle_mcp_command"
    assert handlers["model"].__name__ == "_handle_model_command"


class _ProviderCardAdapter:
    def __init__(self):
        self.kwargs = None

    async def send_provider_management(self, **kwargs):
        self.kwargs = kwargs
        return True


def _event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="123", chat_type="dm"),
    )


@pytest.mark.asyncio
async def test_models_and_providers_open_distinct_views_without_switching(monkeypatch):
    runner = object.__new__(GatewayRunner)
    adapter = _ProviderCardAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    snapshot = [
        [{"slug": "openrouter", "name": "OpenRouter", "configured": True,
          "is_current": True, "models": ["vendor/model-a"], "total_models": 1}],
        "model-a", "openrouter", "session-1", "/tmp/profile",
    ]
    runner._provider_inventory_snapshot = AsyncMock(return_value=tuple(snapshot))
    runner._reply_anchor_for_event = lambda _event: None

    models_result = await runner._handle_models_command(_event("/models"))
    assert models_result is None
    assert adapter.kwargs["view"] == "models"
    assert adapter.kwargs["current_model"] == "model-a"

    providers_result = await runner._handle_providers_command(_event("/providers"))
    assert providers_result is None
    assert adapter.kwargs["view"] == "providers"
    assert adapter.kwargs["current_provider"] == "openrouter"
    assert runner._provider_inventory_snapshot.await_count == 2


@pytest.mark.asyncio
async def test_mcp_command_opens_native_profile_manager_without_plain_text_fallback():
    runner = object.__new__(GatewayRunner)

    class Adapter:
        def __init__(self):
            self.kwargs = None

        async def send_mcp_management(self, **kwargs):
            self.kwargs = kwargs
            return True

    adapter = Adapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._reply_anchor_for_event = lambda _event: 42

    result = await runner._handle_mcp_command(_event("/mcp"))

    assert result is None
    assert adapter.kwargs["source"].user_id == "123"
    from gateway.run import _hermes_home
    assert adapter.kwargs["profile_home"] == _hermes_home
    assert adapter.kwargs["reply_to"] == 42


def test_telegram_menu_exposes_both_commands_separately():
    from hermes_cli.commands_platforms import telegram_bot_commands

    names = {name for name, _description in telegram_bot_commands()}
    assert {"model", "models", "providers", "mcp"} <= names


@pytest.mark.asyncio
async def test_inventory_is_profile_scoped_cache_first_and_secret_free(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    from hermes_cli.auth import is_provider_explicitly_configured

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: ramp-router\n"
        "providers:\n  custom-test:\n    name: Custom Test\n"
        "    base_url: https://example.invalid/v1\n"
        "    api_key: NEVER_SHOW_THIS_KEY\n    models: [custom-a]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda _slug: False)
    observed = {}

    def fake_list(**kwargs):
        observed.update(kwargs)
        return [{
            "slug": "router", "name": "Ramp Router", "is_current": True,
            "models": ["vendor/model-a"], "total_models": 1,
            "api_key": "NEVER_SHOW_THIS_KEY",
        }]

    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", fake_list)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"

    providers, current_model, current_provider, session_key, profile_home = \
        await runner._provider_inventory_snapshot(_event("/providers").source)

    from hermes_cli.provider_catalog import provider_catalog
    rows_by_slug = {row["slug"]: row for row in providers}
    for descriptor in provider_catalog():
        row = rows_by_slug[descriptor.slug]
        assert row["is_catalog_provider"] is True
        assert row["auth_type"] == descriptor.auth_type
        assert row["api_key_env_vars"] == list(descriptor.api_key_env_vars)

    assert current_model == "model-a"
    assert current_provider == "router"
    assert session_key == "session-1"
    assert profile_home == home
    assert observed["refresh"] is False
    assert observed["probe_custom_providers"] is False
    assert observed["probe_current_custom_provider"] is False
    assert observed["non_blocking_catalogs"] is True
    assert rows_by_slug["router"]["catalog_status"] == "cached_or_curated"
    assert any(row["slug"] == "custom-test" for row in providers)
    assert "NEVER_SHOW_THIS_KEY" not in repr(providers)


@pytest.mark.asyncio
async def test_inventory_keeps_provider_default_separate_from_session_override(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: configured-default\n  provider: openrouter\n"
        "agent:\n  reasoning_overrides:\n    configured-default: high\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **_kwargs: [])

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {
        "session-1": {"provider": "openrouter", "model": "session-only-model"},
    }
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"

    providers, current_model, current_provider, *_ = await runner._provider_inventory_snapshot(
        _event("/providers").source,
    )
    openrouter = next(row for row in providers if row["slug"] == "openrouter")
    assert current_model == "session-only-model"
    assert current_provider == "openrouter"
    assert openrouter["default_model"] == "configured-default"
    assert openrouter["is_profile_default"] is True
    assert openrouter["profile_default_model"] == "configured-default"
    assert openrouter["default_reasoning"] == "high"


@pytest.mark.asyncio
async def test_inventory_current_provider_without_credentials_is_not_configured(tmp_path, monkeypatch):
    """A config.yaml model.provider pointer is not a credential.

    With no authenticated providers, the current provider (still selected in
    config.yaml and in the session override) must render as not configured —
    before this fix the snapshot forced configured=True for the current
    provider and OR-ed in is_provider_explicitly_configured, which fires on
    the config pointer alone, so a removed provider kept showing as added.
    """
    import gateway.run as gateway_run

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: configured-default\n  provider: openrouter\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **_kwargs: [])

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {
        "session-1": {"provider": "openrouter", "model": "session-only-model"},
    }
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"

    providers, current_model, current_provider, *_ = await runner._provider_inventory_snapshot(
        _event("/providers").source,
    )
    openrouter = next(row for row in providers if row["slug"] == "openrouter")
    assert current_model == "session-only-model"
    assert current_provider == "openrouter"
    assert openrouter["is_current"] is True
    assert openrouter["configured"] is False
    assert openrouter["catalog_status"] == "not_configured"


@pytest.mark.asyncio
async def test_inventory_hides_providers_added_by_another_user(tmp_path, monkeypatch):
    """Per-user sharing: a provider added by one user is invisible to the others
    in /model and /providers, and visible to the adder."""
    import gateway.run as gateway_run
    from agent.credential_pool import PooledCredential, load_pool
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="k", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra["added_by"] = "222"
    pool.add_entry(entry)
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [{
            "slug": "openrouter", "name": "OpenRouter", "is_current": True,
            "models": ["vendor/model-a"], "total_models": 1,
        }],
    )

    def event_for(user_id):
        return MessageEvent(
            text="/providers", message_type=MessageType.COMMAND,
            source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id=user_id, chat_type="dm"),
        )

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"

    providers, *_ = await runner._provider_inventory_snapshot(event_for("111").source)
    assert [row["slug"] for row in providers if row["slug"] == "openrouter"] == []

    providers, *_ = await runner._provider_inventory_snapshot(event_for("222").source)
    assert any(row["slug"] == "openrouter" for row in providers)


@pytest.mark.asyncio
async def test_provider_inventory_profile_scope_survives_a_b_a_switches(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    homes = {name: tmp_path / f"profile-{name}" for name in ("a", "b")}
    for name, home in homes.items():
        home.mkdir()
        (home / "config.yaml").write_text(
            f"model:\n  default: model-{name}\n  provider: openrouter\n"
            f"providers:\n  endpoint-{name}:\n    name: Endpoint {name.upper()}\n"
            f"    base_url: https://{name}.example.invalid/v1\n"
            f"    api_key: PROFILE_{name.upper()}_SECRET\n"
            f"    default_model: endpoint-model-{name}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(gateway_run, "_hermes_home", homes["a"])
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **_kwargs: [])

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = lambda source: homes[source.chat_id]
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda source: f"session-{source.chat_id}"

    for name in ("a", "b", "a"):
        source = SessionSource(
            platform=Platform.TELEGRAM, chat_id=name, user_id=name, chat_type="dm",
        )
        providers, current_model, _current_provider, _session_key, profile_home = \
            await runner._provider_inventory_snapshot(source)
        custom = next(row for row in providers if row["slug"] == f"endpoint-{name}")
        assert current_model == f"model-{name}"
        assert custom["name"] == f"Endpoint {name.upper()}"
        assert custom["default_model"] == f"endpoint-model-{name}"
        assert profile_home == homes[name]
        assert f"PROFILE_{name.upper()}_SECRET" not in repr(providers)


@pytest.mark.asyncio
async def test_model_parse_error_reply_has_no_emoji(tmp_path, monkeypatch):
    """A /model argument error is part of the /model screen: no emoji prefix."""
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    event = MessageEvent(
        text="/model --once --global",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )
    reply = await runner._handle_model_command_locked(event)
    assert reply is not None
    assert "/model --once cannot be combined with --global" in reply
    assert "❌" not in reply


@pytest.mark.asyncio
async def test_model_switch_confirmation_has_no_emoji(tmp_path, monkeypatch):
    """The /model switch confirmation keeps the usage notice text, without the
    warning-sign emoji prefix."""
    from gateway.slash_commands_model import _ModelSwitchContext
    from hermes_cli.model_switch import ModelSwitchResult

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    result = ModelSwitchResult(
        success=True, new_model="vendor/model-a", target_provider="anthropic",
        provider_label="Anthropic", usage_notice="Large model: check pricing.",
    )
    ctx = _ModelSwitchContext(
        session_key="s", source=SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        config_path=tmp_path / "config.yaml", persist_global=False,
        current_model="old", current_provider="nous",
    )
    reply = await runner._model_switch_confirmation(
        result, ctx, one_turn=False, picker=False)
    assert "Large model: check pricing." in reply
    assert "⚠️" not in reply and "⚠" not in reply


def _multiuser_home(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  default: model-a\n  provider: openrouter\n", encoding="utf-8",
    )
    (home / ".env").write_text("OWNER_TELEGRAM_ID=222\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    return home


@pytest.mark.asyncio
async def test_model_global_switch_refused_for_allowed_non_owner(tmp_path, monkeypatch):
    """Part 2: ``/model --global`` stays owner-only — an allowed user is
    refused before any switch runs, and is told to switch session-only."""
    from unittest.mock import AsyncMock

    _multiuser_home(tmp_path, monkeypatch)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"
    runner._perform_model_switch = AsyncMock(return_value=(object(), None))
    runner._model_selection_guard_reply = AsyncMock(return_value=(False, None))
    runner._commit_model_switch_locked = AsyncMock(return_value="switched")
    event = MessageEvent(
        text="/model vendor/model-b --global", message_type=MessageType.COMMAND,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="999", chat_type="dm"),
    )
    reply = await runner._handle_model_command_locked(event)
    assert reply is not None
    assert "Only the profile owner" in reply
    assert "session" in reply.casefold()
    runner._perform_model_switch.assert_not_awaited()
    runner._commit_model_switch_locked.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_global_switch_allowed_for_owner(tmp_path, monkeypatch):
    """Part 2 guard: the owner keeps the ``--global`` switch path."""
    from unittest.mock import AsyncMock

    _multiuser_home(tmp_path, monkeypatch)
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._session_model_overrides = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda _source: "session-1"
    runner._perform_model_switch = AsyncMock(
        return_value=(SimpleNamespace(success=True, target_provider="openrouter"), None))
    runner._model_selection_guard_reply = AsyncMock(return_value=(False, None))
    runner._commit_model_switch_locked = AsyncMock(return_value="switched")
    event = MessageEvent(
        text="/model vendor/model-b --global", message_type=MessageType.COMMAND,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="222", chat_type="dm"),
    )
    reply = await runner._handle_model_command_locked(event)
    assert reply == "switched"
    runner._perform_model_switch.assert_awaited_once()


@pytest.mark.asyncio
async def test_model_picker_hides_providers_added_by_another_user(tmp_path, monkeypatch):
    """Part 2: the /model picker only offers providers the acting user may
    use — a provider added by another user and not shared is not offered."""
    from agent.credential_pool import PooledCredential, load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pool = load_pool("openrouter")
    entry = PooledCredential(
        provider="openrouter", id="abc123", label="k", auth_type="api_key",
        priority=0, source="manual", access_token="tok",
    )
    entry.extra["added_by"] = "222"
    pool.add_entry(entry)
    monkeypatch.setattr(
        "hermes_cli.model_switch_providers.list_picker_providers",
        lambda **_kwargs: [
            {"slug": "openrouter", "name": "OpenRouter", "models": ["vendor/model-a"], "total_models": 1},
            {"slug": "anthropic", "name": "Anthropic", "models": ["claude-x"], "total_models": 1},
        ],
    )
    runner = object.__new__(GatewayRunner)
    runner._thread_metadata_for_source = lambda _source, _anchor: {}
    runner._reply_anchor_for_event = lambda _event: None

    class _PickerAdapter:
        def __init__(self):
            self.kwargs = None

        async def send_model_picker(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(success=True)

    adapter = _PickerAdapter()
    event = MessageEvent(
        text="/model", message_type=MessageType.COMMAND,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="111", chat_type="dm"),
    )
    result = await runner._send_model_picker(
        event, event.source, adapter, "session-1",
        {"current_model": "model-a", "current_provider": "openrouter"},
        lambda *_args: "ok",
    )
    assert result is True
    assert [row["slug"] for row in adapter.kwargs["providers"]] == ["anthropic"]
