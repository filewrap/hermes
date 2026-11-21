"""Authoritative Mongo bootstrap is explicit, owner-authenticated and non-destructive."""
import pytest
from agent.mongo_credentials import CredentialStoreError
from gateway.mongo_sync import sync_profile


def test_implicit_sync_never_touches_local_files_or_database(tmp_path, monkeypatch):
    from gateway import mongo_sync
    monkeypatch.setattr(mongo_sync, "verify_active_profile", lambda *a: pytest.fail("implicit bootstrap"))
    path = tmp_path / "state.db"
    path.write_bytes(b"preserved local SQL")
    with pytest.raises(CredentialStoreError, match="explicitly enabled"):
        sync_profile(tmp_path)
    assert path.read_bytes() == b"preserved local SQL"


def test_explicit_owner_verification_retains_local_sources(tmp_path, monkeypatch):
    from gateway import mongo_sync
    actor, store, calls = object(), object(), []
    def bootstrap(given_store, given_actor, home):
        calls.append((given_store, given_actor, home))
        return {"records": 2, "written": 0, "verified": 2, "local_deleted": 0}
    monkeypatch.setattr(mongo_sync, "verify_active_profile", bootstrap)
    result = sync_profile(tmp_path, preserve_local=False, actor=actor, tenant_store=store, multiuser_enabled=True)
    assert result == {"records": 2, "written": 0, "verified": 2, "local_deleted": 0}
    assert calls == [(store, actor, tmp_path.resolve())]


@pytest.mark.parametrize("omitted", ["actor", "tenant_store", "multiuser_enabled"])
def test_all_explicit_requirements_are_mandatory(tmp_path, omitted):
    kwargs = {"actor": object(), "tenant_store": object(), "multiuser_enabled": True}
    kwargs.pop(omitted)
    with pytest.raises(CredentialStoreError, match="explicitly enabled"):
        sync_profile(tmp_path, **kwargs)


def test_raw_driver_failure_is_not_exposed(tmp_path, monkeypatch):
    from gateway import mongo_sync
    def fail(*args):
        raise RuntimeError("mongodb://private:password@example.invalid")
    monkeypatch.setattr(mongo_sync, "verify_active_profile", fail)
    with pytest.raises(CredentialStoreError) as caught:
        sync_profile(tmp_path, actor=object(), tenant_store=object(), multiuser_enabled=True)
    assert "private" not in str(caught.value) and "password" not in str(caught.value)


def test_runtime_verification_keeps_new_remote_credentials_and_never_imports_sources(tmp_path, monkeypatch):
    import mongomock
    from agent import mongo_credentials
    from agent.mongo_tenants import MongoTenantStore, Principal
    from agent.mongo_migration import MIGRATION_ID
    from hermes_cli import mongo_credentials as migration
    database = mongomock.MongoClient().hermes
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=1\nMULTIUSER_ENABLED=true\nMONGODB_URI=mongodb://unused/hermes\nOPENAI_API_KEY=preserved-old-local\n")
    before = (tmp_path / ".env").read_bytes()
    monkeypatch.setattr(mongo_credentials, "_settings", lambda home: mongo_credentials._Settings("mongodb://unused/hermes"))
    monkeypatch.setattr(mongo_credentials, "_collection", lambda settings: database.deep_credentials)
    store = MongoTenantStore(tmp_path, "1", database=database)
    store.ensure_owner()
    store.allow_user(Principal("1"), "2", True)
    mongo_credentials.put_record(tmp_path, "env", {"OPENAI_API_KEY": "new-authoritative-key"})
    database.hermes_migrations.insert_one({"_id": MIGRATION_ID, "owner_id": "1", "status": "complete", "durable_verified": 0, "credentials_verified": 1, "sql_verified": 0})
    monkeypatch.setattr(migration, "sync_credentials", lambda *a, **kw: pytest.fail("runtime sync imported local credentials"))
    result = sync_profile(tmp_path, actor=Principal("1"), tenant_store=store, multiuser_enabled=True)
    assert result["written"] == result["local_deleted"] == 0
    assert result["credential_records"] == 1
    assert mongo_credentials.get_record(tmp_path, "env")["OPENAI_API_KEY"] == "new-authoritative-key"
    assert (tmp_path / ".env").read_bytes() == before
    with pytest.raises(CredentialStoreError):
        sync_profile(tmp_path, actor=Principal("2"), tenant_store=store, multiuser_enabled=True)
    database.hermes_migrations.delete_one({"_id": MIGRATION_ID})
    with pytest.raises(CredentialStoreError):
        sync_profile(tmp_path, actor=Principal("1"), tenant_store=store, multiuser_enabled=True)
