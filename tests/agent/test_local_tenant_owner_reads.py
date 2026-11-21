"""Authorization reads must not contend for SQLite's single writer slot."""
import json
import sqlite3
from contextlib import closing

import pytest

from agent.local_tenants import LocalTenantStore
from agent.mongo_tenants import TenantStoreError


def test_existing_owner_access_needs_no_write_transaction(tmp_path, monkeypatch):
    store = LocalTenantStore(tmp_path, "100")
    connect = sqlite3.connect
    denied = []

    def read_only_connect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def authorize(action, arg1, arg2, database, trigger):
            if action in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_INSERT,
                          sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                denied.append(action)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        db.set_authorizer(authorize)
        return db

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    assert store.ensure_owner()["user_id"] == "100"
    assert store.access_for_scope("100", "dm", "100")["allowed"] is True
    assert denied == []


def test_owner_reads_revalidate_durable_identity_and_insert_only_when_missing(tmp_path):
    store = LocalTenantStore(tmp_path, "100")
    # An independent writer must be visible immediately, not hidden by a cached
    # authorization result or an in-memory bootstrap flag.
    with closing(sqlite3.connect(store.database_path)) as db:
        db.execute("UPDATE tenant_documents SET body=? WHERE collection=? AND id='owner'",
                   (json.dumps({"_id": "owner", "user_id": "200"}), "hermes_tenant_profiles"))
        db.commit()
    with pytest.raises(TenantStoreError, match="owner identity"):
        store.ensure_owner()
    with closing(sqlite3.connect(store.database_path)) as db:
        db.execute("DELETE FROM tenant_documents WHERE collection=? AND id='owner'",
                   ("hermes_tenant_profiles",))
        db.commit()
    assert store.ensure_owner()["user_id"] == "100"
    assert LocalTenantStore(tmp_path, "100").ensure_owner()["user_id"] == "100"
