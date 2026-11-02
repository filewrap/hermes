"""Durable, explicitly scoped Mongo profile migration and cache helpers.

Mongo is authoritative only when the caller explicitly enables tenant mode. These
helpers never delete local data and never auto-import local edits after bootstrap.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from agent.mongo_tenants import MongoTenantStore, Principal, TenantStoreError

_FILE_NAMES = {"config.yaml", "SOUL.md", "AGENTS.md", "system_prompt.md", "MEMORY.md", "USER.md"}
_FILE_DIRS = {"memories", "skills", "preferences", "knowledge"}
_EXCLUDED_PARTS = {".env", "venv", ".venv", "__pycache__", "cache", "logs", "sessions", "node_modules", "venvs", ".git"}


def _safe_rel(path: Path, home: Path) -> str:
    if path.is_symlink():
        raise TenantStoreError("Symlink in durable profile inventory")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(home):
        raise TenantStoreError("Profile path escapes home")
    rel = path.relative_to(home).as_posix()
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or any(p in _EXCLUDED_PARTS for p in pure.parts):
        raise TenantStoreError("Unsafe profile path")
    return rel


def inventory_profile(home: Path) -> list[dict[str, Any]]:
    """Pure local inventory of whitelisted profile files, preserving exact bytes."""
    root = Path(home).expanduser().resolve(strict=True)
    candidates = set()
    for name in _FILE_NAMES:
        p = root / name
        if p.exists() or p.is_symlink(): candidates.add(p)
    for directory in _FILE_DIRS:
        base = root / directory
        if base.is_symlink(): raise TenantStoreError("Symlink in durable profile inventory")
        if base.is_dir():
            for current, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [name for name in dirs if name not in _EXCLUDED_PARTS]
                if any((Path(current) / name).is_symlink() for name in dirs):
                    raise TenantStoreError("Symlink in durable profile inventory")
                for name in files:
                    if name in _EXCLUDED_PARTS or name.endswith((".pyc", ".tmp", ".lock")):
                        continue
                    p = Path(current) / name
                    if p.is_symlink(): raise TenantStoreError("Symlink in durable profile inventory")
                    if p.is_file(): candidates.add(p)
    records = []
    for p in sorted(candidates, key=lambda x: x.relative_to(root).as_posix()):
        rel = _safe_rel(p, root)
        raw = p.read_bytes()
        records.append({"kind": "files", "key": rel, "payload": {"path": rel, "encoding": "base64", "content": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}})
    return records


def build_migration_inventory(home: Path) -> list[dict[str, Any]]:
    """Typed deterministic owner-only records; SQLite histories are intentionally not copied."""
    root = Path(home).expanduser().resolve(strict=True)
    records = [{"kind": "profile", "key": "owner", "payload": {"schema": 1}}]
    for record in inventory_profile(root):
        if record["key"] in {"memories/MEMORY.md", "memories/USER.md"}:
            try:
                text = base64.b64decode(record["payload"]["content"], validate=True).decode("utf-8-sig")
            except (ValueError, UnicodeError):
                raise TenantStoreError("Unreadable canonical owner memory") from None
            records.append({"kind": "memory", "key": "root:" + Path(record["key"]).name, "payload": text})
        else:
            records.append({**record, "kind": "instructions"})
    from gateway.mongo_environment import environment_inventory
    records.append({"kind": "settings", "key": "environment", "payload": environment_inventory(root)})
    # Existing credential migration remains explicit and uses its established credential
    # adapters. Bootstrap URI/key are intentionally not part of this inventory.
    # Use the credential adapter's semantic parser and inventory, not a partial
    # hand-maintained list of credential files. The sync itself performs its own
    # fail-closed preflight and exact readback verification.
    credentials = _inventory_credentials(root)
    records.extend({"kind": "providers", "key": key, "payload": payload}
                   for key, payload in sorted(credentials.items()))
    # No raw SQLite snapshots: aggregate durable usage must be migrated by a schema-aware
    # application adapter; current database schemas vary and are not guessed here.
    return records


def bootstrap_owner(store: MongoTenantStore, actor: Principal, home: Path) -> dict:
    """Explicit initial migration. Verify each write/read; safe reruns do not rewrite."""
    if not store._is_owner(actor): raise TenantStoreError("Owner access required")
    records = build_migration_inventory(home)
    existing = {(r["kind"], r["key"]): r["payload"] for r in store.enumerate_tenant_data(actor, "owner")}
    # Preflight the full inventory before any writes, not after a partial bootstrap.
    for record in records:
        address = (record["kind"], record["key"])
        if address in existing and existing[address] != record["payload"]:
            raise TenantStoreError("Remote profile differs; explicit reconciliation required")
    written = 0
    for record in records:
        kind, key, payload = record["kind"], record["key"], record["payload"]
        prior = existing.get((kind, key))
        if prior is not None and prior != payload:
            raise TenantStoreError("Remote profile differs; explicit reconciliation required")
        if prior is None:
            store.owner_put_tenant(actor, "owner", kind, key, payload)
            written += 1
        if store.get(actor, kind, key) != payload:
            raise TenantStoreError("Mongo profile readback verification failed")
    digest = hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    manifest = {"schema": 1, "complete": True, "records": len(records), "sha256": digest,
                "counts": {kind: sum(r["kind"] == kind for r in records) for kind in sorted({r["kind"] for r in records})}}
    prior_manifest = store.get(actor, "profile", "migration_manifest")
    if prior_manifest is not None and prior_manifest != manifest:
        raise TenantStoreError("Remote profile differs; explicit reconciliation required")
    if prior_manifest is None:
        store.owner_put_tenant(actor, "owner", "profile", "migration_manifest", manifest)
    if store.get(actor, "profile", "migration_manifest") != manifest:
        raise TenantStoreError("Mongo migration manifest readback verification failed")
    return {"records": len(records), "written": written, "verified": len(records), "local_deleted": 0,
            "manifest": manifest}


def _inventory_credentials(home: Path) -> dict[str, Any]:
    """Read credential source inventory without making writes; fail closed on parse errors."""
    from hermes_cli.mongo_credentials import _read_json, _owned_legacy_files
    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    from agent import mongo_credentials as credentials
    result: dict[str, Any] = {}
    for path, name in _owned_legacy_files(home).items():
        if path.suffix == ".cimd-off":
            result[name] = {"rejected": True}
        else:
            value = _read_json(path)
            if value is not None:
                if name.endswith(":tokens") and value.get("expires_in") is not None and "expires_at" not in value:
                    value["expires_at"] = path.stat().st_mtime + int(value["expires_in"])
                result[name] = value
    root_env = Path(home) / ".env"
    if root_env.is_symlink():
        raise TenantStoreError("Linked bootstrap environment file")
    if root_env.exists():
        try:
            env = _parse_env_text(_decode_env_bytes(root_env.read_bytes()))
        except Exception:
            raise TenantStoreError("Cannot inventory local credentials") from None
        from hermes_cli.mongo_credentials import is_credential_env
        for key, value in env.items():
            if is_credential_env(key) and key not in credentials.BOOTSTRAP_KEYS:
                result["env:" + key] = value
    # Inline provider secrets in config are included semantically, preserving values
    # in the owner-only record. Bootstrap connection secrets never enter the inventory.
    config = Path(home) / "config.yaml"
    if config.is_file():
        try:
            from hermes_cli.config import require_readable_config_before_write
            from hermes_cli.mongo_credentials import split_config_secrets
            parsed = require_readable_config_before_write(config)
            if not isinstance(parsed, dict): raise ValueError
            _, secrets_found = split_config_secrets(parsed)
        except Exception:
            raise TenantStoreError("Cannot inventory provider configuration") from None
        result.update({"config:" + key: value for key, value in secrets_found.items()})
    auth = _read_json(Path(home) / "auth.json")
    if auth is not None:
        result["auth"] = auth
    vault_path = Path(home) / "vault" / "vault.json.enc"
    if vault_path.exists():
        try:
            from cryptography.fernet import Fernet
            vault = json.loads(Fernet((vault_path.parent / "vault.key").read_bytes().strip()).decrypt(vault_path.read_bytes()))
            if not isinstance(vault, dict) or not isinstance(vault.get("items"), list): raise ValueError
            result["vault"] = vault
        except Exception:
            raise TenantStoreError("Cannot inventory local credential vault") from None
    return result



def _cache_materialize(home: Path, parts: tuple[str, ...], raw: bytes) -> None:
    """Use directory descriptors and exclusive no-follow files, never predictable temp paths."""
    directory = home.joinpath(*parts[:-1])
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        def matches_existing():
            try:
                existing = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return False
            with os.fdopen(existing, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise TenantStoreError("Unsafe local cache path")
                if stream.read() != raw:
                    raise TenantStoreError("Local profile cache diverges from Mongo")
            return True
        if matches_existing():
            return
        temporary = ".mongo-cache-" + secrets.token_hex(16)
        temp_fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(temp_fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Atomic creation, never overwrite a file created concurrently.
                os.link(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                matches_existing()
            os.fsync(fd)
        finally:
            os.unlink(temporary, dir_fd=fd)
    except TenantStoreError:
        raise
    except (OSError, ValueError):
        raise TenantStoreError("Unsafe or unavailable local cache path") from None
    finally:
        os.close(fd)


def hydrate_tenant(store: MongoTenantStore, principal: Principal, cache_home: Path) -> int:
    """Materialize authoritative file documents; reject symlinks/traversal and remote corruption."""
    tenant = store.principal_tenant(principal)
    store._require_active(principal)
    home = Path(cache_home).expanduser().absolute()
    if tenant != "owner" and home != (store.home.resolve() / ".tenant-cache" / tenant):
        raise TenantStoreError("Tenant cache must use its isolated cache directory")
    count = 0
    for doc in store.enumerate_tenant_data(Principal(store.owner_user_id), tenant):
        if doc.get("kind") != "instructions": continue
        payload = doc.get("payload")
        # Not all instruction documents represent files.
        if not isinstance(payload, dict) or "path" not in payload:
            continue
        rel = payload.get("path")
        if not isinstance(rel, str) or not rel: raise TenantStoreError("Invalid remote profile file")
        pure = PurePosixPath(rel)
        if (not pure.parts or pure.is_absolute() or ".." in pure.parts
                or any(p in _EXCLUDED_PARTS for p in pure.parts)
                or not ((len(pure.parts) == 1 and pure.name in _FILE_NAMES)
                        or (len(pure.parts) > 1 and pure.parts[0] in _FILE_DIRS))):
            raise TenantStoreError("Unsafe remote profile path")
        if payload.get("deleted") is True:
            from gateway.mongo_runtime import _safe_cache_path, _read_cache
            target = _safe_cache_path(home, rel)
            cached = _read_cache(target)
            if cached is not None:
                if hashlib.sha256(cached).hexdigest() != payload.get("previous_sha256"):
                    raise TenantStoreError("Deleted owner cache differs from its tombstone")
                target.unlink()
            continue
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str) or not isinstance(payload.get("sha256"), str):
            raise TenantStoreError("Invalid remote profile file")
        try:
            raw = base64.b64decode(payload["content"], validate=True)
        except (ValueError, TypeError):
            raise TenantStoreError("Invalid remote profile file") from None
        if hashlib.sha256(raw).hexdigest() != payload["sha256"]:
            raise TenantStoreError("Remote profile checksum mismatch")
        _cache_materialize(home, pure.parts, raw)
        count += 1
    return count


def flush_tenant_cache(store: MongoTenantStore, principal: Principal, cache_home: Path) -> int:
    """CAS-like guarded flush: only unchanged remote content can be updated; bootstrap is separate."""
    tenant = store.principal_tenant(principal)
    store._require_active(principal)
    home = Path(cache_home).expanduser().resolve(strict=True)
    if tenant != "owner" and home != (store.home.resolve() / ".tenant-cache" / tenant):
        raise TenantStoreError("Tenant cache must use its isolated cache directory")
    docs = store.enumerate_tenant_data(Principal(store.owner_user_id), tenant)
    remote = {d["key"]: d["payload"] for d in docs if d.get("kind") == "instructions"}
    records = inventory_profile(home)
    if set(remote) != {r["key"] for r in records}: raise TenantStoreError("Local profile cache diverges from Mongo")
    changed = 0
    for r in records:
        prior, payload = remote[r["key"]], r["payload"]
        if prior == payload: continue
        # Source edits are accepted only through explicit expected-revision update API; do not
        # overwrite authoritative content from an untracked local cache.
        raise TenantStoreError("Local profile cache diverges from Mongo")
    return changed
