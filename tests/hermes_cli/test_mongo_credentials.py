"""Offline migration through real YAML/env/auth boundaries and plaintext credential-only storage."""
import base64
import copy
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from agent import mongo_credentials as store
from hermes_cli.mongo_credentials import sync_credentials


@pytest.fixture(autouse=True)
def _restore_process_environment():
    # Runtime dotenv publication mutates os.environ outside monkeypatch.setenv.
    # Restore it so later offline tests never inherit a Mongo URI or credentials.
    import os
    original = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    # Archived adapter coverage only. Production storage policy cannot enable
    # this authority via a URI or rollout flag; local-default tests forbid it.
    monkeypatch.setattr("agent.storage_mode.mongo_primary_enabled", lambda home=None: True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv(store.URI_ENV, raising=False)
    monkeypatch.delenv(store.KEY_ENV, raising=False)
    key = base64.urlsafe_b64encode(b"m" * 32).decode()
    (home / ".env").write_text(f"MONGODB_URI=mongodb://example.invalid/deep\nCREDENTIAL_KEY={key}\nPROVIDER_TEST_TOKEN=private-test-value\n")
    documents = {}
    class Collection:
        def find_one(self, query):
            return copy.deepcopy(documents.get(query["_id"]))
        def insert_one(self, document):
            if document["_id"] in documents:
                raise RuntimeError("duplicate")
            documents[document["_id"]] = copy.deepcopy(document)
        def replace_one(self, query, document):
            current = documents.get(query["_id"])
            count = int(current is not None and current["revision"] == query["revision"])
            if count:
                documents[document["_id"]] = copy.deepcopy(document)
            return SimpleNamespace(matched_count=count)
    collection = Collection()
    monkeypatch.setattr(store, "_collection", lambda settings: collection)
    monkeypatch.setattr(store._REVISIONS, "values", {}, raising=False)
    return home, documents, collection


def test_migration_then_real_runtime_reads_and_new_writes_are_plaintext(configured):
    from agent.secret_scope import load_env_file
    from agent.vault_store import VaultStore
    from hermes_cli.auth import _load_auth_store, _save_auth_store
    from hermes_cli.config import load_env, save_env_value, save_config
    from hermes_cli.config_effective import load_user_config_effective
    home, documents, _ = configured
    auth = {"version": 1, "providers": {"test": {"api_key": "provider-secret-test"}}}
    (home / "auth.json").write_text(json.dumps(auth))
    (home / "config.yaml").write_text("model:\n  default: test/model\n  api_key: config-secret-test\nagent:\n  max_turns: 9\n")
    vault_dir = home / "vault"
    vault_dir.mkdir()
    key = Fernet.generate_key()
    (vault_dir / "vault.key").write_bytes(key)
    vault = {"version": 1, "items": []}
    (vault_dir / "vault.json.enc").write_bytes(Fernet(key).encrypt(json.dumps(vault).encode()))
    result = sync_credentials(home)
    assert result["credential_records"] == 3
    assert (home / "auth.json").exists()
    assert (vault_dir / "vault.json.enc").exists()
    assert "PROVIDER_TEST_TOKEN" in (home / ".env").read_text()
    assert "config-secret-test" in (home / "config.yaml").read_text()
    assert load_env()["PROVIDER_TEST_TOKEN"] == "private-test-value"
    assert load_env_file(home / ".env")["PROVIDER_TEST_TOKEN"] == "private-test-value"
    assert _load_auth_store()["providers"]["test"]["api_key"] == "provider-secret-test"
    assert load_user_config_effective()["model"]["api_key"] == "config-secret-test"
    assert VaultStore()._read_all() == []
    save_env_value("NEW_TEST_TOKEN", "new-private-test")
    assert load_env()["NEW_TEST_TOKEN"] == "new-private-test"
    _save_auth_store(auth)
    assert (home / "auth.json").exists()
    save_config({"model": {"default": "test/model", "api_key": "rotated-config-test"}}, strip_defaults=False)
    assert "rotated-config-test" not in (home / "config.yaml").read_text()
    assert load_user_config_effective()["model"]["api_key"] == "rotated-config-test"
    assert all(value in repr(documents) for value in ("private-test-value", "provider-secret-test", "new-private-test", "rotated-config-test"))
    # Preserved old local values must never silently overwrite newer remote credentials.
    with pytest.raises(store.CredentialStoreError, match="differ"):
        sync_credentials(home)
    assert load_env()["NEW_TEST_TOKEN"] == "new-private-test"


def test_failed_upload_preserves_every_local_credential(configured, monkeypatch):
    home, _, collection = configured
    original = (home / ".env").read_bytes()
    (home / "auth.json").write_text('{"providers":{"test":{"api_key":"offline"}}}')
    auth_original = (home / "auth.json").read_bytes()
    def fail(document):
        raise RuntimeError("secret-driver-error")
    monkeypatch.setattr(collection, "insert_one", fail)
    with pytest.raises(store.CredentialStoreError):
        sync_credentials(home)
    assert (home / ".env").read_bytes() == original
    assert (home / "auth.json").read_bytes() == auth_original


def test_preserve_local_keeps_all_sources_and_writes_plaintext_records(configured):
    home, documents, _ = configured
    from hermes_cli.mongo_credentials import split_config_secrets
    (home / "config.yaml").write_text("model:\n  api_key: config-preserved-test\nagent:\n  max_turns: 8\n")
    (home / "auth.json").write_text('{"providers":{"test":{"api_key":"auth-preserved-test"}}}')
    vault_dir = home / "vault"
    vault_dir.mkdir()
    vault_key = Fernet.generate_key()
    (vault_dir / "vault.key").write_bytes(vault_key)
    (vault_dir / "vault.json.enc").write_bytes(Fernet(vault_key).encrypt(b'{"version":1,"items":[]}'))
    oauth = home / ".anthropic_oauth.json"
    oauth.write_text('{"accessToken":"oauth-preserved-test","refreshToken":"oauth-refresh-test"}')
    token_dir = home / "mcp-tokens"
    token_dir.mkdir()
    mcp = token_dir / "server.json"
    mcp.write_text('{"access_token":"mcp-preserved-test","refresh_token":"mcp-refresh-test"}')
    sources = [home / ".env", home / "config.yaml", home / "auth.json", vault_dir / "vault.key",
               vault_dir / "vault.json.enc", oauth, mcp]
    original = {path: path.read_bytes() for path in sources}

    result = sync_credentials(home, preserve_local=True)

    assert result == {"credential_records": 5, "config_credentials": 1}
    assert all(path.exists() and path.read_bytes() == data for path, data in original.items())
    expected = {
        "env": {"PROVIDER_TEST_TOKEN": "private-test-value", **split_config_secrets({"model": {"api_key": "config-preserved-test"}})[1]},
        "auth": {"providers": {"test": {"api_key": "auth-preserved-test"}}},
        "vault": {"version": 1, "items": []},
        "anthropic_oauth": {"accessToken": "oauth-preserved-test", "refreshToken": "oauth-refresh-test"},
        "mcp_oauth:server:tokens": {"access_token": "mcp-preserved-test", "refresh_token": "mcp-refresh-test"},
    }
    assert {name: store.get_record(home, name) for name in expected} == expected
    assert set(documents) == {store._record_id(home, name) for name in expected}
    assert all("ciphertext" not in document and "nonce" not in document for document in documents.values())
    assert all(value.encode() in repr(documents).encode() for value in ("private-test-value", "config-preserved-test", "auth-preserved-test", "oauth-preserved-test", "mcp-preserved-test"))


def test_backend_outage_cannot_create_plaintext_auth_or_config(configured, monkeypatch):
    from hermes_cli.auth import _load_auth_store, _save_auth_store
    from utils import atomic_yaml_write
    home, _, collection = configured
    def fail(query):
        raise RuntimeError("database-uri-and-password-must-not-escape")
    monkeypatch.setattr(collection, "find_one", fail)
    with pytest.raises(store.CredentialStoreError):
        _load_auth_store()
    with pytest.raises(store.CredentialStoreError):
        _save_auth_store({"providers": {"test": {"api_key": "private-test"}}})
    with pytest.raises(store.CredentialStoreError):
        atomic_yaml_write(home / "config.yaml", {"model": {"api_key": "private-test"}})
    assert not (home / "auth.json").exists()
    assert not (home / "config.yaml").exists()
    assert not list(home.glob("*.corrupt"))


def test_config_secret_extraction_preserves_nonsecret_settings():
    from hermes_cli.mongo_credentials import split_config_secrets
    config = {"model": {"api_key": "private-test", "base_url": "https://example.invalid/v1"},
              "providers": {"custom": {"api": "https://example.invalid", "api_key": "${EXISTING_KEY}"}},
              "agent": {"max_turns": 10}, "mcp_servers": {"service": {"headers": {"Authorization": "Bearer offline-test"}}}}
    protected, values = split_config_secrets(config)
    assert protected["agent"] == config["agent"]
    assert protected["model"]["base_url"] == config["model"]["base_url"]
    assert protected["providers"] == config["providers"]
    assert set(values.values()) == {"private-test", "Bearer offline-test"}


def test_source_changed_during_upload_is_kept_not_deleted(configured, monkeypatch):
    home, _, collection = configured
    original_insert = collection.insert_one
    def insert_and_change(document):
        original_insert(document)
        with (home / ".env").open("a") as handle:
            handle.write("CONCURRENT_TEST_TOKEN=must-not-be-lost\n")
    monkeypatch.setattr(collection, "insert_one", insert_and_change)
    with pytest.raises(store.CredentialStoreError, match="changed during migration"):
        sync_credentials(home)
    assert "CONCURRENT_TEST_TOKEN=must-not-be-lost" in (home / ".env").read_text()
    assert "PROVIDER_TEST_TOKEN=private-test-value" in (home / ".env").read_text()


def test_native_oauth_and_mcp_migrate_then_runtime_uses_plaintext_records(configured):
    from agent.anthropic_credentials import read_hermes_oauth_credentials, _write_hermes_oauth_credentials
    from tools.mcp_oauth import HermesTokenStorage, _read_json, _write_json
    home, documents, _ = configured
    native = home / ".anthropic_oauth.json"
    native.write_text(json.dumps({"accessToken": "pkce-access-test", "refreshToken": "pkce-refresh-test"}))
    token_dir = home / "mcp-tokens"
    token_dir.mkdir()
    tokens = token_dir / "server.json"
    tokens.write_text(json.dumps({"access_token": "mcp-access-test", "refresh_token": "mcp-refresh-test", "expires_in": 3600}))
    client = token_dir / "server.client.json"
    client.write_text(json.dumps({"client_id": "client-test", "client_secret": "mcp-client-secret-test"}))
    meta = token_dir / "server.meta.json"
    meta.write_text(json.dumps({"issuer": "https://example.invalid"}))
    cimd = token_dir / "server.cimd-off"
    cimd.write_text("1")
    expected_expiry = tokens.stat().st_mtime + 3600
    sync_credentials(home)
    assert all(path.exists() for path in (native, tokens, client, meta, cimd))
    assert read_hermes_oauth_credentials()["accessToken"] == "pkce-access-test"
    assert _read_json(tokens)["refresh_token"] == "mcp-refresh-test"
    assert _read_json(tokens)["expires_at"] == expected_expiry
    assert _read_json(client)["client_secret"] == "mcp-client-secret-test"
    assert HermesTokenStorage("server", hermes_home=home).cimd_rejected()
    _write_json(tokens, {"access_token": "rotated-mcp-access-test"})
    _write_hermes_oauth_credentials("rotated-pkce-access-test", "rotated-pkce-refresh-test", 1234)
    assert native.exists() and tokens.exists()
    for value in ("rotated-pkce-access-test", "rotated-pkce-refresh-test", "mcp-client-secret-test", "rotated-mcp-access-test"):
        assert value in repr(documents)


def test_dashboard_disconnect_clears_backend_native_oauth(configured):
    from agent.anthropic_credentials import read_hermes_oauth_credentials, _write_hermes_oauth_credentials
    from hermes_cli.web_routers.oauth import _clear_anthropic_auth
    home, _, _ = configured
    sync_credentials(home)
    _write_hermes_oauth_credentials("disconnect-access-test", "disconnect-refresh-test", 1234)
    assert _clear_anthropic_auth()
    assert read_hermes_oauth_credentials() is None
    assert store.get_record(home, "anthropic_oauth") == {}
    assert not (home / ".anthropic_oauth.json").exists()


def test_default_and_false_preserve_sources_and_rerun_is_noop(configured):
    home, documents, _ = configured
    (home / "auth.json").write_text('{"providers":{"test":{"api_key":"key"}}}')
    with (home / ".env").open("a") as handle:
        handle.write("TELEGRAM_ALLOWED_USERS=123\nTELEGRAM_API_ID=456\nTELEGRAM_API_HASH=login-hash\nTERMINAL_TIMEOUT=20\n")
    (home / "state.db").write_bytes(b"local-sqlite-placeholder")
    before = {p:p.read_bytes() for p in home.iterdir() if p.is_file()}
    first = sync_credentials(home)
    docs = copy.deepcopy(documents)
    assert sync_credentials(home, preserve_local=False) == first
    assert documents == docs
    assert all(p.read_bytes() == value for p,value in before.items())
    env = store.get_record(home, "env")
    assert env["TELEGRAM_API_ID"] == "456" and env["TELEGRAM_API_HASH"] == "login-hash"
    assert not {"TELEGRAM_ALLOWED_USERS", "TERMINAL_TIMEOUT", store.URI_ENV, store.KEY_ENV} & set(env)
    assert len(documents) == 2


def test_environment_classifier_excludes_settings_and_bootstrap():
    from hermes_cli.mongo_credentials import is_credential_env
    for name in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "CUSTOM_SECRET"):
        assert is_credential_env(name)
    for name in (store.URI_ENV, store.KEY_ENV, "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL", "TERMINAL_TIMEOUT", "API_SERVER_PORT"):
        assert not is_credential_env(name)
