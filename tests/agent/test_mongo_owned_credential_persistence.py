from pathlib import Path

from agent import mongo_credentials


def test_anthropic_pkce_reads_writes_and_removes_backend_record(tmp_path, monkeypatch):
    from agent import anthropic_credentials, credential_sources

    records = {}
    monkeypatch.setattr(anthropic_credentials, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(mongo_credentials, "enabled", lambda home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda home, name: records.get(name))
    monkeypatch.setattr(mongo_credentials, "put_record", lambda home, name, data: records.__setitem__(name, dict(data)))

    # A legacy plaintext file is ignored whenever backend mode is active.
    (tmp_path / ".anthropic_oauth.json").write_text('{"accessToken":"plaintext"}')
    assert anthropic_credentials.read_hermes_oauth_credentials() is None
    anthropic_credentials._write_hermes_oauth_credentials("access", "refresh", 1234)
    assert records["anthropic_oauth"] == {
        "accessToken": "access", "refreshToken": "refresh", "expiresAt": 1234,
    }
    assert anthropic_credentials.read_hermes_oauth_credentials()["accessToken"] == "access"
    credential_sources._remove_hermes_pkce("anthropic", None)
    assert records["anthropic_oauth"] == {}
    assert not (tmp_path / ".anthropic_oauth.json").exists()


def test_mcp_oauth_token_and_client_state_use_separate_backend_records(tmp_path, monkeypatch):
    from tools.mcp_oauth import HermesTokenStorage, _read_json, _write_json

    records = {}
    monkeypatch.setattr(mongo_credentials, "enabled", lambda home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda home, name: records.get(name))
    monkeypatch.setattr(mongo_credentials, "put_record", lambda home, name, data: records.__setitem__(name, dict(data)))
    storage = HermesTokenStorage("server with spaces", hermes_home=tmp_path)
    # Exercise path-facing helpers as used by HermesTokenStorage, including secret-bearing client data.
    _write_json(storage._tokens_path(), {"access_token": "token", "refresh_token": "refresh"})
    _write_json(storage._client_info_path(), {"client_id": "id", "client_secret": "secret"})
    assert records["mcp_oauth:server_with_spaces:tokens"]["refresh_token"] == "refresh"
    assert records["mcp_oauth:server_with_spaces:client"]["client_secret"] == "secret"
    assert _read_json(storage._tokens_path())["access_token"] == "token"
    assert not storage._tokens_path().exists()
    storage.remove()
    assert records["mcp_oauth:server_with_spaces:tokens"] == {}
    assert records["mcp_oauth:server_with_spaces:client"] == {}
    assert not storage.has_cached_tokens()


def test_mcp_async_storage_and_cache_invalidation_do_not_depend_on_local_files(tmp_path, monkeypatch):
    import asyncio
    import copy
    from types import SimpleNamespace
    from pydantic import BaseModel
    from tools import mcp_oauth
    from tools.mcp_oauth_manager import MCPOAuthManager, _ProviderEntry

    class Token(BaseModel):
        access_token: str
        refresh_token: str | None = None
        expires_in: int | None = None
    records = {}
    monkeypatch.setattr(mongo_credentials, "enabled", lambda home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda home, name: copy.deepcopy(records.get(name)))
    monkeypatch.setattr(mongo_credentials, "put_record", lambda home, name, data: records.__setitem__(name, copy.deepcopy(data)))
    monkeypatch.setattr(mcp_oauth, "_sdk_class", lambda name: Token)
    storage = mcp_oauth.HermesTokenStorage("server", hermes_home=tmp_path)
    asyncio.run(storage.set_tokens(Token(access_token="opaque-access", refresh_token="opaque-refresh", expires_in=3600)))
    loaded = asyncio.run(storage.get_tokens())
    assert loaded.access_token == "opaque-access" and 0 < loaded.expires_in <= 3600
    assert not storage._tokens_path().exists()
    manager = MCPOAuthManager()
    provider = SimpleNamespace(_initialized=True)
    manager._entries[manager._key("server", tmp_path)] = _ProviderEntry("https://example.invalid", None, provider)
    assert asyncio.run(manager.invalidate_if_disk_changed("server", hermes_home=tmp_path))
    assert provider._initialized is False
