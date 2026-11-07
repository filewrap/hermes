"""Offline integration coverage for MongoDB credential-only env runtime routing."""
import pytest
from agent import mongo_credentials


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
def runtime_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv(mongo_credentials.URI_ENV, raising=False)
    monkeypatch.delenv(mongo_credentials.KEY_ENV, raising=False)
    env_path = home / ".env"
    env_path.write_text(
        "MONGODB_URI=mongodb://example.invalid/db\n"
        "CREDENTIAL_KEY=bootstrap-key\n"
        "LOCAL_ONLY_TOKEN=plaintext-old-value\n"
        "NONCREDENTIAL_MODE=local-mode\n",
        encoding="utf-8",
    )
    return home


def test_runtime_reads_authoritative_credentials_and_routes_only_credentials(runtime_home, monkeypatch):
    from agent.secret_scope import load_env_file
    from hermes_cli import auth, config, env_loader

    home = runtime_home
    records = {"env": {"PROVIDER_TOKEN": "remote-value", "CONFIG_TEST_SECRET": "config-value",
                        "DEEP_CONFIG_LEGACY_SECRET": "legacy-config-value",
                        "NONCREDENTIAL_REMOTE_SETTING": "should-be-ignored"}}
    writes = []
    monkeypatch.setattr(mongo_credentials, "enabled", lambda path: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda path, name: dict(records.get(name, {})) or None)

    def put_record(path, name, payload):
        records[name] = dict(payload)
        writes.append((name, dict(payload)))
    monkeypatch.setattr(mongo_credentials, "put_record", put_record)

    loaded = load_env_file(home / ".env")
    assert loaded["PROVIDER_TOKEN"] == "remote-value"
    assert "LOCAL_ONLY_TOKEN" not in loaded  # Mongo is authoritative and key is absent remotely
    assert loaded["CREDENTIAL_KEY"] == "bootstrap-key"  # local bootstrap retained
    assert loaded["NONCREDENTIAL_MODE"] == "local-mode"
    assert "NONCREDENTIAL_REMOTE_SETTING" not in loaded

    # Startup's normal dotenv publication path hydrates the Mongo-backed value.
    env_loader._load_dotenv_with_fallback(home / ".env", override=True)
    assert __import__("os").environ["PROVIDER_TOKEN"] == "remote-value"
    assert __import__("os").environ.get("LOCAL_ONLY_TOKEN") != "plaintext-old-value"

    config.save_env_value("NEW_PROVIDER_TOKEN", "new-secret")
    assert records["env"]["NEW_PROVIDER_TOKEN"] == "new-secret"
    assert "NEW_PROVIDER_TOKEN" not in (home / ".env").read_text()
    config.save_env_value("LOCAL_PORT", "8123")
    assert records["env"].get("LOCAL_PORT") is None
    assert "LOCAL_PORT=8123" in (home / ".env").read_text()
    assert "LOCAL_ONLY_TOKEN=plaintext-old-value" in (home / ".env").read_text()
    assert "plaintext-old-value" not in __import__("os").environ.get("LOCAL_ONLY_TOKEN", "")
    assert config._env_ref_lookup("CONFIG_TEST_SECRET") == "config-value"
    assert config._env_ref_lookup("CONFIG_LEGACY_SECRET") == "legacy-config-value"
    assert config._env_ref_lookup("DEEP_CONFIG_LEGACY_SECRET") == "legacy-config-value"

    auth_path = home / "auth.json"
    auth_path.write_text('{"providers":{"local":{"api_key":"ignored"}}}')
    assert auth._load_auth_store(auth_path)["providers"] == {}
    auth._save_auth_store({"providers": {"remote": {"api_key": "auth-secret"}}}, target_path=auth_path)
    assert records["auth"]["providers"]["remote"]["api_key"] == "auth-secret"
    assert "auth-secret" not in auth_path.read_text()
    assert writes


def test_backend_failure_is_not_replaced_with_local_env(runtime_home, monkeypatch):
    from agent.secret_scope import load_env_file

    monkeypatch.setattr(mongo_credentials, "enabled", lambda path: True)
    def fail(path, name):
        raise mongo_credentials.CredentialStoreError("offline backend")
    monkeypatch.setattr(mongo_credentials, "get_record", fail)
    with pytest.raises(mongo_credentials.CredentialStoreError):
        load_env_file(runtime_home / ".env")


def test_startup_hydrates_remote_credentials_without_local_dotenv(runtime_home, monkeypatch):
    import os
    from hermes_cli import env_loader
    (runtime_home / ".env").unlink()
    monkeypatch.setattr(mongo_credentials, "enabled", lambda home: home == runtime_home)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda home, name: {"REMOTE_TEST_TOKEN": "remote-test-value"})
    monkeypatch.delenv("REMOTE_TEST_TOKEN", raising=False)
    env_loader.load_hermes_dotenv(hermes_home=runtime_home, load_external_secrets=False)
    assert os.environ["REMOTE_TEST_TOKEN"] == "remote-test-value"
    assert not (runtime_home / ".env").exists()
