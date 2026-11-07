"""Owner-only verification of already-migrated Mongo authority.

Deployment uses agent.mongo_migration explicitly. Runtime /sync must not re-import
preserved local sources, stale credentials, or whole-profile snapshots.
"""
from __future__ import annotations

from pathlib import Path

from agent.mongo_credentials import CredentialStoreError


def verify_active_profile(store, actor, home: Path) -> dict:
    from agent.mongo_migration import MIGRATION_ID
    from agent.mongo_tenants import _validate_payload
    from agent import mongo_credentials
    from agent.mongo_sql_cache import verify_sql_cache

    store._owner_action(actor)
    manifest = store.database.hermes_migrations.find_one({"_id": MIGRATION_ID})
    if not manifest or manifest.get("status") != "complete" or manifest.get("owner_id") != store.owner_user_id:
        raise CredentialStoreError("A verified semantic deployment migration is required first")
    records = 0
    for document in store._data.find():
        _validate_payload(document["payload"])
        if document["_id"] != store._data_id(document["tenant_id"], document["kind"], document["key"]):
            raise CredentialStoreError("Durable record identity verification failed")
        records += 1
    if records < manifest.get("durable_verified", 0):
        raise CredentialStoreError("Migrated durable inventory is incomplete")
    credentials = 0
    collection = mongo_credentials._collection(mongo_credentials._settings(home))
    for document in collection.find({}, {"_id": 1, "name": 1}):
        name = document.get("name")
        mongo_credentials._validate_name(name)
        if document["_id"] != mongo_credentials._record_id(home, name):
            continue  # A different explicitly configured profile is not this command's scope.
        if mongo_credentials.get_record(home, name) is None:
            raise CredentialStoreError("Credential record read-back failed")
        credentials += 1
    if credentials < manifest.get("credentials_verified", 0):
        raise CredentialStoreError("Migrated credential inventory is incomplete")
    sql = 0
    for item in manifest.get("sql_receipts", []):
        verify_sql_cache(home, home / item["path"], mongo_database=store.database)
        sql += 1
    if sql != manifest.get("sql_verified", 0):
        raise CredentialStoreError("SQL cache inventory verification is incomplete")
    return {"records": records, "written": 0, "verified": records,
            "credential_records": credentials, "sql_databases": sql, "local_deleted": 0}


def sync_profile(home: Path, *, preserve_local: bool = True, actor=None, tenant_store=None,
                 multiuser_enabled: bool = False) -> dict:
    """Verify existing owner-authorized state without importing or deleting sources."""
    if not multiuser_enabled or actor is None or tenant_store is None:
        raise CredentialStoreError("Mongo profile sync requires explicitly enabled multiuser owner mode")
    try:
        return verify_active_profile(tenant_store, actor, Path(home).expanduser().resolve())
    except Exception:
        raise CredentialStoreError("Cannot verify authoritative Mongo profile state; local sources retained") from None
