"""Personal state remains native and isolated when Mongo is backup-only."""
from __future__ import annotations

import json
import pytest

from agent.local_tenants import LocalTenantStore
from gateway import mongo_multiuser
from gateway.telegram_personal_settings import load_personal_settings, save_personal_settings
from plugins.platforms.telegram.telegram_user_settings_store import load_user_settings, save_user_settings


@pytest.fixture
def tenant_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nMULTIUSER_ENABLED=true\nMONGODB_URI=mongodb://never-contact.invalid/hermes\n")
    monkeypatch.setattr("pymongo.MongoClient.__init__", lambda *a, **kw: pytest.fail("Ordinary personal state contacted Mongo"))
    store = LocalTenantStore(tmp_path, "100", allowed_user_ids=["200"])
    store.ensure_owner()
    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: store)
    return store


def test_personal_settings_native_authoritative_and_isolated(tmp_path, tenant_store):
    for uid, text in (("100", "owner local"), ("200", "user local")):
        path = tmp_path / "personal" / "telegram" / "users" / uid / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"general_instructions": text}))
    assert load_personal_settings(tmp_path, "200")["general_instructions"] == "user local"
    saved = save_personal_settings(tmp_path, "200", {"general_instructions": "updated locally"})
    assert saved["general_instructions"] == "updated locally"
    assert load_personal_settings(tmp_path, "200")["general_instructions"] == "updated locally"
    assert load_personal_settings(tmp_path, "100")["general_instructions"] == "owner local"
    assert tenant_store.get(mongo_multiuser.principal("200"), "settings", "personal") is None


def test_backup_unavailability_cannot_block_personal_settings(tmp_path, monkeypatch, tenant_store):
    monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: (_ for _ in ()).throw(RuntimeError("backup down")))
    path = tmp_path / "personal" / "telegram" / "users" / "200" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"general_instructions":"local canonical"}')
    assert load_personal_settings(tmp_path, "200")["general_instructions"] == "local canonical"
    save_personal_settings(tmp_path, "200", {"general_instructions": "durable despite outage"})
    assert json.loads(path.read_text())["general_instructions"] == "durable despite outage"


def test_telegram_native_settings_keep_owner_only_provider_policy(tmp_path, tenant_store):
    assert load_user_settings(tmp_path, "200")["instructions"]["general"] == ""
    with pytest.raises(ValueError, match="provider and custom MCP"):
        save_user_settings(tmp_path, "200", {"default_provider": "user-provider"})
    with pytest.raises(ValueError, match="provider and custom MCP"):
        save_user_settings(tmp_path, "200", {"mcp_servers": ["personal-server"]})
    result = save_user_settings(tmp_path, "200", {"instructions": {"general": "private"}})
    assert result["instructions"]["general"] == "private"
    assert load_user_settings(tmp_path, "100")["instructions"]["general"] == ""
    assert (tmp_path / "telegram_user_settings.sqlite3").exists()
    assert tenant_store.get(mongo_multiuser.principal("200"), "settings", "telegram") is None


def test_native_memory_roundtrip_and_no_owner_fallthrough(tmp_path, tenant_store):
    from gateway.telegram_personal_settings import set_personal_memory_context, reset_personal_memory_context, current_personal_memory_dir
    from tools.memory_tool_store import MemoryStore
    from agent.mongo_tenants import TenantStoreError
    owner_memory = tmp_path / "memories" / "MEMORY.md"
    owner_memory.parent.mkdir()
    owner_memory.write_text("owner secret memory")
    token = set_personal_memory_context(tmp_path, "200")
    try:
        path = current_personal_memory_dir() / "MEMORY.md"
        path.parent.mkdir(parents=True)
        path.write_text("existing user memory")
        assert MemoryStore._read_raw_checked(path) == ("existing user memory", True)
        MemoryStore._write_file(path, ["user-only memory"])
        assert MemoryStore._read_raw_checked(path) == ("user-only memory", True)
        assert tenant_store.get(mongo_multiuser.principal("200"), "memory", "MEMORY.md") is None
        with pytest.raises(TenantStoreError, match="outside"):
            MemoryStore._read_raw_checked(owner_memory)
        with pytest.raises(TenantStoreError, match="outside"):
            MemoryStore._write_file(owner_memory, ["attack"])
        assert owner_memory.read_text() == "owner secret memory"
        path.unlink()
        path.symlink_to(owner_memory)
        with pytest.raises(TenantStoreError, match="outside"):
            MemoryStore._read_raw_checked(path)
    finally:
        reset_personal_memory_context(token)


def test_backup_outage_does_not_block_native_memory_save(tmp_path, tenant_store, monkeypatch):
    from gateway.telegram_personal_settings import set_personal_memory_context, reset_personal_memory_context, current_personal_memory_dir
    from tools.memory_tool_store import MemoryStore
    token = set_personal_memory_context(tmp_path, "200")
    try:
        path = current_personal_memory_dir() / "USER.md"
        path.parent.mkdir(parents=True)
        path.write_text("existing profile")
        monkeypatch.setattr(mongo_multiuser, "store_for_home", lambda _home: (_ for _ in ()).throw(RuntimeError("backup unavailable")))
        MemoryStore._write_file(path, ["new value"])
        assert path.read_text() == "new value"
        assert MemoryStore._read_raw_checked(path) == ("new value", True)
    finally:
        reset_personal_memory_context(token)
