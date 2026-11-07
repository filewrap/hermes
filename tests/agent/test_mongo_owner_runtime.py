import base64
import hashlib

import mongomock
import pytest

from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError
from gateway.mongo_runtime import get_owner_file, put_owner_file, owner_profile_context


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    return MongoTenantStore(tmp_path, "9000", database=mongomock.MongoClient().get_database("hermes"))


def seed(store, relpath, content):
    payload = {"path": relpath, "encoding": "base64",
               "content": base64.b64encode(content).decode(),
               "sha256": hashlib.sha256(content).hexdigest()}
    return store.owner_put_tenant(Principal("9000"), "owner", "instructions", relpath, payload)


def test_owner_write_uses_previous_cache_observation_and_updates_cache(store, tmp_path):
    actor = Principal("9000")
    (tmp_path / "SOUL.md").write_bytes(b"old")
    revision = seed(store, "SOUL.md", b"old")
    assert put_owner_file(store, actor, "SOUL.md", b"new", revision, expected_cache=b"old") == revision + 1
    assert (tmp_path / "SOUL.md").read_bytes() == b"new"
    assert get_owner_file(store, actor, "SOUL.md") == (b"new", revision + 1)


def test_owner_write_rejects_remote_revision_conflict_without_cache_change(store, tmp_path):
    actor = Principal("9000")
    (tmp_path / "SOUL.md").write_bytes(b"old")
    revision = seed(store, "SOUL.md", b"old")
    seed(store, "SOUL.md", b"newer")
    with pytest.raises(TenantStoreError, match="revision conflict"):
        put_owner_file(store, actor, "SOUL.md", b"attempt", revision, expected_cache=b"old")
    assert (tmp_path / "SOUL.md").read_bytes() == b"old"


def test_owner_write_rejects_diverged_local_cache_before_commit(store, tmp_path):
    actor = Principal("9000")
    (tmp_path / "SOUL.md").write_bytes(b"local divergence")
    revision = seed(store, "SOUL.md", b"remote")
    with pytest.raises(TenantStoreError, match="diverges"):
        put_owner_file(store, actor, "SOUL.md", b"attempt", revision, expected_cache=b"local divergence")
    assert store.get(actor, "instructions", "SOUL.md")["content"] == base64.b64encode(b"remote").decode()
    assert (tmp_path / "SOUL.md").read_bytes() == b"local divergence"


def test_owner_context_hydrates_before_config_read(tmp_path, monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda home: object())
    store = MongoTenantStore(tmp_path, "9000", database=mongomock.MongoClient().get_database("hermes"))
    seed(store, "config.yaml", b"agent:\n  max_turns: 7\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with owner_profile_context(store, Principal("9000"), tmp_path):
        assert config.read_raw_config()["agent"]["max_turns"] == 7
