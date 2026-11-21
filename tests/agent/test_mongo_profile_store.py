import os

import mongomock
import pytest

from agent.mongo_profile_store import (
    bootstrap_owner, build_migration_inventory, flush_tenant_cache,
    hydrate_tenant, inventory_profile,
)
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    return MongoTenantStore(tmp_path, "9000", database=mongomock.MongoClient().get_database("hermes"))


def test_inventory_whitelist_and_runtime_exclusions(tmp_path):
    (tmp_path / "config.yaml").write_text("x: 1\n")
    (tmp_path / "MEMORY.md").write_text("remember\n")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "one.md").write_text("skill")
    for name in (".env", "state.db", "session.db", "debug.log"):
        (tmp_path / name).write_text("secret")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "large").write_text("not durable")
    entries = inventory_profile(tmp_path)
    assert {r["key"] for r in entries} == {"config.yaml", "MEMORY.md", "skills/one.md"}
    inv = build_migration_inventory(tmp_path)
    assert all("state.db" not in r["key"] and ".env" not in r["key"] for r in inv)


def test_explicit_bootstrap_is_verified_idempotent_and_does_not_clone(store, tmp_path):
    owner, user = Principal("9000"), Principal("42")
    (tmp_path / "SOUL.md").write_text("owner only")
    first = bootstrap_owner(store, owner, tmp_path)
    again = bootstrap_owner(store, owner, tmp_path)
    assert first["written"] == first["records"]
    assert again["written"] == 0 and again["verified"] == first["records"]
    store.allow_user(owner, "42", True)
    assert store.get(user, "instructions", "SOUL.md") is None
    assert store.get(owner, "instructions", "SOUL.md")["path"] == "SOUL.md"


def test_symlink_and_traversal_rejected(tmp_path):
    (tmp_path / "skills").mkdir()
    outside = tmp_path.parent / "outside-profile-file"
    outside.write_text("x")
    (tmp_path / "skills" / "escape").symlink_to(outside)
    with pytest.raises(TenantStoreError):
        inventory_profile(tmp_path)


def test_hydrate_remote_authoritative_and_divergence_fails_closed(store, tmp_path):
    owner = Principal("9000")
    user = Principal("42")
    store.allow_user(owner, "42", True)
    cache = tmp_path / ".tenant-cache" / "tg-42"
    # Correct checksum for bytes "keep".
    import hashlib
    store.owner_put_tenant(owner, "tg-42", "instructions", "memories/keep.md", {
        "path": "memories/keep.md", "encoding": "base64", "content": "a2VlcA==",
        "sha256": hashlib.sha256(b"keep").hexdigest()})
    assert hydrate_tenant(store, user, cache) == 1
    assert (cache / "memories/keep.md").read_text() == "keep"
    (cache / "memories/keep.md").write_text("edited locally")
    with pytest.raises(TenantStoreError, match="diverges"):
        flush_tenant_cache(store, user, cache)
    with pytest.raises(TenantStoreError, match="diverges"):
        hydrate_tenant(store, user, cache)


def test_remote_path_traversal_rejected(store, tmp_path):
    owner, user = Principal("9000"), Principal("42")
    store.allow_user(owner, "42", True)
    store.owner_put_tenant(owner, "tg-42", "instructions", "../escape", {
        "path": "../escape", "encoding": "base64", "content": "eA==", "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"})
    with pytest.raises(TenantStoreError, match="Unsafe"):
        hydrate_tenant(store, user, tmp_path / ".tenant-cache" / "tg-42")


def test_cache_parent_symlink_cannot_modify_external_directory(store, tmp_path):
    import base64, hashlib
    owner, user = Principal("9000"), Principal("42")
    store.allow_user(owner, "42", True)
    outside = tmp_path / "outside"
    outside.mkdir()
    cache = tmp_path / ".tenant-cache" / "tg-42"
    cache.mkdir(parents=True)
    (cache / "memories").symlink_to(outside, target_is_directory=True)
    store.owner_put_tenant(owner, "tg-42", "instructions", "memories/MEMORY.md", {
        "path": "memories/MEMORY.md", "encoding": "base64", "content": base64.b64encode(b"private").decode(),
        "sha256": hashlib.sha256(b"private").hexdigest()})
    with pytest.raises(TenantStoreError, match="Unsafe"):
        hydrate_tenant(store, user, cache)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("path", ["", ".", "unapproved/config.yaml", "../escape", "/tmp/escape"])
def test_malformed_remote_path_is_safe_error(store, tmp_path, path):
    owner, user = Principal("9000"), Principal("42")
    store.allow_user(owner, "42", True)
    store.owner_put_tenant(owner, "tg-42", "instructions", "malformed", {"path": path})
    with pytest.raises(TenantStoreError):
        hydrate_tenant(store, user, tmp_path / ".tenant-cache" / "tg-42")


def test_bootstrap_preflights_all_conflicts_before_writes(store, tmp_path):
    owner = Principal("9000")
    (tmp_path / "SOUL.md").write_text("owner")
    store.owner_put_tenant(owner, "owner", "instructions", "SOUL.md", {"path": "SOUL.md", "different": True})
    before = store.enumerate_tenant_data(owner, "owner")
    with pytest.raises(TenantStoreError, match="differs"):
        bootstrap_owner(store, owner, tmp_path)
    assert store.enumerate_tenant_data(owner, "owner") == before
