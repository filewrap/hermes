"""Mongo-backed tenant profiles and data isolation for Hermes.

All records are plaintext in explicitly named ``hermes_*`` collections. Mongo
configuration and verified-TLS client setup are shared with mongo_credentials.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent import mongo_credentials

logger = logging.getLogger(__name__)


class TenantStoreError(RuntimeError):
    """Safe, non-sensitive tenant storage/authorization error."""


@dataclass(frozen=True)
class Principal:
    """Authenticated external identity. Never construct from caller privilege flags."""
    user_id: str | int


_PUBLIC_KINDS = frozenset({"profile", "settings", "skills", "instructions", "memory", "usage"})
_PRIVILEGED_KINDS = frozenset({"providers", "provider_credentials", "access", "policy", "weight", "credits"})
_SAFE_GROUP_FIELDS = frozenset({"enabled", "name", "description", "default_model", "system_prompt"})
_NUMERIC_ID = re.compile(r"[0-9]+\Z")


def _uid(value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TenantStoreError("Invalid user identity")
    value = str(value).strip()
    if not value or len(value) > 512 or "\x00" in value:
        raise TenantStoreError("Invalid user identity")
    return value


def _tenant_id(user_id: str) -> str:
    if _NUMERIC_ID.fullmatch(user_id):
        return "tg-" + user_id
    return "u-" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _strict_generation(value: Any) -> int:
    """Validate a durable authorization epoch without accepting bools or floats."""
    if type(value) is not int or value < 0:
        raise TenantStoreError("Corrupt authorization generation")
    return value


def _safe_failure_kind(exc: Exception, operation: str) -> str:
    """Classify a storage failure without exposing driver-provided text."""
    error_type = type(exc).__name__
    if operation == "start_user_result" and error_type in {"KeyError", "TypeError"}:
        return "invalid_update_result"
    try:
        code = exc.code
    except Exception:
        code = None
    if type(code) is int:
        known_codes = {13: "authorization", 18: "authentication", 11000: "duplicate_key"}
        if code in known_codes:
            return known_codes[code]
    return {
        "ModuleNotFoundError": "dependency_unavailable",
        "ImportError": "dependency_unavailable",
        "ServerSelectionTimeoutError": "server_selection_timeout",
        "NetworkTimeout": "network_timeout",
        "WaitQueueTimeoutError": "connection_pool_timeout",
        "AutoReconnect": "connection",
        "ConnectionFailure": "connection",
        "AuthenticationFailure": "authentication",
        "ConfigurationError": "configuration",
        "InvalidURI": "configuration",
        "DuplicateKeyError": "duplicate_key",
        "WriteError": "write_error",
        "WriteConcernError": "write_concern",
        "OperationFailure": "mongo_operation_failure",
    }.get(error_type, "unexpected_error")


class MongoTenantStore:
    """Durable per-user data store; owner identity comes only from configuration."""
    def __init__(self, home: Path, owner_user_id: str | int, database: Any = None):
        self.home = Path(home)
        self.owner_user_id = _uid(owner_user_id)
        try:
            settings = mongo_credentials._settings(self.home)
            self.database = database if database is not None else mongo_credentials._collection(settings).database
            self._profiles = self.database["hermes_tenant_profiles"]
            self._data = self.database["hermes_tenant_data"]
            self._usage = self.database["hermes_tenant_usage"]
            self._groups = self.database["hermes_group_settings"]
            pinned = self._profiles.find_one({"_id": "owner"})
            if pinned and _uid(pinned.get("user_id", "")) != self.owner_user_id:
                raise TenantStoreError("Configured owner does not match durable owner identity")
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Cannot initialize tenant storage") from None

    def _call(self, callback, *, operation: str = "tenant_store"):
        try:
            return callback()
        except TenantStoreError:
            raise
        except Exception as exc:
            # Driver messages may include URIs, hosts, query values, or user data.
            # Log only static context and classifications so WARNING-level logs
            # explain the failure without leaking the underlying payload.
            try:
                code = exc.code
            except Exception:
                code = None
            safe_code = code if type(code) is int else None
            logger.warning(
                "Tenant storage operation failed; operation=%s error_type=%s "
                "failure_kind=%s error_code=%s",
                operation,
                type(exc).__name__,
                _safe_failure_kind(exc, operation),
                safe_code,
            )
            raise TenantStoreError("Tenant storage operation failed") from None

    def _is_owner(self, actor: Principal) -> bool:
        if not isinstance(actor, Principal):
            raise TenantStoreError("Authenticated principal required")
        return _uid(actor.user_id) == self.owner_user_id

    def _require_active(self, actor: Principal) -> None:
        if self._is_owner(actor):
            return
        access = self.get_access(actor.user_id)
        if not access["allowed"] or access["banned"]:
            raise TenantStoreError("Tenant access denied")

    def _profile_for(self, user_id):
        return self.ensure_owner() if _uid(user_id) == self.owner_user_id else self.provision_user(user_id)

    def principal_tenant(self, principal: Principal) -> str:
        if not isinstance(principal, Principal):
            raise TenantStoreError("Authenticated principal required")
        uid = _uid(principal.user_id)
        return "owner" if uid == self.owner_user_id else _tenant_id(uid)

    def provision_user(self, user_id: str | int) -> dict:
        uid = _uid(user_id)
        if uid == self.owner_user_id:
            return self.ensure_owner()
        tenant = _tenant_id(uid)
        doc = {"_id": tenant, "user_id": uid, "profile_id": tenant, "allowed": False,
               "banned": False, "credits": 0, "weight": 1, "start_count": 0}
        return self._call(lambda: self._provision(doc))

    def _provision(self, doc):
        self._profiles.update_one({"_id": doc["_id"]}, {"$setOnInsert": doc}, upsert=True)
        return copy.deepcopy(self._profiles.find_one({"_id": doc["_id"]}))

    def ensure_owner(self) -> dict:
        tenant = {"_id": "owner", "user_id": self.owner_user_id, "profile_id": "owner",
                  "allowed": True, "banned": False, "credits": 0, "weight": 1, "start_count": 0}
        def ensure():
            current = self._profiles.find_one({"_id": "owner"})
            if current and _uid(current.get("user_id", "")) != self.owner_user_id:
                raise TenantStoreError("Configured owner does not match durable owner identity")
            return self._provision(tenant)
        return self._call(ensure)

    def get(self, principal: Principal, kind: str, key: str) -> Any:
        tenant = self.principal_tenant(principal)
        if not isinstance(kind, str) or not isinstance(key, str) or not kind or not key:
            raise TenantStoreError("Invalid tenant data address")
        owner = self._is_owner(principal)
        if kind == "provider_credentials":
            raise TenantStoreError("Provider credentials are not readable through tenant data API")
        if kind not in _PUBLIC_KINDS and not (owner and kind in _PRIVILEGED_KINDS):
            raise TenantStoreError("Tenant data access denied")
        if kind in _PRIVILEGED_KINDS and tenant != "owner" and not owner:
            raise TenantStoreError("Tenant data access denied")
        self._require_active(principal)
        if kind == "usage":
            raise TenantStoreError("Usage ledger is not readable through tenant data API")
        doc = self._call(lambda: self._data.find_one({"_id": self._data_id(tenant, kind, key)}))
        return copy.deepcopy(doc.get("payload")) if doc else None

    def get_versioned(self, principal: Principal, kind: str, key: str) -> tuple[Any, int]:
        """Read one tenant-owned payload and its CAS revision under the same authorization checks."""
        tenant = self.principal_tenant(principal)
        if not isinstance(kind, str) or not isinstance(key, str) or not kind or not key:
            raise TenantStoreError("Invalid tenant data address")
        owner = self._is_owner(principal)
        if kind == "provider_credentials":
            raise TenantStoreError("Provider credentials are not readable through tenant data API")
        if kind not in _PUBLIC_KINDS and not (owner and kind in _PRIVILEGED_KINDS):
            raise TenantStoreError("Tenant data access denied")
        if kind in _PRIVILEGED_KINDS and tenant != "owner" and not owner:
            raise TenantStoreError("Tenant data access denied")
        self._require_active(principal)
        if kind == "usage":
            raise TenantStoreError("Usage ledger is not readable through tenant data API")
        doc = self._call(lambda: self._data.find_one({"_id": self._data_id(tenant, kind, key)}))
        return (copy.deepcopy(doc.get("payload")), int(doc.get("revision", 0))) if doc else (None, 0)

    def list_telegram_continuations(self, index_key: str = "telegram-continuations-v1") -> list[dict]:
        """Internal scheduler index: return only non-owner Telegram continuation records.

        This deliberately is not a generic tenant enumeration API. The scheduler must still
        re-check each record's user/chat authorization before claiming or executing it. A
        profile-root suffix prevents schedulers sharing one Mongo database from crossing profiles.
        """
        if not isinstance(index_key, str) or not re.fullmatch(
            r"telegram-continuations-v1(?::[a-f0-9]{24})?", index_key,
        ):
            raise TenantStoreError("Invalid Telegram continuation index")
        rows = self._call(lambda: list(self._data.find({
            "kind": "profile", "key": index_key, "tenant_id": {"$ne": "owner"},
        })))
        records = []
        for row in rows:
            tenant = row.get("tenant_id")
            if not isinstance(tenant, str) or not tenant or tenant == "owner":
                continue
            profile = self._call(lambda tenant=tenant: self._profiles.find_one({"_id": tenant}))
            if not isinstance(profile, dict):
                continue
            try:
                user_id = _uid(profile.get("user_id", ""))
                revision = row.get("revision", 0)
                if type(revision) is not int or revision < 0:
                    continue
                records.append({
                    "user_id": user_id,
                    "payload": copy.deepcopy(row.get("payload")),
                    "revision": int(row.get("revision", 0)),
                })
            except (TenantStoreError, TypeError, ValueError):
                continue
        return records

    def put(self, principal: Principal, kind: str, key: str, payload: Any,
            expected_revision: int | None = None) -> int:
        tenant = self.principal_tenant(principal)
        if not isinstance(kind, str) or not isinstance(key, str) or not kind or not key:
            raise TenantStoreError("Invalid tenant data address")
        owner = self._is_owner(principal)
        if kind == "provider_credentials" or kind not in _PUBLIC_KINDS | _PRIVILEGED_KINDS:
            raise TenantStoreError("Tenant data access denied")
        if kind in _PRIVILEGED_KINDS and not owner:
            raise TenantStoreError("Tenant data access denied")
        if kind in {"usage", "credits", "weight", "providers"}:
            raise TenantStoreError("Tenant data kind is not user-writable")
        self._require_active(principal)
        _validate_payload(payload)
        return self._put(tenant, kind, key, payload, expected_revision)

    @staticmethod
    def _data_id(tenant, kind, key):
        return hashlib.sha256((tenant + "\0" + kind + "\0" + key).encode()).hexdigest()

    def _put(self, tenant, kind, key, payload, expected_revision=None):
        if not isinstance(key, str) or not key or len(key) > 512 or key.startswith("$") or "\x00" in key or key == "_id":
            raise TenantStoreError("Invalid tenant data address")
        ident = self._data_id(tenant, kind, key)
        def operation():
            old = self._data.find_one({"_id": ident})
            revision = old.get("revision", 0) if old else 0
            if expected_revision is not None and expected_revision != revision:
                raise TenantStoreError("Tenant data revision conflict")
            if old and old.get("payload") == payload:
                return revision
            new_revision = revision + 1
            doc = {"_id": ident, "tenant_id": tenant, "kind": kind, "key": key,
                   "payload": copy.deepcopy(payload), "revision": new_revision}
            if old:
                result = self._data.replace_one({"_id": ident, "revision": revision}, doc)
                if result.matched_count != 1:
                    raise TenantStoreError("Tenant data revision conflict")
            else:
                self._data.insert_one(doc)
            return new_revision
        return self._call(operation)

    def _owner_action(self, actor):
        if not self._is_owner(actor):
            raise TenantStoreError("Owner access required")

    def allow_user(self, actor: Principal, target: str | int, allowed: bool) -> dict:
        self._owner_action(actor)
        if _uid(target) == self.owner_user_id and allowed is not True:
            raise TenantStoreError("Owner access cannot be changed")
        if type(allowed) is not bool:
            raise TenantStoreError("Invalid access state")
        return self._set_profile(target, {"allowed": allowed})

    def ban_user(self, actor: Principal, target: str | int, banned: bool) -> dict:
        self._owner_action(actor)
        if _uid(target) == self.owner_user_id and banned is not False:
            raise TenantStoreError("Owner cannot be banned")
        if type(banned) is not bool:
            raise TenantStoreError("Invalid ban state")
        return self._set_profile(target, {"banned": banned})

    def _set_profile(self, target, fields):
        doc = self.provision_user(target)
        def update():
            current = self._profiles.find_one({"_id": doc["_id"]})
            if current is None:
                raise TenantStoreError("Tenant profile not found")
            _strict_generation(current.get("authorization_generation", 0))
            self._profiles.update_one(
                {"_id": doc["_id"]},
                {"$inc": {"authorization_generation": 1}, "$set": fields},
            )
            return self._profiles.find_one({"_id": doc["_id"]})
        return self._call(update)

    def authorization_generation(self, user_id: str | int) -> int:
        """Return the durable per-user authorization epoch (legacy records start at zero)."""
        doc = self._profile_for(user_id)
        return _strict_generation(doc.get("authorization_generation", 0))

    def get_access(self, user_id: str | int) -> dict:
        doc = self._profile_for(user_id)
        return {key: doc[key] for key in ("allowed", "banned")}

    def start_user(self, user_id: str | int) -> dict:
        uid = _uid(user_id)
        doc = self._profile_for(uid)
        def update_counter():
            from pymongo import ReturnDocument
            return self._profiles.find_one_and_update(
                {"_id": doc["_id"]},
                {"$inc": {"start_count": 1}},
                return_document=ReturnDocument.AFTER,
            )

        updated = self._call(update_counter, operation="start_user_counter_update")
        return self._call(
            lambda: {
                "first_start": updated["start_count"] == 1,
                "start_count": updated["start_count"],
            },
            operation="start_user_result",
        )

    def set_group_settings(self, actor: Principal, group_id: str | int, settings: dict) -> dict:
        self._require_active(actor)
        if not isinstance(settings, dict) or not set(settings) <= _SAFE_GROUP_FIELDS:
            raise TenantStoreError("Unsupported group setting")
        ident = str(group_id)
        if not ident or len(ident) > 512:
            raise TenantStoreError("Invalid group identity")
        def update():
            current = self._groups.find_one({"_id": ident})
            _strict_generation((current or {}).get("authorization_generation", 0))
            self._groups.update_one(
                {"_id": ident},
                {"$inc": {"authorization_generation": 1}, "$set": copy.deepcopy(settings)},
                upsert=True,
            )
            result = copy.deepcopy(self._groups.find_one({"_id": ident}))
            result.pop("authorization_generation", None)
            return result
        return self._call(update)

    def group_authorization_generation(self, group_id: str | int) -> int:
        """Return the durable per-group authorization epoch; an absent group is zero."""
        ident = str(group_id)
        if not ident or len(ident) > 512:
            raise TenantStoreError("Invalid group identity")
        doc = self._call(lambda: self._groups.find_one({"_id": ident}))
        return _strict_generation((doc or {}).get("authorization_generation", 0))

    def get_group_settings(self, actor: Principal, group_id: str | int) -> dict | None:
        self._require_active(actor)
        result = self._call(lambda: copy.deepcopy(self._groups.find_one({"_id": str(group_id)})))
        if result is not None:
            result.pop("authorization_generation", None)
        return result

    def record_usage(self, principal: Principal, event_id: str, units: float, cost: float) -> bool:
        tenant = self.principal_tenant(principal)
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            raise TenantStoreError("Invalid usage event identity")
        self._require_active(principal)
        if isinstance(units, bool) or not isinstance(units, (int, float)) or not math.isfinite(units) or units < 0 or isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise TenantStoreError("Invalid usage values")
        ident = hashlib.sha256((tenant + "\0" + event_id).encode()).hexdigest()
        def insert():
            from pymongo.errors import DuplicateKeyError
            try:
                self._usage.insert_one({"_id": ident, "tenant_id": tenant, "event_id": event_id, "units": units, "cost": cost})
                return True
            except DuplicateKeyError:
                existing = self._usage.find_one({"_id": ident})
                if existing and existing.get("units") == units and existing.get("cost") == cost:
                    return False
                raise TenantStoreError("Usage event identity conflict")
        try:
            return self._call(insert)
        except TenantStoreError:
            # Duplicate-key errors from compatible Mongo implementations are idempotent too.
            existing = self._call(lambda: self._usage.find_one({"_id": ident}))
            if existing and existing.get("units") == units and existing.get("cost") == cost:
                return False
            if existing:
                raise TenantStoreError("Usage event identity conflict")
            raise

    def owner_put_tenant(self, actor: Principal, tenant_id: str, kind: str, key: str, payload: Any) -> int:
        self._owner_action(actor)
        if not isinstance(tenant_id, str) or not tenant_id or not isinstance(kind, str) or kind not in _PUBLIC_KINDS | _PRIVILEGED_KINDS or kind == "provider_credentials":
            raise TenantStoreError("Invalid migration data address")
        return self._put(tenant_id, kind, key, payload)

    def enumerate_tenant_data(self, actor: Principal, tenant_id: str) -> list[dict]:
        self._owner_action(actor)
        if not isinstance(tenant_id, str) or not tenant_id:
            raise TenantStoreError("Invalid tenant identity")
        return self._call(lambda: [copy.deepcopy({k: v for k, v in item.items() if k != "_id"})
                                   for item in self._data.find({"tenant_id": tenant_id}) if item.get("kind") != "provider_credentials"])


def _validate_payload(value):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TenantStoreError("Tenant payload must be finite JSON data")
        return
    if isinstance(value, list):
        for item in value:
            _validate_payload(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or key.startswith("$") or "\x00" in key or key == "_id":
                raise TenantStoreError("Invalid tenant payload key")
            _validate_payload(item)
        return
    raise TenantStoreError("Tenant payload must be JSON-compatible")
