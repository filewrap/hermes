"""Explicit plaintext tenant migration; no local source or unrelated DB is deleted."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from agent.mongo_profile_store import build_migration_inventory
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError, _tenant_id

MIGRATION_ID = "plaintext-tenants-v2"


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _json(path: Path) -> dict:
    if path.is_symlink():
        raise TenantStoreError("Linked personal source is not permitted")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise TenantStoreError("Cannot read personal settings source") from None


def personal_inventory(home: Path, owner_id: str) -> tuple[list[dict], set[str], set[Path]]:
    """Read original settings/memories/skills with their original external identity."""
    from gateway.telegram_personal_settings import _DEFAULTS, _validated_changes
    from plugins.platforms.telegram.telegram_user_settings_store import normalize_user_settings
    records, users, sources = [], {owner_id}, set()

    def add(uid, kind, key, payload):
        if not str(uid).isdecimal():
            raise TenantStoreError("Legacy personal identity is not a numeric Telegram identity")
        users.add(str(uid))
        records.append({"user_id": str(uid), "kind": kind, "key": key, "payload": payload})

    for base in (home / "personal/telegram/users", home / "telegram/users"):
        if base.is_symlink():
            raise TenantStoreError("Linked personal profile directory")
        if not base.exists():
            continue
        for directory in sorted(base.iterdir()):
            if directory.is_symlink():
                raise TenantStoreError("Linked personal profile directory")
            if not directory.is_dir():
                continue
            uid = directory.name
            if not uid.isdecimal():
                raise TenantStoreError("Cannot attribute legacy personal profile")
            users.add(uid)
            settings_path = directory / "settings.json"
            if settings_path.exists():
                raw = _json(settings_path)
                sources.add(settings_path)
                # Preserve every original field privately, including future extensions.
                add(uid, "policy", "legacy_personal_source", raw)
                active = {k: v for k, v in raw.items() if k in _DEFAULTS and k != "updated_at"}
                if uid != owner_id:
                    for key in ("selected_mcp_servers", "personal_mcp_servers"):
                        active.pop(key, None)
                clean = {**_DEFAULTS, **_validated_changes(active)}
                clean["updated_at"] = str(raw.get("updated_at") or "")
                add(uid, "settings", "personal", clean)
            for name in ("MEMORY.md", "USER.md"):
                path = directory / "memory" / name
                if path.is_symlink() or path.parent.is_symlink():
                    raise TenantStoreError("Linked personal memory source")
                if path.is_file():
                    try:
                        text = path.read_text(encoding="utf-8-sig")
                    except (OSError, UnicodeError):
                        raise TenantStoreError("Unreadable personal memory") from None
                    sources.add(path)
                    add(uid, "memory", name, text)
            for category in ("skills", "instructions"):
                folder = directory / category
                if folder.is_symlink():
                    raise TenantStoreError("Linked personal durable source")
                if not folder.is_dir():
                    continue
                for path in sorted(folder.rglob("*")):
                    if path.is_symlink():
                        raise TenantStoreError("Linked personal durable source")
                    if not path.is_file() or path.suffix in {".lock", ".pyc", ".tmp"}:
                        continue
                    if not path.resolve().is_relative_to(directory.resolve()):
                        raise TenantStoreError("Personal source path escapes its profile")
                    import base64
                    raw = path.read_bytes()
                    sources.add(path)
                    add(uid, category, path.relative_to(folder).as_posix(), {
                        "encoding": "base64", "content": base64.b64encode(raw).decode(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    })
    path = home / "telegram_user_settings.sqlite3"
    if path.exists():
        if path.is_symlink():
            raise TenantStoreError("Linked legacy settings SQL source")
        sources.add(path)
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {"telegram_user_settings"}:
                    raise TenantStoreError("Unexpected legacy settings SQL schema")
                for uid, payload in conn.execute("SELECT user_id, settings_json FROM telegram_user_settings ORDER BY user_id"):
                    raw = json.loads(payload)
                    if not isinstance(raw, dict):
                        raise ValueError
                    add(uid, "policy", "legacy_telegram_source", raw)
                    clean = normalize_user_settings(raw)
                    if str(uid) != owner_id:
                        clean.update(default_provider="", mcp_servers=[], mcp_customization_enabled=False)
                    add(uid, "settings", "telegram", clean)
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Cannot inventory legacy Telegram SQL settings") from None
    return records, users, sources


def build_plan(home: Path, owner_id: str, allowed_ids: list[str]) -> dict:
    """Pure preflight; include all known durable adapters before making any DB write."""
    from agent.mongo_accounting import build_usage_inventory
    from hermes_cli.mongo_credentials import _owned_legacy_files
    home = Path(home).resolve(strict=True)
    if not str(owner_id).isdecimal() or any(not str(uid).isdecimal() for uid in allowed_ids):
        raise TenantStoreError("Explicit numeric owner/access identities are required")
    records = [{"user_id": owner_id, **r} for r in build_migration_inventory(home)]
    personal, users, sources = personal_inventory(home, owner_id)
    records.extend(personal)
    usage = build_usage_inventory(home, owner_id)
    users.update(str(r["user_id"]) for r in usage["records"])
    users.update(str(uid) for uid in allowed_ids)
    # Fail before upload on duplicate/conflicting semantic addresses.
    unique = {}
    for record in records:
        key = (record["user_id"], record["kind"], record["key"])
        if key in unique and unique[key] != record:
            raise TenantStoreError("Conflicting legacy durable sources")
        unique[key] = record
    records = [unique[k] for k in sorted(unique)]
    from bson import BSON
    if any(len(BSON.encode(r)) > 15_000_000 for r in records):
        raise TenantStoreError("Durable document needs explicit chunk migration")
    for record in records:
        if record["kind"] == "instructions" and isinstance(record["payload"], dict) and "path" in record["payload"]:
            sources.add(home / record["payload"]["path"])
    sources.update(p for p in (home / ".env", home / "auth.json", home / "config.yaml",
                              home / "vault/vault.json.enc", home / "vault/vault.key", *_owned_legacy_files(home)) if p.exists())
    for name in ("memories/MEMORY.md", "memories/USER.md"):
        if (home / name).is_file():
            sources.add(home / name)
    for name in ("state.db", "shared-state.db"):
        if (home / name).exists():
            sources.add(home / name)
    from agent.mongo_sql_cache import _snapshot, _namespace, _digest, _manifest
    sql = []
    for path in sorted(set(home.glob("*.db")) | set(home.glob("*.sqlite3"))):
        if path.is_symlink():
            raise TenantStoreError("Linked durable SQL source")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            snapshot = _snapshot(connection)
        if len(BSON.encode(_manifest(snapshot, None, _namespace(path, home)))) > 15_000_000:
            raise TenantStoreError("SQL inventory requires an explicit larger migration")
        if any(len(BSON.encode({"payload": row})) > 15_000_000 for table in snapshot["tables"].values() for row in table["rows"].values()):
            raise TenantStoreError("Durable SQL row needs explicit chunk migration")
        sql.append({"path": path.relative_to(home).as_posix(), "namespace": _namespace(path, home),
                    "digest": _digest(snapshot), "tables": len(snapshot["tables"]),
                    "rows": sum(len(t["rows"]) for t in snapshot["tables"].values())})
        sources.add(path)
    hashes = {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)}
    plan = {"schema": 2, "owner_id": owner_id, "users": sorted(users),
            "allowed_ids": sorted(set(map(str, allowed_ids))), "records": records,
            "usage": usage, "sql": sql, "source_hashes": hashes}
    plan["digest"] = _hash(plan)
    return plan


def assert_sources_unchanged(home: Path, plan: dict) -> None:
    for rel, digest in plan["source_hashes"].items():
        path = Path(home) / rel
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise TenantStoreError("A local durable source changed during migration")


def execute_plan(store: MongoTenantStore, actor: Principal, plan: dict, *, credential_sync=None, accounting=None) -> dict:
    """Apply only this deterministic plan, resume safely, verify all records, keep sources."""
    from agent.mongo_accounting import account_store, migrate_usage_inventory
    from hermes_cli.mongo_credentials import sync_credentials
    if not store._is_owner(actor) or str(actor.user_id) != plan["owner_id"]:
        raise TenantStoreError("Owner-authorized migration required")
    unsigned = {k: v for k, v in plan.items() if k != "digest"}
    if _hash(unsigned) != plan.get("digest"):
        raise TenantStoreError("Migration plan digest mismatch")
    assert_sources_unchanged(store.home, plan)
    manifests = store.database["hermes_migrations"]
    previous = manifests.find_one({"_id": MIGRATION_ID})
    if previous and (previous.get("digest") != plan["digest"] or previous.get("owner_id") != plan["owner_id"]):
        raise TenantStoreError("Existing migration requires explicit reconciliation")
    expected = {(r["user_id"], r["kind"], r["key"]): r["payload"] for r in plan["records"]}
    # Only local, verified addresses authorize payload reads. Unrelated rows are
    # neither migration inputs nor a reason to reject an otherwise safe import.
    for (uid, kind, key), payload in expected.items():
        tenant = "owner" if uid == plan["owner_id"] else _tenant_id(uid)
        doc = store._data.find_one({"_id": store._data_id(tenant, kind, key)})
        if doc is not None and (doc.get("tenant_id") != tenant or doc.get("kind") != kind
                                or doc.get("key") != key or doc.get("payload") != payload):
            raise TenantStoreError("Unexpected or conflicting durable rows before migration")
    for uid in plan["users"]:
        tenant = "owner" if uid == plan["owner_id"] else _tenant_id(uid)
        profile = store._profiles.find_one({"_id": tenant}, {"user_id": 1, "profile_id": 1})
        if profile is not None and (profile.get("user_id") != uid or profile.get("profile_id") != tenant):
            raise TenantStoreError("Conflicting user profile before migration")
    from agent.mongo_sql_cache import _read_head, _digest, SQLCacheError
    for item in plan.get("sql", []):
        if store.database.hermes_sql_heads.find_one({"_id": "state:" + item["namespace"]}):
            _head, snapshot = _read_head(store.database, item["namespace"])
            if _digest(snapshot) != item["digest"]:
                raise TenantStoreError("Conflicting SQL inventory before migration")
    marker = {"_id": MIGRATION_ID, "schema": 2, "owner_id": plan["owner_id"], "digest": plan["digest"], "status": "pending"}
    if previous is None:
        manifests.insert_one(marker)
    store.ensure_owner()
    for uid in plan["users"]:
        tenant = "owner" if uid == plan["owner_id"] else _tenant_id(uid)
        profile = store.provision_user(uid)
        if profile.get("user_id") != uid or profile.get("profile_id") != tenant:
            raise TenantStoreError("Conflicting user profile during migration")
        if not previous or previous.get("status") != "complete":
            if uid != plan["owner_id"] and uid in plan["allowed_ids"]:
                result = store._profiles.update_one(
                    {"_id": tenant, "user_id": uid, "profile_id": tenant}, {"$set": {"allowed": True}})
                if result.matched_count != 1:
                    raise TenantStoreError("Conflicting user profile during migration")
    for record in plan["records"]:
        uid, kind, key, payload = record["user_id"], record["kind"], record["key"], record["payload"]
        tenant = "owner" if uid == plan["owner_id"] else _tenant_id(uid)
        found = store._data.find_one({"_id": store._data_id(tenant, kind, key)})
        if found is None:
            # Initial inserts must not turn into updates if another writer races us.
            store._put(tenant, kind, key, payload, expected_revision=0)
        verified = store._data.find_one({"_id": store._data_id(tenant, kind, key)})
        if (verified is None or verified.get("tenant_id") != tenant or verified.get("kind") != kind
                or verified.get("key") != key or verified.get("payload") != payload):
            raise TenantStoreError("Durable migration record read-back failed")
    if credential_sync is None:
        credentials = sync_credentials(store.home, preserve_local=True, _migration_authorized=True)
    else:
        credentials = credential_sync(store.home, preserve_local=True)
    accounts = accounting or account_store(store.home, database=store.database)
    usage_receipt = migrate_usage_inventory(accounts, actor, plan["usage"])
    from agent.mongo_sql_cache import bootstrap_sql_cache, verify_sql_cache
    sql_receipts = []
    for item in plan.get("sql", []):
        path = store.home / item["path"]
        bootstrap_sql_cache(store.home, path, mongo_database=store.database)
        verified = verify_sql_cache(store.home, path, mongo_database=store.database)
        if verified["sha256"] != item["digest"]:
            raise TenantStoreError("SQL read-back differs from the migration plan")
        sql_receipts.append({"path": item["path"], "verified": True, "rows": item["rows"], "digest": item["digest"]})
    assert_sources_unchanged(store.home, plan)
    receipt = {**marker, "status": "complete", "durable_verified": len(plan["records"]),
               "usage_verified": usage_receipt["verified"], "credentials_verified": credentials["credential_records"],
               "sql_verified": len(sql_receipts), "sql_receipts": sql_receipts,
               "source_files_preserved": len(plan["source_hashes"]), "local_deleted": 0}
    if receipt["usage_verified"] != len(plan["usage"]["records"]):
        raise TenantStoreError("Usage inventory verification is incomplete")
    manifests.replace_one({"_id": MIGRATION_ID, "digest": plan["digest"]}, receipt)
    if manifests.find_one({"_id": MIGRATION_ID}) != receipt:
        raise TenantStoreError("Migration completion manifest read-back failed")
    return receipt


def main(argv: list[str] | None = None) -> int:
    """Preflight or apply the owner migration without deleting local sources."""
    import argparse
    import os
    import re
    import sys
    from hermes_constants import get_process_hermes_home
    from gateway.mongo_multiuser import _profile_env, owner_user_id
    from agent.mongo_credentials import CredentialStoreError
    from agent.mongo_sql_cache import SQLCacheError

    parser = argparse.ArgumentParser(description="Migrate Hermes profile data to MongoDB; chat transcripts stay local.")
    parser.add_argument("--home", type=Path, help="Profile home (defaults to HERMES_HOME).")
    parser.add_argument("--apply", action="store_true", help="Apply the verified, idempotent migration; local sources are preserved.")
    parser.add_argument("--enable-primary", action="store_true", help="Select Mongo as primary after a complete migration; requires --gateway-stopped.")
    parser.add_argument("--gateway-stopped", action="store_true", help="Confirm the gateway is stopped before activation; this command never restarts it.")
    args = parser.parse_args(argv)
    if args.enable_primary and (not args.apply or not args.gateway_stopped):
        parser.error("--enable-primary requires --apply and --gateway-stopped")
    home = (args.home or Path(os.environ.get("HERMES_HOME", get_process_hermes_home()))).expanduser().resolve(strict=True)
    try:
        owner = owner_user_id(home)
        values = _profile_env(home)
        allowed = sorted({part for part in re.split(r"[,\s]+", values.get("TELEGRAM_ALLOWED_USERS", "").strip()) if part})
        plan = build_plan(home, owner, allowed)
        print(f"Plan ready: {len(plan['records'])} durable records; {len(plan['sql'])} SQL databases; chat messages and FTS indexes excluded; local sources preserved.")
        if not args.apply:
            print("Dry run only. Re-run with --apply to write the verified backup.")
            return 0
        store = MongoTenantStore(home, owner)
        receipt = execute_plan(store, Principal(owner), plan)
        print(f"Migration complete and verified: {receipt['durable_verified']} durable records, {receipt['usage_verified']} usage records, {receipt['sql_verified']} SQL databases; local files and chat history were not deleted.")
        if args.enable_primary:
            from agent.storage_mode import activate_mongo_primary
            activate_mongo_primary(home, receipt, mongo_database=store.database)
            print("Mongo is primary for non-chat state on the next gateway start; message history remains local and in eligible HF bucket backups.")
        return 0
    except Exception as exc:
        # Surface only safe application errors and exception classes, never a URI or driver payload.
        detail = str(exc) if isinstance(exc, (TenantStoreError, CredentialStoreError, SQLCacheError)) else type(exc).__name__
        print(f"Mongo migration failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
