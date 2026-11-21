"""Explicit Mongo owner-profile runtime boundary.

Owner profile files are an owner-only, Mongo-first cache. A write compares both
Mongo's observed revision and the local cache bytes observed before commit.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Iterator

from agent.mongo_profile_store import hydrate_tenant
from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError

_SNAPSHOT: ContextVar[dict | None] = ContextVar("owner_profile_observed_files", default=None)
_ACTIVE: ContextVar[tuple[MongoTenantStore, Principal, Path] | None] = ContextVar("owner_profile_active", default=None)


def active_owner() -> tuple[MongoTenantStore, Principal, Path] | None:
    return _ACTIVE.get()


def write_active_owner_file(path: Path, content: bytes | str) -> int | None:
    """Write an in-scope owner profile path durably before updating the cache."""
    active = _ACTIVE.get()
    if active is None:
        from agent.storage_mode import mongo_primary_enabled
        if not mongo_primary_enabled(path):
            return None
        from hermes_constants import get_hermes_home
        from gateway.mongo_multiuser import enabled, store_for_home, owner_user_id
        home = Path(get_hermes_home())
        if not enabled(home):
            return None
        store = store_for_home(home)
        active = (store, Principal(store.owner_user_id), home)
    store, actor, home = active
    try:
        relpath = Path(path).absolute().relative_to(Path(home).absolute()).as_posix()
    except ValueError:
        return None
    _validate_owner_path(store, actor, relpath)
    try:
        _prior, revision = get_owner_file(store, actor, relpath)
    except TenantStoreError as exc:
        if str(exc) != "Owner profile file is not present in Mongo":
            raise
        revision = (_find_doc(store, actor, relpath) or {}).get("revision", 0)
    return put_owner_file(store, actor, relpath, content, revision)


@contextmanager
def owner_profile_context(store: MongoTenantStore, actor: Principal, home: Path) -> Iterator[int]:
    """Hydrate authoritative owner files before caller loads config/executes work."""
    if not store._is_owner(actor):
        raise TenantStoreError("Owner access required")
    home = Path(home)
    count = hydrate_tenant(store, actor, home)
    token = _ACTIVE.set((store, actor, home))
    observed = {doc["key"]: doc for doc in store.enumerate_tenant_data(actor, "owner") if doc.get("kind") == "instructions" and isinstance(doc.get("payload"), dict) and doc["payload"].get("path") == doc["key"]}
    snapshot_token = _SNAPSHOT.set(observed)
    try:
        yield count
        flush_active_owner_files()
    finally:
        _SNAPSHOT.reset(snapshot_token)
        _ACTIVE.reset(token)


def owner_file_revision(store: MongoTenantStore, actor: Principal, relpath: str) -> int:
    _validate_owner_path(store, actor, relpath)
    doc = _find_doc(store, actor, relpath)
    if doc is None:
        raise TenantStoreError("Owner profile file is not present in Mongo")
    return int(doc.get("revision", 0))


def get_owner_file(store: MongoTenantStore, actor: Principal, relpath: str) -> tuple[bytes, int]:
    _validate_owner_path(store, actor, relpath)
    doc = _find_doc(store, actor, relpath)
    if doc is None or (doc.get("payload") or {}).get("deleted"):
        raise TenantStoreError("Owner profile file is not present in Mongo")
    payload = doc.get("payload") or {}
    try:
        data = base64.b64decode(payload["content"], validate=True)
    except Exception:
        raise TenantStoreError("Invalid Mongo profile content") from None
    if hashlib.sha256(data).hexdigest() != payload.get("sha256"):
        raise TenantStoreError("Mongo profile checksum mismatch")
    return data, int(doc.get("revision", 0))


def put_owner_file(store: MongoTenantStore, actor: Principal, relpath: str,
                   content: bytes | str, expected_revision: int,
                   expected_cache: bytes | str | None = None) -> int:
    """CAS-write Mongo first, then atomically install cache if it still matches."""
    _validate_owner_path(store, actor, relpath)
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes):
        raise TenantStoreError("Invalid profile file content")
    path = _safe_cache_path(Path(store.home), relpath)
    observed = _read_cache(path) if expected_cache is None else expected_cache
    if isinstance(observed, str):
        observed = observed.encode("utf-8")
    if observed is not None and not isinstance(observed, bytes):
        raise TenantStoreError("Invalid observed cache value")
    doc = _find_doc(store, actor, relpath)
    prior = None
    revision = 0
    if doc is not None:
        revision = int(doc.get("revision", 0))
        if not (doc.get("payload") or {}).get("deleted"):
            prior, revision = get_owner_file(store, actor, relpath)
    if revision != expected_revision:
        raise TenantStoreError("Tenant data revision conflict")
    if observed != prior:
        raise TenantStoreError("Local profile cache diverges from Mongo")
    payload = {"path": relpath, "encoding": "base64", "content": base64.b64encode(content).decode("ascii"), "sha256": hashlib.sha256(content).hexdigest()}
    new_revision = store._put("owner", "instructions", relpath, payload, expected_revision=expected_revision)
    verify, verified_revision = get_owner_file(store, actor, relpath)
    if verified_revision != new_revision or verify != content:
        raise TenantStoreError("Mongo profile write readback failed")
    if _read_cache(path) != observed:
        raise TenantStoreError("Local profile cache diverges from Mongo")
    _atomic_cache_write(path, content)
    return new_revision


def _find_doc(store: MongoTenantStore, actor: Principal, relpath: str):
    store._owner_action(actor)
    return store._call(lambda: store._data.find_one({"_id": store._data_id("owner", "instructions", relpath)}))


def _read_cache(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            raise TenantStoreError("Linked owner profile cache file")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise TenantStoreError("Cannot read owner profile cache") from None


def _safe_cache_path(home: Path, relpath: str) -> Path:
    root = Path(home).resolve()
    path = root.joinpath(*PurePosixPath(relpath).parts)
    current = root
    for part in PurePosixPath(relpath).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise TenantStoreError("Linked owner profile cache directory")
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise TenantStoreError("Unsafe owner profile cache path")
    return path


def _atomic_cache_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise TenantStoreError("Linked owner profile cache directory")
    fd, name = tempfile.mkstemp(prefix=".owner-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise TenantStoreError("Linked owner profile cache file")
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _validate_owner_path(store: MongoTenantStore, actor: Principal, relpath: str) -> None:
    if not store._is_owner(actor):
        raise TenantStoreError("Owner access required")
    if not isinstance(relpath, str):
        raise TenantStoreError("Invalid profile path")
    pure = PurePosixPath(relpath)
    if (pure.is_absolute() or not pure.parts or ".." in pure.parts
            or any(part in {".env", ".git", "sessions", "cache", "logs", "venv", ".venv"} for part in pure.parts)
            or not ((len(pure.parts) == 1 and pure.name in {"config.yaml", "SOUL.md", "AGENTS.md", "system_prompt.md", "MEMORY.md", "USER.md"})
                    or (len(pure.parts) > 1 and pure.parts[0] in {"memories", "skills", "preferences", "knowledge"}))):
        raise TenantStoreError("Unsafe owner profile path")


def ensure_authoritative_config(home: Path) -> None:
    """Owner startup/CLI reads use Mongo; no silent local fallback in enabled mode."""
    from agent.storage_mode import mongo_primary_enabled
    if not mongo_primary_enabled(home):
        return
    from gateway.mongo_multiuser import enabled, store_for_home, owner_user_id
    home = Path(home)
    if not enabled(home):
        return
    store = store_for_home(home)
    actor = Principal(owner_user_id(home))
    content, _revision = get_owner_file(store, actor, "config.yaml")
    path = _safe_cache_path(home, "config.yaml")
    cached = _read_cache(path)
    if cached is None:
        _atomic_cache_write(path, content)
    elif cached != content:
        raise TenantStoreError("Owner config cache differs from its Mongo authority")


def _atomic_binding():
    binding = active_owner()
    if binding is not None:
        return binding
    from agent.storage_mode import mongo_primary_enabled
    if not mongo_primary_enabled():
        return None
    from hermes_constants import get_hermes_home
    from gateway.mongo_multiuser import enabled, owner_user_id, store_for_home
    home = Path(get_hermes_home())
    if not enabled(home):
        return None
    store = store_for_home(home)
    return store, Principal(store.owner_user_id), home


def maybe_persist_atomic(path: Path, writer, *, binary: bool = False, encoding: str = "utf-8") -> bool:
    """Called only by the common atomic writer; runtime/cache/bootstrap files bypass."""
    from io import StringIO, BytesIO
    from hermes_constants import get_hermes_home
    active = active_owner()
    home = active[2] if active is not None else Path(get_hermes_home())
    try:
        rel = Path(path).absolute().relative_to(Path(home).absolute()).as_posix()
    except ValueError:
        return False
    pure = PurePosixPath(rel)
    if not ((len(pure.parts) == 1 and pure.name in {"config.yaml", "SOUL.md", "AGENTS.md", "system_prompt.md", "MEMORY.md", "USER.md"})
            or (len(pure.parts) > 1 and pure.parts[0] in {"memories", "skills", "preferences", "knowledge"})):
        return False
    if rel in {"memories/MEMORY.md", "memories/USER.md"}:
        return False
    binding = _atomic_binding()
    if binding is None:
        return False
    store, actor, home = binding
    try:
        _validate_owner_path(store, actor, rel)
    except TenantStoreError:
        return False
    if rel in {"memories/MEMORY.md", "memories/USER.md"}:
        return False  # MemoryStore has its own typed authoritative record.
    deferred = _DEFERRED.get()
    if deferred is not None:
        deferred.setdefault(Path(path), _read_cache(_safe_cache_path(home, rel)))
        return False
    stream = BytesIO() if binary else StringIO()
    writer(stream)
    content = stream.getvalue()
    if isinstance(content, str):
        content = content.encode(encoding)
    return write_active_owner_file(Path(path), content) is not None


_DEFERRED: ContextVar[dict | None] = ContextVar("owner_profile_deferred_writes", default=None)


def defer_durable_writes(function):
    """Commit a scanned skill's final state, never its unscanned tentative contents."""
    from functools import wraps
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _DEFERRED.get() is not None:
            return function(*args, **kwargs)
        pending = {}
        token = _DEFERRED.set(pending)
        try:
            result = function(*args, **kwargs)
        except BaseException:
            for path, before in pending.items():
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_cache_write(path, before)
            raise
        finally:
            _DEFERRED.reset(token)
        for path, before in pending.items():
            after = _read_cache(path)
            if after != before:
                commit_staged_owner_file(path, before, after)
        return result
    return wrapped


def commit_staged_owner_file(path: Path, before: bytes | None, after: bytes | None) -> None:
    binding = _atomic_binding()
    if binding is None:
        return
    store, actor, home = binding
    rel = Path(path).absolute().relative_to(Path(home).absolute()).as_posix()
    _validate_owner_path(store, actor, rel)
    _safe_cache_path(home, rel)
    doc = _find_doc(store, actor, rel)
    revision = int(doc.get("revision", 0)) if doc else 0
    prior = None if not doc or (doc.get("payload") or {}).get("deleted") else get_owner_file(store, actor, rel)[0]
    if prior == after:
        return
    if prior != before or _read_cache(path) != after:
        raise TenantStoreError("Concurrent owner profile mutation")
    payload = ({"path": rel, "deleted": True, "previous_sha256": hashlib.sha256(before).hexdigest()}
               if after is None else {"path": rel, "encoding": "base64", "content": base64.b64encode(after).decode(), "sha256": hashlib.sha256(after).hexdigest()})
    new_revision = store._put("owner", "instructions", rel, payload, expected_revision=revision)
    check = _find_doc(store, actor, rel)
    if not check or check.get("revision") != new_revision or check.get("payload") != payload:
        raise TenantStoreError("Owner staged profile read-back failed")


def flush_active_owner_files() -> int:
    """Commit explicit owner tool file edits/deletes before returning their results."""
    binding, observed = active_owner(), _SNAPSHOT.get()
    if binding is None or observed is None:
        return 0
    store, actor, home = binding
    from agent.mongo_profile_store import inventory_profile
    local = {record["key"]: record for record in inventory_profile(home)
             if record["key"] not in {"memories/MEMORY.md", "memories/USER.md"}}
    current = {doc["key"]: doc for doc in store.enumerate_tenant_data(actor, "owner") if doc.get("kind") == "instructions" and isinstance(doc.get("payload"), dict) and doc["payload"].get("path") == doc["key"]}
    changed = 0
    for rel in sorted(set(local) | set(observed)):
        doc = current.get(rel)
        payload = doc.get("payload") if doc else None
        target = local[rel]["payload"] if rel in local else None
        if payload == target or (target is None and (not payload or payload.get("deleted"))):
            continue
        previous = observed.get(rel)
        if doc and previous and doc.get("revision") != previous.get("revision"):
            raise TenantStoreError("Concurrent owner profile mutation")
        if doc and not previous:
            raise TenantStoreError("Unexpected concurrent owner profile file")
        before = None if not payload or payload.get("deleted") else base64.b64decode(payload["content"], validate=True)
        after = None if target is None else base64.b64decode(target["content"], validate=True)
        commit_staged_owner_file(Path(home) / rel, before, after)
        changed += 1
    observed.clear()
    observed.update({doc["key"]: doc for doc in store.enumerate_tenant_data(actor, "owner") if doc.get("kind") == "instructions" and isinstance(doc.get("payload"), dict) and doc["payload"].get("path") == doc["key"]})
    return changed


def delete_active_owner_tree(directory: Path) -> None:
    binding = _atomic_binding()
    if binding is None:
        return
    store, actor, home = binding
    directory = Path(directory).absolute()
    prefix = directory.relative_to(Path(home).absolute()).as_posix() + "/"
    for doc in store.enumerate_tenant_data(actor, "owner"):
        if doc.get("kind") != "instructions" or not doc.get("key", "").startswith(prefix):
            continue
        payload = doc.get("payload") or {}
        if payload.get("deleted"):
            continue
        path = _safe_cache_path(home, doc["key"])
        raw, revision = get_owner_file(store, actor, doc["key"])
        if _read_cache(path) != raw:
            raise TenantStoreError("Owner skill cache differs from Mongo")
        tombstone = {"path": doc["key"], "deleted": True, "previous_sha256": hashlib.sha256(raw).hexdigest()}
        new_revision = store._put("owner", "instructions", doc["key"], tombstone, expected_revision=revision)
        check = _find_doc(store, actor, doc["key"])
        if not check or check.get("payload") != tombstone or check.get("revision") != new_revision:
            raise TenantStoreError("Owner skill deletion read-back failed")
