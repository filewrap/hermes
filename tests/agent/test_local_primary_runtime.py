"""Local serving paths must never connect to, restore from, or publish to Mongo."""
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "MONGODB_URI=mongodb://never-contact.invalid/hermes\n"
        "MULTIUSER_ENABLED=true\nOWNER_TELEGRAM_ID=100\n"
        "TELEGRAM_ALLOWED_USERS=100,200\nAPI_KEY=local-provider-value\n"
        "TELEGRAM_USER_SETTINGS_MONGODB_URI=mongodb://also-never.invalid\n"
    )
    def forbidden(*args, **kwargs):
        pytest.fail("Ordinary local serving attempted to access Mongo")
    monkeypatch.setattr("agent.mongo_credentials._settings", forbidden)
    monkeypatch.setattr("agent.mongo_credentials._collection", forbidden)
    monkeypatch.setattr("agent.mongo_tenants.MongoTenantStore.__init__", forbidden)
    monkeypatch.setattr("pymongo.MongoClient.__init__", forbidden)
    return tmp_path


def test_uri_and_old_rollout_cannot_enable_remote_authority(home):
    from agent.storage_mode import mongo_primary_enabled
    from agent.mongo_credentials import enabled
    from gateway.mongo_multiuser import enabled as isolated, runtime_ready
    assert mongo_primary_enabled(home) is False
    assert enabled(home) is False
    assert isolated(home) and runtime_ready(home)


def test_config_guard_and_atomic_write_stay_native(home):
    from gateway.mongo_runtime import ensure_authoritative_config, maybe_persist_atomic, write_active_owner_file
    from utils import atomic_write_text
    config = home / "config.yaml"
    config.write_text("model: selected-locally\n")
    ensure_authoritative_config(home)
    assert config.read_text() == "model: selected-locally\n"
    assert write_active_owner_file(config, b"remote-copy") is None
    assert maybe_persist_atomic(config, lambda stream: stream.write("unused")) is False
    atomic_write_text(config, "model: user-selected\n")
    assert config.read_text() == "model: user-selected\n"
    assert not (home / "tenant-state.db").exists()


def test_native_sql_commit_and_reopen(home):
    from agent.mongo_sql_cache import connect
    db = home / "state.db"
    with connect(db) as conn:
        assert type(conn) is sqlite3.Connection
        conn.execute("CREATE TABLE custom(value TEXT)")
        conn.execute("INSERT INTO custom VALUES ('durable')")
    with connect(db) as conn:
        assert conn.execute("SELECT value FROM custom").fetchone() == ("durable",)


def test_local_sql_mode_keeps_owner_database_boundary(home):
    from agent.mongo_sql_cache import connect
    from agent.tenant_scope import TenantScope, set_tenant_scope, reset_tenant_scope
    from agent.mongo_tenants import Principal
    cache = home / ".tenant-cache" / "200"
    cache.mkdir(parents=True)
    token = set_tenant_scope(TenantScope(home, Principal("200"), "room", cache, "user:200"))
    try:
        with pytest.raises(PermissionError, match="owner SQL"):
            connect(home / "state.db")
        with connect(cache / "state.db") as conn:
            conn.execute("CREATE TABLE own(value TEXT)")
    finally:
        reset_tenant_scope(token)


def test_allowlist_bootstrap_preserves_access_but_never_reverses_a_ban(home):
    from gateway.mongo_multiuser import store_for_home
    from agent.mongo_tenants import Principal
    store = store_for_home(home)
    owner, user = Principal("100"), Principal("200")
    assert store.get_access("200")["allowed"]
    store.put(owner, "profile", "private", {"owner": "only"})
    assert store.get(user, "profile", "private") is None
    store.ban_user(owner, "200", True)
    reopened = store_for_home(home)
    assert reopened.get_access("200")["banned"]
    with pytest.raises(Exception):
        reopened.get(user, "profile", "private")
    assert not reopened.get_access("300")["allowed"]


def test_local_accounting_is_idempotent_and_tenant_isolated(home):
    from agent.mongo_accounting import account_store
    from agent.mongo_tenants import Principal
    store = account_store(home)
    owner, user = Principal("100"), Principal("200")
    assert store.readycheck() == {"ready": True, "authority": "sqlite"}
    assert store.record_turn(user, "session", "event", {"input_tokens": 7})
    assert not store.record_turn(user, "session", "event", {"input_tokens": 7})
    assert account_store(home).totals(user)["input_tokens"] == 7
    assert store.totals(owner)["input_tokens"] == 0
    store.owner_grant(owner, "200", credits=3, weight=2)
    assert account_store(home).balance(user) == {"balance": 3, "weight": 2}
    with pytest.raises(Exception):
        store.owner_grant(user, "100", credits=5)


def test_owner_memory_reads_and_saves_native_file(home):
    from tools.memory_tool_store import MemoryStore
    path = home / "memories" / "MEMORY.md"
    path.parent.mkdir()
    path.write_text("existing user-added memory")
    assert MemoryStore._read_raw_checked(path) == ("existing user-added memory", True)
    MemoryStore._write_file(path, ["updated memory"])
    assert path.read_text() == "updated memory"
    assert not (home / "tenant-state.db").exists()


def test_personal_memory_scope_still_blocks_owner_memory(home, monkeypatch):
    from tools.memory_tool_store import MemoryStore
    directory = home / "personal" / "telegram" / "users" / "200" / "memory"
    directory.mkdir(parents=True)
    context = {"home": str(home), "user_id": "200", "memory_dir": str(directory)}
    monkeypatch.setattr("gateway.telegram_personal_settings.current_personal_memory_context", lambda: context)
    owner_path = home / "memories" / "MEMORY.md"
    owner_path.parent.mkdir()
    owner_path.write_text("owner private memory")
    with pytest.raises(Exception, match="outside"):
        MemoryStore._read_raw_checked(owner_path)
    with pytest.raises(Exception, match="outside"):
        MemoryStore._write_file(owner_path, ["attack"])
    assert owner_path.read_text() == "owner private memory"


def test_provider_selection_and_credentials_are_not_replaced_from_backup(home):
    import json
    from hermes_cli.auth import _load_auth_store
    path = home / "auth.json"
    value = {"providers": {"openrouter": {"api_key": "local-secret"}}, "version": 1}
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    loaded = _load_auth_store(path)
    assert loaded.get("providers", {}).get("openrouter", {}).get("api_key") == "local-secret"
    assert path.read_bytes() == before


def test_environment_save_hook_does_not_create_a_second_source(home):
    from gateway.mongo_environment import save_environment_setting
    before = (home / ".env").read_bytes()
    assert save_environment_setting(home, "CUSTOM_PREFERENCE", "yes") is False
    assert (home / ".env").read_bytes() == before
    assert not (home / "tenant-state.db").exists()


def test_local_telegram_settings_never_use_independent_mongo_uri(home, monkeypatch):
    from plugins.platforms.telegram import telegram_user_settings_store as settings
    monkeypatch.setattr("gateway.platforms._shared.get_scoped_secret", lambda *a, **k: pytest.fail("Mongo setting secret requested"))
    settings.save_user_settings(home, "200", {"default_model": "user-model"})
    assert settings.load_user_settings(home, "200")["default_model"] == "user-model"
    assert settings.load_user_settings(home, "300")["default_model"] != "user-model"


def test_sync_reports_local_mode_without_touching_storage(home, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from agent import mongo_credentials
    from gateway import mongo_sync
    from gateway.slash_commands import GatewaySlashCommandsMixin

    monkeypatch.setattr(
        mongo_sync, "sync_profile", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("sync must not run"))
    )
    # No admin list configured: the admin gate denies before any storage is touched.
    runner = SimpleNamespace(
        config={},
        _resolve_profile_home_for_source=lambda source: home,
    )
    event = SimpleNamespace(
        source=SimpleNamespace(user_id="100", platform="telegram", chat_type="private", profile="default")
    )
    result = asyncio.run(GatewaySlashCommandsMixin._handle_sync_command(runner, event))
    assert "Sync is owner/admin-only" in result and "No data was written." in result
    # An admin whose credentials are not enabled gets the not-configured reply, still
    # without touching storage.
    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: False)
    runner.config = SimpleNamespace(platforms={"telegram": {"allow_admin_from": ["100"]}})
    result = asyncio.run(GatewaySlashCommandsMixin._handle_sync_command(runner, event))
    assert "Sync is not configured yet" in result
    assert not (home / "tenant-state.db").exists()


def test_personal_settings_cannot_alias_another_user(home):
    from gateway.telegram_personal_settings import load_personal_settings
    users = home / "personal" / "telegram" / "users"
    (users / "100").mkdir(parents=True)
    (users / "100" / "settings.json").write_text('{"general_instructions":"owner-private"}')
    (users / "200").symlink_to(users / "100", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        load_personal_settings(home, "200")
    assert "owner-private" in (users / "100" / "settings.json").read_text()


def test_personal_memory_cannot_alias_owner_memory(home):
    from gateway.telegram_personal_settings import personal_settings_path, set_personal_memory_context
    owner = home / "memories"
    owner.mkdir()
    (owner / "MEMORY.md").write_text("owner-private")
    directory = personal_settings_path(home, "200").parent
    directory.mkdir(parents=True)
    (directory / "memory").symlink_to(owner, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        set_personal_memory_context(home, "200")
    assert (owner / "MEMORY.md").read_text() == "owner-private"
