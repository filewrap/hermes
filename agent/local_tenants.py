"""SQLite-backed canonical tenant storage (Mongo is not imported or contacted)."""
from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError, _strict_generation, _tenant_id, _uid


@dataclass(frozen=True)
class ScopedPrincipal(Principal):
    """Identity bound to a trusted transport chat scope for one isolated request."""
    chat_type: str
    chat_id: str


class _Result:
    def __init__(self, matched_count=0, modified_count=0, upserted_id=None):
        self.matched_count = matched_count
        self.modified_count = modified_count
        self.upserted_id = upserted_id


class _SQLiteCollection:
    """Small strict JSON document collection, with each mutation transactional."""
    def __init__(self, path: Path, name: str, lock: threading.RLock):
        self.path, self.name, self.lock = path, name, lock

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _json(doc):
        return json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def _decode(self, raw):
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or "_id" not in doc:
                raise ValueError
            return doc
        except Exception:
            raise TenantStoreError("Corrupt local tenant record") from None

    def find_one(self, query, projection=None):
        _validate_query(query)
        ident = query.get("_id") if set(query) == {"_id"} and isinstance(query.get("_id"), str) else None
        with self.lock, closing(self._connect()) as db:
            if ident is not None:
                rows = db.execute("SELECT body FROM tenant_documents WHERE collection=? AND id=?", (self.name, ident)).fetchall()
            else:
                rows = db.execute("SELECT body FROM tenant_documents WHERE collection=?", (self.name,)).fetchall()
        doc = next((d for raw, in rows if _matches(d := self._decode(raw), query)), None)
        if doc is not None and projection:
            if any(bool(v) for v in projection.values()):
                selected = {k: doc[k] for k, v in projection.items() if v and k in doc}
                if projection.get("_id", 1) and "_id" in doc:
                    selected.setdefault("_id", doc["_id"])
                return selected
            return {k: v for k, v in doc.items() if k not in projection or projection[k]}
        return doc

    def find(self, query):
        _validate_query(query)
        with self.lock, closing(self._connect()) as db:
            rows = db.execute("SELECT body FROM tenant_documents WHERE collection=? ORDER BY id", (self.name,)).fetchall()
        return [d for raw, in rows if _matches(d := self._decode(raw), query)]

    def insert_one(self, doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("_id"), str):
            raise TenantStoreError("Invalid local tenant record")
        encoded = self._json(doc)
        with self.lock, closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)", (self.name, doc["_id"], encoded))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                raise TenantStoreError("Duplicate local tenant record") from None
        return _Result(upserted_id=doc["_id"])

    def update_one(self, query, update, upsert=False):
        _validate_query(query)
        _validate_update(update)
        with self.lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,body FROM tenant_documents WHERE collection=?", (self.name,)).fetchall()
            found = next(((ident, self._decode(raw)) for ident, raw in row if _matches(self._decode(raw), query)), None)
            if found:
                ident, old = found
                new = _apply_update(old, update, inserting=False)
                db.execute("UPDATE tenant_documents SET body=? WHERE collection=? AND id=?", (self._json(new), self.name, ident))
                db.commit()
                return _Result(1, int(new != old))
            if upsert:
                doc = {k: v for k, v in query.items() if not k.startswith("$") and not isinstance(v, dict)}
                if "_id" not in doc:
                    raise TenantStoreError("Upsert requires an exact _id")
                new = _apply_update(doc, update, inserting=True)
                if not isinstance(new.get("_id"), str):
                    raise TenantStoreError("Invalid local tenant record")
                db.execute("INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)", (self.name, new["_id"], self._json(new)))
                db.commit()
                return _Result(upserted_id=new["_id"])
            db.commit()
            return _Result()

    def replace_one(self, query, replacement, upsert=False):
        _validate_query(query)
        if not isinstance(replacement, dict) or not isinstance(replacement.get("_id"), str):
            raise TenantStoreError("Invalid local tenant record")
        with self.lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,body FROM tenant_documents WHERE collection=?", (self.name,)).fetchall()
            found = next(((i, self._decode(raw)) for i, raw in rows if _matches(self._decode(raw), query)), None)
            if not found:
                if upsert:
                    ident = query.get("_id")
                    if not isinstance(ident, str) or replacement["_id"] != ident:
                        db.rollback(); raise TenantStoreError("Upsert requires matching exact _id")
                    db.execute("INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)", (self.name, ident, self._json(replacement)))
                    db.commit(); return _Result(upserted_id=ident)
                db.commit(); return _Result()
            ident, old = found
            if ident != replacement["_id"]:
                db.rollback(); raise TenantStoreError("Cannot change tenant record identity")
            db.execute("UPDATE tenant_documents SET body=? WHERE collection=? AND id=?", (self._json(replacement), self.name, ident))
            db.commit()
            return _Result(1, int(old != replacement))

    def find_one_and_update(self, query, update, return_document=None):
        _validate_query(query); _validate_update(update)
        with self.lock, closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,body FROM tenant_documents WHERE collection=?", (self.name,)).fetchall()
            found = next(((i, self._decode(raw)) for i, raw in rows if _matches(self._decode(raw), query)), None)
            if not found:
                db.commit(); return None
            ident, old = found
            new = _apply_update(old, update, inserting=False)
            db.execute("UPDATE tenant_documents SET body=? WHERE collection=? AND id=?", (self._json(new), self.name, ident))
            db.commit()
            return new


def _validate_query(query):
    if not isinstance(query, dict): raise TenantStoreError("Unsupported tenant query")
    for key, value in query.items():
        if key in ("$or", "$and"):
            if not isinstance(value, list) or not value: raise TenantStoreError("Unsupported tenant query")
            for sub in value: _validate_query(sub)
        elif key.startswith("$"):
            raise TenantStoreError("Unsupported tenant query operator")
        elif isinstance(value, dict):
            for op, operand in value.items():
                if op not in ("$ne", "$in", "$exists"): raise TenantStoreError("Unsupported tenant query operator")
                if op == "$in" and not isinstance(operand, (list, tuple, set)): raise TenantStoreError("Invalid tenant query")
                if op == "$exists" and type(operand) is not bool: raise TenantStoreError("Invalid tenant query")


def _matches(doc, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, x) for x in expected): return False
        elif key == "$and":
            if not all(_matches(doc, x) for x in expected): return False
        else:
            exists = key in doc
            actual = doc.get(key)
            if isinstance(expected, dict):
                for op, operand in expected.items():
                    if op == "$ne" and exists and actual == operand: return False
                    if op == "$in" and actual not in operand: return False
                    if op == "$exists" and exists != operand: return False
            elif not exists or actual != expected: return False
    return True


def _validate_update(update):
    if not isinstance(update, dict) or not update or any(k not in ("$set", "$setOnInsert", "$inc") for k in update):
        raise TenantStoreError("Unsupported tenant update")
    for op, values in update.items():
        if not isinstance(values, dict): raise TenantStoreError("Invalid tenant update")
        for k, v in values.items():
            if not isinstance(k, str) or not k or k.startswith("$") or "\x00" in k or (k == "_id" and op != "$setOnInsert"): raise TenantStoreError("Invalid tenant update field")
            if op == "$inc" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
                raise TenantStoreError("Invalid tenant increment")
            if op != "$inc":
                try: json.dumps(v, allow_nan=False)
                except (TypeError, ValueError): raise TenantStoreError("Invalid tenant update value") from None


def _apply_update(doc, update, inserting):
    out = dict(doc)
    for op, fields in update.items():
        if op == "$set" or (op == "$setOnInsert" and inserting): out.update(fields)
        elif op == "$inc":
            for k, v in fields.items():
                old = out.get(k, 0)
                if isinstance(old, bool) or not isinstance(old, (int, float)): raise TenantStoreError("Invalid tenant numeric field")
                value = old + v
                if not math.isfinite(value): raise TenantStoreError("Invalid tenant numeric field")
                out[k] = value
    return out


class _LocalDatabase:
    def __init__(self, path, lock):
        self.path, self.lock = path, lock
        self._collections = {}
    def __getitem__(self, name):
        if not isinstance(name, str) or not name:
            raise TenantStoreError("Invalid local collection")
        if name not in self._collections:
            self._collections[name] = _SQLiteCollection(self.path, name, self.lock)
        return self._collections[name]


class LocalTenantStore(MongoTenantStore):
    """Same authorization/data API as MongoTenantStore; SQLite is canonical."""
    def __init__(self, home: Path, owner_user_id: str | int, *, allowed_user_ids=None):
        self.home = Path(home)
        self.owner_user_id = _uid(owner_user_id)
        requested_home = self.home.absolute()
        if any(part.is_symlink() for part in (requested_home, *requested_home.parents)):
            raise TenantStoreError("Unsafe local tenant storage path")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.home = self.home.resolve()
        self.database_path = self.home / "tenant-state.db"
        if self.database_path.is_symlink() or any(Path(str(self.database_path) + suffix).is_symlink() for suffix in ("-wal", "-shm")):
            raise TenantStoreError("Unsafe local tenant storage path")
        self._lock = threading.RLock()
        try:
            with closing(sqlite3.connect(self.database_path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("CREATE TABLE IF NOT EXISTS tenant_documents(collection TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)), PRIMARY KEY(collection,id))")
                db.execute("CREATE TABLE IF NOT EXISTS tenant_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.commit()
            self.database_path.chmod(0o600)
            for suffix in ("-wal", "-shm"):
                p = Path(str(self.database_path) + suffix)
                if p.exists(): p.chmod(0o600)
            self.database = _LocalDatabase(self.database_path, self._lock)
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
            raise TenantStoreError("Cannot initialize local tenant storage") from None
        # Pin identity before any caller can write owner-scoped records, even
        # when this backend is used directly instead of through the facade.
        self.ensure_owner()
        if allowed_user_ids is not None:
            # Atomic one-time seed: current allowlist config must never undo a ban/revocation.
            try:
                ids = {_uid(uid) for uid in allowed_user_ids if _uid(uid) != self.owner_user_id}
                with closing(sqlite3.connect(self.database_path, timeout=30, isolation_level=None)) as db:
                    db.execute("PRAGMA busy_timeout=30000")
                    db.execute("BEGIN IMMEDIATE")
                    seeded = db.execute("SELECT 1 FROM tenant_metadata WHERE key='initial_allowlist_seeded'").fetchone()
                    if not seeded:
                        for uid in ids:
                            ident = _tenant_id(uid)
                            profile = {"_id": ident, "user_id": uid, "profile_id": ident, "allowed": True,
                                       "banned": False, "credits": 0, "weight": 1, "start_count": 0}
                            db.execute("INSERT OR IGNORE INTO tenant_documents(collection,id,body) VALUES(?,?,?)",
                                       ("hermes_tenant_profiles", ident, self._profiles._json(profile)))
                        db.execute("INSERT INTO tenant_metadata(key,value) VALUES('initial_allowlist_seeded','1')")
                    db.commit()
            except Exception:
                raise TenantStoreError("Cannot initialize local tenant storage") from None

    def ensure_owner(self) -> dict:
        tenant = {"_id": "owner", "user_id": self.owner_user_id, "profile_id": "owner",
                  "allowed": True, "banned": False, "credits": 0, "weight": 1, "start_count": 0}

        def read_owner(db):
            row = db.execute("SELECT body FROM tenant_documents WHERE collection=? AND id='owner'",
                             ("hermes_tenant_profiles",)).fetchone()
            if row is None:
                return None
            current = self._profiles._decode(row[0])
            if _uid(current.get("user_id", "")) != self.owner_user_id:
                raise TenantStoreError("Configured owner does not match durable owner identity")
            return current

        try:
            with self._lock, closing(sqlite3.connect(self.database_path, timeout=30, isolation_level=None)) as db:
                db.execute("PRAGMA busy_timeout=30000")
                # Authorization runs for every file part. Existing owners need
                # a fresh read, not contention for SQLite's single writer slot.
                current = read_owner(db)
                if current is not None:
                    return current
                db.execute("BEGIN IMMEDIATE")
                # Another process may initialize the owner before the lock is
                # acquired. Recheck under the write transaction before inserting.
                current = read_owner(db)
                if current is not None:
                    db.commit(); return current
                db.execute("INSERT INTO tenant_documents(collection,id,body) VALUES(?,?,?)",
                           ("hermes_tenant_profiles", "owner", self._profiles._json(tenant)))
                db.commit(); return tenant
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Cannot initialize local tenant storage") from None

    @staticmethod
    def _scope_key(scope: str | int) -> str:
        """Canonicalize a Telegram DM or exact group chat ID scope."""
        if isinstance(scope, str) and scope.strip().casefold() == "dm":
            return "dm"
        value = _uid(scope)
        if not re.fullmatch(r"-?[0-9]+", value):
            raise TenantStoreError("Invalid Telegram permission scope")
        return "group:" + value

    @staticmethod
    def _event_scope(chat_type: str, chat_id: str | int) -> str | None:
        kind = str(chat_type or "").strip().casefold()
        if kind in {"dm", "private", "direct"}:
            return "dm"
        if kind in {"group", "forum"}:
            try:
                return LocalTenantStore._scope_key(chat_id)
            except TenantStoreError:
                return None
        return None

    def principal_for_scope(self, user_id: str | int, chat_type: str, chat_id: str | int) -> ScopedPrincipal:
        """Bind an authenticated transport identity to its exact DM or group scope."""
        kind = str(chat_type or "").strip().casefold()
        if kind not in {"dm", "private", "direct", "group", "forum"}:
            raise TenantStoreError("Unsupported Telegram tenant scope")
        if self._event_scope(kind, chat_id) is None:
            raise TenantStoreError("Invalid Telegram tenant scope")
        return ScopedPrincipal(_uid(user_id), kind, str(chat_id))

    def _require_active(self, actor: Principal) -> None:
        if isinstance(actor, ScopedPrincipal) and not self._is_owner(actor):
            access = self.access_for_scope(actor.user_id, actor.chat_type, actor.chat_id)
            if not access["allowed"] or access["banned"]:
                raise TenantStoreError("Tenant access denied")
            return
        super()._require_active(actor)

    @staticmethod
    def _check_scoped_group(actor: Principal, group_id: str | int) -> None:
        if isinstance(actor, ScopedPrincipal):
            if (str(actor.chat_type).strip().casefold() not in {"group", "forum"}
                    or str(actor.chat_id) != str(group_id)):
                raise TenantStoreError("Scoped principal cannot access another group")

    def set_group_settings(self, actor: Principal, group_id: str | int, settings: dict) -> dict:
        self._check_scoped_group(actor, group_id)
        return super().set_group_settings(actor, group_id, settings)

    def get_group_settings(self, actor: Principal, group_id: str | int) -> dict | None:
        self._check_scoped_group(actor, group_id)
        return super().get_group_settings(actor, group_id)

    def _mutate_profile(self, user_id: str | int, mutate, *, authorization_change: bool = False) -> dict:
        """Read/modify/write one profile atomically in SQLite."""
        uid = _uid(user_id)
        ident = "owner" if uid == self.owner_user_id else _tenant_id(uid)
        try:
            with self._lock, closing(sqlite3.connect(self.database_path, timeout=30, isolation_level=None)) as db:
                db.execute("PRAGMA busy_timeout=30000")
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT body FROM tenant_documents WHERE collection=? AND id=?",
                    ("hermes_tenant_profiles", ident),
                ).fetchone()
                if not row:
                    db.rollback()
                    raise TenantStoreError("Started Telegram user not found")
                profile = self._profiles._decode(row[0])
                permission_fields = ("allowed", "banned", "allowed_scopes", "sudo_scopes")
                prior_permissions = {key: copy.deepcopy(profile.get(key)) for key in permission_fields}
                mutate(profile)
                changed_permissions = any(profile.get(key) != value for key, value in prior_permissions.items())
                if authorization_change or changed_permissions:
                    generation = _strict_generation(profile.get("authorization_generation", 0))
                    profile["authorization_generation"] = generation + 1
                db.execute(
                    "UPDATE tenant_documents SET body=? WHERE collection=? AND id=?",
                    (self._profiles._json(profile), "hermes_tenant_profiles", ident),
                )
                db.commit()
                return profile
        except TenantStoreError:
            raise
        except Exception:
            raise TenantStoreError("Local tenant profile update failed") from None

    def allow_user(self, actor: Principal, target: str | int, allowed: bool) -> dict:
        """Preserve inherited allow semantics; a revocation also removes scoped grants."""
        if type(allowed) is not bool:
            raise TenantStoreError("Invalid access state")
        if allowed:
            return super().allow_user(actor, target, True)
        self._owner_action(actor)
        uid = _uid(target)
        if uid == self.owner_user_id:
            raise TenantStoreError("Owner access cannot be changed")
        self.provision_user(uid)

        def revoke(profile):
            profile["allowed"] = False
            profile["allowed_scopes"] = []
            profile["sudo_scopes"] = []

        return self._mutate_profile(uid, revoke, authorization_change=True)

    def record_started_identity(self, user_id: str | int, username: str | None, name: str | None) -> dict:
        """Record Telegram identity metadata after the authenticated /start increment."""
        uid = _uid(user_id)
        clean_username = str(username or "").strip().removeprefix("@").casefold()
        if clean_username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", clean_username):
            raise TenantStoreError("Invalid Telegram username")
        clean_name = str(name or "").replace("\x00", "").strip()[:256]

        def update(profile):
            if _uid(profile.get("user_id", "")) != uid or int(profile.get("start_count", 0) or 0) < 1:
                raise TenantStoreError("Telegram user has not started")
            profile["username"] = clean_username or None
            profile["name"] = clean_name

        profile = self._mutate_profile(uid, update)
        return {key: profile.get(key) for key in ("user_id", "name", "username", "start_count")}

    def resolve_started_user(self, target: str | int) -> dict | None:
        """Resolve only an already-started Telegram ID/username; never provision on lookup."""
        raw = _uid(target)
        numeric = bool(re.fullmatch(r"[0-9]+", raw))
        username = raw.strip().removeprefix("@").casefold()
        if not numeric and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            return None
        matches = []
        try:
            for profile in self._profiles.find({}):
                try:
                    started = int(profile.get("start_count", 0) or 0) > 0
                except (TypeError, ValueError):
                    started = False
                if not started:
                    continue
                if (numeric and str(profile.get("user_id", "")) == raw) or (
                    not numeric and str(profile.get("username") or "").removeprefix("@").casefold() == username
                ):
                    matches.append(profile)
        except Exception:
            raise TenantStoreError("Cannot resolve started Telegram user") from None
        if len(matches) > 1:
            raise TenantStoreError("Telegram username matches multiple started users")
        if not matches:
            return None
        profile = matches[0]
        return {key: profile.get(key) for key in ("user_id", "name", "username", "start_count")}

    def _require_scope_admin(self, actor: Principal, scope_key: str) -> None:
        if self._is_owner(actor):
            return
        if not isinstance(actor, ScopedPrincipal):
            raise TenantStoreError("Scoped transport identity required")
        actor_scope = self._event_scope(actor.chat_type, actor.chat_id)
        if actor_scope != scope_key:
            raise TenantStoreError("Scoped administrator cannot grant across chats")
        access = self.access_for_scope(actor.user_id, actor.chat_type, actor.chat_id)
        if access["banned"] or not access["sudo"]:
            raise TenantStoreError("Owner or scoped administrator access required")

    def grant_sudo(self, actor: Principal, target: str | int, scope: str | int = "dm") -> dict:
        """Grant scoped access and admin capability to a started identity only."""
        scope_key = self._scope_key(scope)
        self._require_scope_admin(actor, scope_key)
        resolved = self.resolve_started_user(target)
        if resolved is None:
            raise TenantStoreError("Started Telegram user not found")
        uid = _uid(resolved["user_id"])

        def grant(profile):
            if int(profile.get("start_count", 0) or 0) < 1:
                raise TenantStoreError("Telegram user has not started")
            if bool(profile.get("banned", False)):
                raise TenantStoreError("Banned Telegram user cannot receive a grant")
            allowed_scopes = list(profile.get("allowed_scopes") or [])
            sudo_scopes = list(profile.get("sudo_scopes") or [])
            if scope_key not in allowed_scopes:
                allowed_scopes.append(scope_key)
            if scope_key not in sudo_scopes:
                sudo_scopes.append(scope_key)
            profile["allowed_scopes"] = allowed_scopes
            profile["sudo_scopes"] = sudo_scopes

        return self._mutate_profile(uid, grant, authorization_change=True)

    def revoke_sudo(self, actor: Principal, target: str | int, scope: str | int = "dm") -> dict:
        """Remove only the matching scoped grant; a separate global allow remains intact."""
        scope_key = self._scope_key(scope)
        self._require_scope_admin(actor, scope_key)
        resolved = self.resolve_started_user(target)
        if resolved is None:
            raise TenantStoreError("Started Telegram user not found")
        uid = _uid(resolved["user_id"])

        def revoke(profile):
            profile["allowed_scopes"] = [x for x in (profile.get("allowed_scopes") or []) if x != scope_key]
            profile["sudo_scopes"] = [x for x in (profile.get("sudo_scopes") or []) if x != scope_key]

        return self._mutate_profile(uid, revoke, authorization_change=True)

    def access_for_scope(self, user_id: str | int, chat_type: str, chat_id: str | int) -> dict:
        """Resolve effective access in one chat scope; bans override global and scoped grants."""
        uid = _uid(user_id)
        profile = self._profile_for(uid)
        kind = str(chat_type or "").strip().casefold()
        scope_key = self._event_scope(kind, chat_id)
        started = False
        try:
            started = int(profile.get("start_count", 0) or 0) > 0
        except (TypeError, ValueError):
            pass
        banned = bool(profile.get("banned", False))
        owner = uid == self.owner_user_id
        allowed = bool(profile.get("allowed", False)) or bool(
            scope_key and scope_key in (profile.get("allowed_scopes") if isinstance(profile.get("allowed_scopes"), list) else [])
        ) or owner
        sudo = owner or bool(profile.get("allowed", False)) or bool(
            scope_key and scope_key in (profile.get("sudo_scopes") if isinstance(profile.get("sudo_scopes"), list) else [])
        )
        # Group allow_all can only add access, never revoke an independent grant
        # or override a ban. Keep fresh tenant reads above, but avoid reparsing
        # unrelated YAML for every download-part authorization check.
        if not allowed and not banned and scope_key and kind in {"group", "forum"}:
            try:
                from plugins.platforms.telegram.telegram_chat_settings import load_chat_settings
                allow_all = load_chat_settings(self.home, "group", str(chat_id)).get("allow_all") is True
            except Exception:
                allow_all = False
            allowed = allowed or allow_all
        if banned:
            allowed = False
            sudo = False
        return {"allowed": allowed, "banned": banned, "sudo": sudo, "started": started}

    def list_scope_users(self, chat_type: str, chat_id: str | int) -> list[dict]:
        """List active, explicitly allowed identities in this exact scope (excluding owner)."""
        kind = str(chat_type or "").strip().casefold()
        scope_key = self._event_scope(kind, chat_id)
        if scope_key is None:
            raise TenantStoreError("Invalid Telegram permission scope")
        try:
            profiles = self._profiles.find({})
        except Exception:
            raise TenantStoreError("Cannot list scoped Telegram users") from None
        result = []
        for profile in profiles:
            uid = str(profile.get("user_id", ""))
            if not uid or uid == self.owner_user_id or bool(profile.get("banned", False)):
                continue
            try:
                started = int(profile.get("start_count", 0) or 0) > 0
            except (TypeError, ValueError):
                started = False
            if not started:
                continue
            allowed_scopes = profile.get("allowed_scopes") if isinstance(profile.get("allowed_scopes"), list) else []
            sudo_scopes = profile.get("sudo_scopes") if isinstance(profile.get("sudo_scopes"), list) else []
            if not profile.get("allowed", False) and scope_key not in allowed_scopes and scope_key not in sudo_scopes:
                continue
            result.append({key: profile.get(key) for key in ("user_id", "name", "username", "start_count")})
        return sorted(result, key=lambda row: (str(row.get("name") or "").casefold(), str(row.get("user_id") or "")))

    def list_global_users(self) -> list[dict]:
        """List exact global allow grants, independently of per-chat grants and bans."""
        try:
            profiles = self._profiles.find({})
        except Exception:
            raise TenantStoreError("Cannot list global Telegram users") from None
        result = []
        for profile in profiles:
            uid = str(profile.get("user_id", ""))
            if not uid or uid == self.owner_user_id or profile.get("allowed") is not True:
                continue
            try:
                started = int(profile.get("start_count", 0) or 0) > 0
            except (TypeError, ValueError):
                started = False
            result.append({
                "user_id": uid,
                "name": profile.get("name") or uid,
                "username": profile.get("username") or "",
                "banned": bool(profile.get("banned", False)),
                "started": started,
            })
        return sorted(result, key=lambda row: (str(row.get("name") or "").casefold(), row["user_id"]))

    def close(self):
        """Connections are short-lived; provided for lifecycle/API compatibility."""
        return
