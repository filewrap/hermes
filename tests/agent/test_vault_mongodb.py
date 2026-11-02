"""Offline tests for the optional Mongo-backed vault persistence path."""

from __future__ import annotations

import pytest

from agent import mongo_credentials
from agent.vault_store import VaultError, VaultStore


def test_mongo_vault_roundtrip_has_no_local_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home-a"
    records = {}
    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda _home, name: records.get(name))
    monkeypatch.setattr(mongo_credentials, "put_record", lambda _home, name, value: records.__setitem__(name, value))

    store = VaultStore(home / "vault")
    item = store.add_item("login", "Example", {
        "identifier_type": "email", "identifier": "person@example.test",
        "password": "private-password", "otp_secret": "JBSWY3DPEHPK3PXP",
    }, "https://example.test/login")

    secret = store.resolve_secret(item.id)
    assert secret["password"] == "private-password"
    assert secret["otp_secret"] == "JBSWY3DPEHPK3PXP"
    assert records["vault"]["version"] == 1
    assert records["vault"]["items"][0]["secret"] == secret
    assert not (home / "vault" / "vault.key").exists()
    disk = b"".join(p.read_bytes() for p in home.rglob("*") if p.is_file())
    assert b"private-password" not in disk
    assert b"JBSWY3DPEHPK3PXP" not in disk


def test_mongo_backend_failure_does_not_fall_back_to_plaintext_or_local_vault(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda *_: (_ for _ in ()).throw(RuntimeError("offline")))

    store = VaultStore(home / "vault")
    with pytest.raises(VaultError):
        store.list_items()
    assert not (home / "vault" / "vault.json.enc").exists()
    assert not (home / "vault" / "vault.key").exists()


def test_mongo_vault_profile_alternation_isolated(tmp_path, monkeypatch):
    records = {}
    monkeypatch.setattr(mongo_credentials, "enabled", lambda _home: True)
    monkeypatch.setattr(mongo_credentials, "get_record", lambda home, name: records.get((str(home), name)))
    monkeypatch.setattr(mongo_credentials, "put_record", lambda home, name, value: records.__setitem__((str(home), name), value))

    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    a = VaultStore(home_a / "vault")
    b = VaultStore(home_b / "vault")
    a.add_item("login", "A", {"identifier_type": "username", "identifier": "alice", "password": "a-secret"}, "https://a.test")
    b.add_item("login", "B", {"identifier_type": "username", "identifier": "bob", "password": "b-secret"}, "https://b.test")

    assert [item.label for item in a.list_items()] == ["A"]
    assert [item.label for item in b.list_items()] == ["B"]
    assert a.resolve_secret(a.list_items()[0].id)["password"] == "a-secret"
    assert b.resolve_secret(b.list_items()[0].id)["password"] == "b-secret"
