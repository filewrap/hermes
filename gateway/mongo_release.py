"""Fail-closed release attestation for the plaintext Mongo tenant runtime.

Only the trusted deployment path records this marker after inventory read-back and
regression checks. Chat commands and tenant tools never receive this capability.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
RELEASE_ID = "tenant-runtime-v2"


class ReleaseGateError(RuntimeError):
    """Safe deployment verification error, with no connection details."""


def source_state() -> tuple[str, bool]:
    repository = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        changes = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repository, text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseGateError("Cannot verify the committed runtime source") from None
    return commit, not bool(changes.strip())


def _database(home: Path):
    from agent.mongo_credentials import _collection, _settings
    return _collection(_settings(Path(home))).database


def assert_release_ready(home: Path, *, database: Any = None) -> dict:
    """Check deployment attestation; absence, DB failure or changed code denies start."""
    from gateway.mongo_multiuser import owner_user_id
    try:
        owner = owner_user_id(home)
        commit, clean = source_state()
        if not clean:
            raise ReleaseGateError("Runtime checkout has unverified source changes")
        db = database if database is not None else _database(Path(home))
        marker = db["hermes_release"].find_one({"_id": RELEASE_ID})
        if not marker or marker.get("schema") != SCHEMA_VERSION:
            raise ReleaseGateError("Verified tenant migration/release marker is missing")
        if marker.get("owner_id") != owner or marker.get("source_commit") != commit:
            raise ReleaseGateError("Release owner or committed source differs from verification")
        if marker.get("status") != "verified" or not marker.get("migration_verified"):
            raise ReleaseGateError("Tenant migration has not been fully verified")
        regression = marker.get("regression", {})
        if regression.get("failed") != 0 or regression.get("errors") != 0 or regression.get("passed", 0) <= 0:
            raise ReleaseGateError("Release regression checks did not pass")
        if not marker.get("inventory_digest") or marker.get("records_verified", 0) <= 0:
            raise ReleaseGateError("Complete durable inventory verification is missing")
        return marker
    except ReleaseGateError:
        raise
    except Exception:
        raise ReleaseGateError("Cannot verify tenant release readiness") from None


def release_ready(home: Path, *, database: Any = None) -> bool:
    try:
        assert_release_ready(home, database=database)
        return True
    except Exception:
        return False


def record_verified_release(
    home: Path, actor_id: str, *, inventory_digest: str,
    records_verified: int, regression: dict, migration_verified: bool,
    database: Any = None,
) -> dict:
    """Trusted deployment-only final step, never an ordinary user-facing mutation."""
    from gateway.mongo_multiuser import owner_user_id
    owner = owner_user_id(home)
    if str(actor_id) != owner:
        raise ReleaseGateError("Only the configured owner may attest deployment")
    commit, clean = source_state()
    if not clean:
        raise ReleaseGateError("Commit and verify source before recording deployment")
    if (not migration_verified or type(records_verified) is not int or records_verified <= 0
            or not isinstance(inventory_digest, str) or len(inventory_digest) != 64
            or any(c not in "0123456789abcdef" for c in inventory_digest)):
        raise ReleaseGateError("Complete durable inventory verification is required")
    if (not isinstance(regression, dict) or regression.get("failed") != 0
            or regression.get("errors") != 0 or type(regression.get("passed")) is not int
            or regression["passed"] <= 0):
        raise ReleaseGateError("Passing regression evidence is required")
    marker = {
        "_id": RELEASE_ID, "schema": SCHEMA_VERSION, "owner_id": owner,
        "source_commit": commit, "status": "verified", "migration_verified": True,
        "inventory_digest": inventory_digest, "records_verified": records_verified,
        "regression": {key: regression.get(key, 0) for key in ("passed", "failed", "errors", "skipped")},
        "verified_at": datetime.now(UTC).isoformat(),
    }
    try:
        db = database if database is not None else _database(Path(home))
        previous = db["hermes_release"].find_one({"_id": RELEASE_ID})
        if previous and previous.get("owner_id") != owner:
            raise ReleaseGateError("Existing release belongs to a different owner")
        db["hermes_release"].replace_one({"_id": RELEASE_ID}, marker, upsert=True)
        if db["hermes_release"].find_one({"_id": RELEASE_ID}) != marker:
            raise ReleaseGateError("Release attestation read-back failed")
    except ReleaseGateError:
        raise
    except Exception:
        raise ReleaseGateError("Cannot record verified tenant release") from None
    return marker
