"""Mongo-authoritative tenant accounting. SQL is a non-authoritative cache only.

Each usage event is an insert-once ledger record; each session has a CAS-updated
absolute snapshot plus a separately idempotent delta-event ledger. No driver or
storage failure is converted into local fallback accounting.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError, _uid


_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "api_call_count")
_VALUES = (*_COUNTERS, "estimated_cost_usd", "actual_cost_usd")


def _finite_nonnegative(value: Any, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise TenantStoreError(f"Invalid {field}")
    if field in _COUNTERS and not isinstance(value, int):
        raise TenantStoreError(f"Invalid {field}")
    return value


def _clean_usage(usage: dict) -> dict:
    if not isinstance(usage, dict) or set(usage) - set(_VALUES):
        raise TenantStoreError("Invalid usage fields")
    return {key: _finite_nonnegative(usage.get(key, 0), key) for key in _VALUES}


def _digest(tenant: str, key: str) -> str:
    return hashlib.sha256((tenant + "\0" + key).encode()).hexdigest()


class MongoAccountingStore:
    def __init__(self, root: Path, *, database: Any = None):
        self.root = Path(root)
        # The owner is immutable profile bootstrap policy, never a request or process-global
        # default. Non-multiuser local profiles have no external owner identity.
        from gateway.mongo_multiuser import owner_user_id
        try:
            owner = owner_user_id(self.root)
            self.tenants = MongoTenantStore(self.root, owner, database=database)
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Mongo accounting bootstrap unavailable") from None
        self.db = self.tenants.database
        self.events = self.db["hermes_accounting_events"]
        self.snapshots = self.db["hermes_accounting_sessions"]
        self.credits = self.db["hermes_accounting_credits"]
        self.legacy = self.db["hermes_accounting_legacy_imports"]

    def readycheck(self) -> dict:
        try:
            self._find_one(self.events, {"_id": "__schema_readiness__"}, {"_id": 1})
            return {"ready": True, "authority": "mongo"}
        except Exception:
            raise TenantStoreError("Mongo accounting readiness check failed") from None

    def _find_one(self, collection, query, projection=None):
        try:
            return collection.find_one(query, projection)
        except Exception:
            raise TenantStoreError("Mongo accounting read failed") from None

    def _active(self, principal: Principal) -> str:
        try:
            self.tenants._require_active(principal)
            return self.tenants.principal_tenant(principal)
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Mongo tenant authorization failed") from None

    def record_turn(self, principal: Principal, session_id: str, event_id: str, usage: dict) -> bool:
        tenant = self._active(principal)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise TenantStoreError("Invalid session identity")
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            raise TenantStoreError("Invalid usage event identity")
        values = _clean_usage(usage)
        ident = _digest(tenant, event_id)
        record = {"_id": ident, "tenant_id": tenant, "user_id": _uid(principal.user_id),
                  "event_id": event_id, "session_id": session_id, "usage": values}
        try:
            self.events.insert_one(record)
            inserted = True
        except Exception:
            existing = self._find_one(self.events, {"_id": ident})
            if existing is None:
                raise TenantStoreError("Mongo usage write failed") from None
            if any(existing.get(k) != record.get(k) for k in ("tenant_id", "user_id", "event_id", "session_id", "usage")):
                raise TenantStoreError("Usage event identity conflict") from None
            inserted = False
        # Reconcile even on duplicate: the first insert can commit while its
        # response is lost, before the aggregate CAS is reached.
        self._apply_delta(tenant, session_id, event_id, values)
        return inserted

    def _apply_delta(self, tenant: str, session_id: str, event_id: str, values: dict) -> None:
        # A ledger marker and aggregate update in one atomic per-session document CAS.
        ident = _digest(tenant, session_id)
        marker = _digest(tenant, event_id)
        for _ in range(32):
            doc = self._find_one(self.snapshots, {"_id": ident})
            if doc and marker in doc.get("applied_events", []):
                return
            old = doc.get("totals", {}) if doc else {}
            totals = {k: old.get(k, 0) + values[k] for k in _VALUES}
            events = list(doc.get("applied_events", [])) if doc else []
            events.append(marker)
            version = doc.get("revision", 0) if doc else 0
            new = {"_id": ident, "tenant_id": tenant, "session_id": session_id, "totals": totals,
                   "applied_events": events, "revision": version + 1}
            try:
                if doc:
                    result = self.snapshots.replace_one({"_id": ident, "revision": version}, new)
                    if result.matched_count:
                        return
                else:
                    self.snapshots.insert_one(new)
                    return
            except Exception:
                # Insert may have committed despite lost response; re-read marker.
                latest = self._find_one(self.snapshots, {"_id": ident})
                if latest and marker in latest.get("applied_events", []):
                    return
                if latest is None and not doc:
                    continue
                raise TenantStoreError("Mongo session accounting write failed") from None
        raise TenantStoreError("Mongo session accounting contention")

    def record_snapshot(self, principal: Principal, session_id: str, event_id: str, usage: dict) -> bool:
        """Persist absolute counters; event IDs make retries safe and stale revisions fail closed."""
        tenant = self._active(principal)
        values = _clean_usage(usage)
        if not isinstance(session_id, str) or not session_id or not isinstance(event_id, str) or not event_id:
            raise TenantStoreError("Invalid session snapshot identity")
        ident, event_ident = _digest(tenant, session_id), _digest(tenant, "snapshot:" + event_id)
        event = {"_id": event_ident, "tenant_id": tenant, "event_id": event_id, "session_id": session_id, "usage": values}
        try:
            self.events.insert_one(event)
        except Exception:
            old_event = self._find_one(self.events, {"_id": event_ident})
            if old_event is None:
                raise TenantStoreError("Mongo snapshot event write failed") from None
            if old_event.get("usage") != values or old_event.get("session_id") != session_id:
                raise TenantStoreError("Snapshot event identity conflict")
            duplicate = True
        else:
            duplicate = False
        import re
        match = re.search(r"(\d+)$", event_id)
        sequence = int(match.group(1)) if match else None
        for _ in range(32):
            old = self._find_one(self.snapshots, {"_id": ident})
            if old and old.get("snapshot_event") == event_id:
                return False
            if old and sequence is not None and old.get("snapshot_sequence") is not None and sequence <= old["snapshot_sequence"]:
                return False
            revision = old.get("revision", 0) if old else 0
            new = {"_id": ident, "tenant_id": tenant, "session_id": session_id, "totals": values,
                   "snapshot_event": event_id, "snapshot_sequence": sequence,
                   "applied_events": list(old.get("applied_events", [])) if old else [], "revision": revision + 1}
            try:
                if old:
                    if self.snapshots.replace_one({"_id": ident, "revision": revision}, new).matched_count:
                        return not duplicate
                else:
                    self.snapshots.insert_one(new)
                    return not duplicate
            except Exception:
                current = self._find_one(self.snapshots, {"_id": ident})
                if current and current.get("snapshot_event") == event_id and current.get("totals") == values:
                    return not duplicate
                if current is None and not old:
                    continue
                raise TenantStoreError("Mongo session snapshot write failed") from None
        raise TenantStoreError("Mongo session snapshot contention")

    def totals(self, principal: Principal) -> dict:
        tenant = self._active(principal)
        result = {k: 0 for k in _VALUES}
        try:
            for doc in self.snapshots.find({"tenant_id": tenant}):
                for key in _VALUES:
                    result[key] += doc.get("totals", {}).get(key, 0)
            return result
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Mongo usage totals unavailable") from None

    def owner_grant(self, actor: Principal, target: str | int, *, credits: int, weight: float | None = None) -> dict:
        self.tenants._owner_action(actor)
        if isinstance(credits, bool) or not isinstance(credits, int) or credits < 0:
            raise TenantStoreError("Invalid credit balance")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0):
            raise TenantStoreError("Invalid usage weight")
        uid = _uid(target)
        tenant = self.tenants.principal_tenant(Principal(uid))
        self.tenants._require_active(Principal(uid))
        try:
            previous = self._find_one(self.credits, {"_id": tenant}) or {}
            effective_weight = weight if weight is not None else previous.get("weight", 1)
            doc = {"_id": tenant, "user_id": uid, "balance": credits, "weight": effective_weight}
            self.credits.replace_one({"_id": tenant}, doc, upsert=True)
            return {"tenant_id": tenant, "balance": credits, "weight": effective_weight}
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Mongo credit grant failed") from None

    def balance(self, principal: Principal) -> dict:
        tenant = self._active(principal)
        doc = self._find_one(self.credits, {"_id": tenant})
        # Provider credits snapshots remain opaque; no token-to-credit conversion.
        return {"balance": doc.get("balance", 0) if doc else 0, "weight": doc.get("weight", 1) if doc else 1}

    def import_legacy_usage(self, actor: Principal, owner: Principal, session_id: str, usage: dict, *, metadata: dict | None = None) -> bool:
        """Idempotently import one SQL aggregate into its correct tenant; caller does pure SQL reads."""
        self.tenants._owner_action(actor)
        tenant = self.tenants.principal_tenant(owner)
        values = _clean_usage(usage)
        event_id = "legacy-session:" + session_id
        ident = _digest(tenant, event_id)
        doc = {"_id": ident, "tenant_id": tenant, "user_id": _uid(owner.user_id), "event_id": event_id,
               "session_id": session_id, "usage": values, "legacy_import": True,
               "metadata": metadata or {}}
        try:
            self.events.insert_one(doc)
        except Exception:
            try:
                found = self._find_one(self.events, {"_id": ident})
            except Exception:
                raise TenantStoreError("Legacy usage import failed") from None
            if not found or any(found.get(k) != doc.get(k) for k in ("tenant_id", "user_id", "event_id", "session_id", "usage", "legacy_import", "metadata")):
                raise TenantStoreError("Legacy usage import conflict") from None
            inserted = False
        else:
            inserted = True
        # Always reconcile: insert may have committed before the previous process died.
        self._apply_delta(tenant, "legacy:" + session_id, event_id, values)
        return inserted


class LocalAccountingStore(MongoAccountingStore):
    """Tenant ledgers are canonical SQLite; owner session usage stays in state.db."""
    def __init__(self, root: Path):
        from gateway.mongo_multiuser import store_for_home
        self.root = Path(root)
        self.tenants = store_for_home(self.root)
        self.db = self.tenants.database
        self.events = self.db["hermes_accounting_events"]
        self.snapshots = self.db["hermes_accounting_sessions"]
        self.credits = self.db["hermes_accounting_credits"]
        self.legacy = self.db["hermes_accounting_legacy_imports"]

    def readycheck(self) -> dict:
        self.events.find_one({"_id": "__schema_readiness__"}, {"_id": 1})
        return {"ready": True, "authority": "sqlite"}


def account_store(root: Path, *, database: Any = None) -> MongoAccountingStore:
    # Explicit database injection is an archive/test seam, not runtime routing.
    if database is not None:
        return MongoAccountingStore(root, database=database)
    return LocalAccountingStore(root)


def build_usage_inventory(root: Path, owner_id: str | int) -> dict:
    """Build a deterministic, pure read-only inventory from state.db and shared-state.db.

    Ownership is derived only from stored surface metadata: configured owner identity
    plus explicit source/user/chat/origin identity. Unknown external identities fail closed.
    """
    import json, sqlite3
    root = Path(root)
    owner_id = _uid(owner_id)
    records = []
    files = [root / "state.db", root / "shared-state.db"]
    for path in files:
        if not path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sessions" not in tables:
                conn.close(); continue
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            select = [k for k in ("id", "source", "user_id", "chat_id", "chat_type", "origin_json", *_VALUES) if k in cols]
            for row in conn.execute("SELECT " + ",".join('"'+k+'"' for k in select) + " FROM sessions"):
                data = dict(row); source = str(data.get("source") or "")
                origin = data.get("origin_json")
                if isinstance(origin, str):
                    try: origin = json.loads(origin)
                    except Exception: origin = {}
                origin = origin if isinstance(origin, dict) else {}
                ident = str(data.get("user_id") or origin.get("user_id") or "")
                if ident and ident == owner_id:
                    principal = owner_id
                elif ident:
                    # Explicit source user identity is authoritative for non-owner records.
                    principal = _uid(ident)
                elif source in {"cli", "cron", "kanban", "acp", "api_server", "subagent", "tool", "recovered"} or not ident and source not in {"telegram", "telegram_bot", "telegram_mtproto"}:
                    principal = owner_id
                else:
                    conn.close(); raise TenantStoreError("Legacy usage ownership is ambiguous")
                usage = {k: data.get(k, 0) or 0 for k in _VALUES}
                models = []
                if "session_model_usage" in tables:
                    mcols = {r[1] for r in conn.execute("PRAGMA table_info(session_model_usage)")}
                    if {"session_id", "model"} <= mcols:
                        mrows = conn.execute("SELECT * FROM session_model_usage WHERE session_id=?", (data["id"],)).fetchall()
                        models = [dict(r) for r in mrows]
                records.append({"source_db": path.name, "session_id": str(data["id"]), "user_id": principal, "usage": usage, "metadata": {"source": data.get("source"), "user_id": data.get("user_id"), "chat_id": data.get("chat_id"), "chat_type": data.get("chat_type"), "origin_json": data.get("origin_json"), "model_usage": models}})
            conn.close()
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Legacy usage inventory failed") from None
    records.sort(key=lambda r: (r["source_db"], r["session_id"]))
    payload = repr(records).encode()
    return {"records": records, "count": len(records), "digest": hashlib.sha256(payload).hexdigest()}

def verify_usage_inventory(accounting: MongoAccountingStore, owner: Principal, plan: dict) -> dict:
    accounting.tenants._owner_action(owner)
    if not isinstance(plan, dict) or not isinstance(plan.get("records"), list):
        raise TenantStoreError("Invalid usage migration plan")
    for record in plan["records"]:
        actor = Principal(record["user_id"])
        tenant = accounting.tenants.principal_tenant(actor)
        event_key = record["source_db"] + ":" + record["session_id"]
        doc = accounting._find_one(accounting.events, {"_id": _digest(tenant, "legacy-session:" + event_key)})
        if (not doc or doc.get("usage") != _clean_usage(record["usage"])
                or doc.get("metadata", {}) != record.get("metadata", {})):
            raise TenantStoreError("Legacy usage readback verification failed")
    return {"verified": len(plan["records"]), "digest": plan.get("digest")}


def migrate_usage_inventory(accounting: MongoAccountingStore, owner: Principal, plan: dict) -> dict:
    """Import a pure SQL plan and verify exact identity, usage and metadata."""
    accounting.tenants._owner_action(owner)
    if not isinstance(plan, dict) or not isinstance(plan.get("records"), list):
        raise TenantStoreError("Invalid usage migration plan")
    imported = 0
    for record in plan["records"]:
        principal = Principal(record["user_id"])
        event_key = record["source_db"] + ":" + record["session_id"]
        if accounting.import_legacy_usage(owner, principal, event_key, record["usage"], metadata=record.get("metadata", {})):
            imported += 1
    verified = verify_usage_inventory(accounting, owner, plan)
    return {"count": len(plan["records"]), "imported": imported, **verified}
