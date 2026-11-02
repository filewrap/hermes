"""Runtime persistence policy for explicitly migrated Mongo-primary profiles.

A URI alone never changes serving authority. Mongo becomes primary only after an
owner-authorized migration has been verified and its local activation marker is
installed. Chat/message history remains in the local SQLite cache.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

_MARKER = ".mongo-primary.json"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _identity(home: Path) -> tuple[str, str]:
    from agent.mongo_credentials import URI_ENV, _bootstrap
    from gateway.mongo_multiuser import owner_user_id

    uri = _bootstrap(home).get(URI_ENV, "").strip()
    if not uri:
        raise RuntimeError("Mongo primary activation requires the saved MongoDB URI")
    owner = owner_user_id(home)
    return owner, uri


def mongo_primary_enabled(home: str | Path | None = None) -> bool:
    """Return true only for an exact, valid local marker bound to URI, owner and profile."""
    if home is None:
        from hermes_constants import get_process_hermes_home
        home = get_process_hermes_home()
    root = Path(home).expanduser().resolve(strict=False)
    marker_path = root / _MARKER
    if not marker_path.exists() and not marker_path.is_symlink():
        return False
    if marker_path.is_symlink() or not marker_path.is_file():
        raise RuntimeError("Mongo primary activation marker is unsafe")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        owner, uri = _identity(root)
    except Exception:
        raise RuntimeError("Mongo primary activation marker cannot be verified") from None
    if (not isinstance(marker, dict) or marker.get("schema") != 1
            or marker.get("migration_id") != "plaintext-tenants-v2"
            or marker.get("owner_id") != owner
            or marker.get("uri_sha256") != hashlib.sha256(uri.encode()).hexdigest()
            or marker.get("home_sha256") != hashlib.sha256(str(root).encode()).hexdigest()
            or not isinstance(marker.get("migration_digest"), str)
            or not _DIGEST.fullmatch(marker["migration_digest"])):
        raise RuntimeError("Mongo primary activation marker does not match this profile")
    return True


def activate_mongo_primary(home: str | Path, receipt: dict, *, mongo_database) -> dict:
    """Install a no-clobber marker only for the exact persisted migration receipt."""
    root = Path(home).expanduser().resolve(strict=True)
    if (not isinstance(receipt, dict)
            or receipt.get("_id") != "plaintext-tenants-v2"
            or receipt.get("status") != "complete"
            or type(receipt.get("local_deleted")) is not int
            or receipt["local_deleted"] != 0
            or not isinstance(receipt.get("owner_id"), str)
            or not isinstance(receipt.get("digest"), str)
            or not _DIGEST.fullmatch(receipt["digest"])):
        raise ValueError("A complete verified Mongo migration is required")
    owner, uri = _identity(root)
    if receipt["owner_id"] != owner:
        raise ValueError("Mongo migration owner does not match this profile")
    from agent.mongo_migration import MIGRATION_ID
    persisted = mongo_database.hermes_migrations.find_one({"_id": MIGRATION_ID})
    if persisted != receipt:
        raise ValueError("Persisted Mongo migration receipt does not match the verified plan")
    marker = {
        "schema": 1,
        "migration_id": receipt["_id"],
        "migration_digest": receipt["digest"],
        "owner_id": owner,
        "uri_sha256": hashlib.sha256(uri.encode()).hexdigest(),
        "home_sha256": hashlib.sha256(str(root).encode()).hexdigest(),
    }
    target = root / _MARKER
    encoded = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
            raise FileExistsError("Conflicting Mongo primary activation marker")
        return marker
    fd, temporary = tempfile.mkstemp(prefix=".mongo-primary-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
                raise FileExistsError("Conflicting Mongo primary activation marker") from None
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    if not mongo_primary_enabled(root):
        raise RuntimeError("Mongo primary activation marker read-back failed")
    return marker
