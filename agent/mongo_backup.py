"""Explicit local-to-Mongo backup; never imported by ordinary serving/save paths.

Local files and SQLite remain canonical. This module is inert on import. A backup
worker is a separate opt-in process; recovery only exports into a NEW directory.
Existing Mongo collections and snapshots are never updated or deleted.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Iterator

CHUNK_SIZE = 256 * 1024
DEFAULT_INTERVAL = 300
MIN_INTERVAL, MAX_INTERVAL = 30, 86400
MAX_MANIFEST_BYTES = 16 * 1024 * 1024 - 1024
MAX_FILES, MAX_TOTAL_REFS = 100_000, 1_000_000
MAX_ENV_BYTES = 1024 * 1024
_CHUNKS, _MANIFESTS = "hermes_backup_chunks", "hermes_backup_manifests"
_EXCLUDE_DIRS = {"cache", "caches", "logs", "tmp", "temp", "sessions-media", "media", "node_modules", "venv", ".venv", "venvs", ".git", "__pycache__", ".tenant-cache", "locks", ".locks"}
_DURABLE_DIRS = {"skills", "memories", "personal", "preferences", "knowledge", "sessions", "cron", "instructions", "credentials", "providers", "auth", "mcp", "vault", "profiles"}
_DURABLE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".session", ".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".md", ".txt", ".bin", ".pem", ".key"}
_SQL_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".session"}
_SECRET_ENV_KEYS = {"MONGODB_URI", "CREDENTIAL_KEY", "DEEP_MONGODB_URI", "DEEP_CREDENTIAL_KEY"}
_HEX = re.compile(r"^[0-9a-f]{64}$")


class BackupError(RuntimeError):
    """Only fixed, non-secret error labels may cross the worker status boundary."""


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _packed(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def _bson_size(value: dict) -> int:
    from bson import BSON
    return len(BSON.encode(value))


def _clean_env(data: bytes) -> bytes:
    from agent.secret_scope import _decode_env_bytes
    from dotenv.parser import parse_stream
    from io import StringIO
    try:
        bindings = list(parse_stream(StringIO(_decode_env_bytes(data))))
        if any(binding.error for binding in bindings):
            raise ValueError("invalid dotenv syntax")
        values = {binding.key: binding.value for binding in bindings if binding.key is not None}
    except Exception:
        raise BackupError("invalid_environment") from None
    result = []
    for key, value in values.items():
        upper = key.upper()
        if upper in _SECRET_ENV_KEYS or ("MONGO" in upper and ("URI" in upper or "URL" in upper)) or "CONNECTION_STRING" in upper or "BOOTSTRAP" in upper:
            continue
        # Reconstruct parsed values, not original lines: multiline bootstrap
        # values and comments cannot accidentally leak into the backup.
        if value is None:
            result.append(f"{key}\n")
            continue
        if "\n" in value or "\r" in value or "'" in value:
            rendered = json.dumps(value, ensure_ascii=False)
        elif not value or re.search(r"[\s#\"\\$]", value):
            rendered = "'" + value + "'"
        else:
            rendered = value
        result.append(f"{key}={rendered}\n")
    return "".join(result).encode()


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError("unsafe_path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in {".", "..", ""} or part in _EXCLUDE_DIRS for part in path.parts):
        raise BackupError("unsafe_path")
    return path


def _iter_files(root: Path) -> Iterator[tuple[str, Path]]:
    count = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in _EXCLUDE_DIRS and (base != root or d in _DURABLE_DIRS))
        if any((base / name).is_symlink() for name in dirs):
            raise BackupError("unsafe_path")
        for name in sorted(names):
            path = base / name
            if name.endswith((".lock", ".tmp", ".pyc", ".log", "-wal", "-shm", "-journal")) or name.startswith(".backup-worker"):
                continue
            if base == root and not (name == ".env" or name.startswith(".env.") or path.suffix.lower() in _DURABLE_SUFFIXES):
                continue
            if "cache" in name.lower() and base == root:
                continue
            if path.is_symlink():
                raise BackupError("unsafe_path")
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            _relative(rel)
            if not path.resolve(strict=True).is_relative_to(root):
                raise BackupError("unsafe_path")
            count += 1
            if count > MAX_FILES:
                raise BackupError("too_many_files")
            yield rel, path


def _stat_key(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _stream_hash_directory(directory: Path) -> str:
    """Byte-compatible with skills_sync.dir_hash, with bounded content reads."""
    from tools.skills_sync import _is_runtime_cache
    result = hashlib.md5()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise BackupError("unsafe_path")
        if path.is_file() and not _is_runtime_cache(path, directory):
            before = _stat_key(path)
            result.update(str(path.relative_to(directory)).encode())
            with path.open("rb") as stream:
                for block in _chunks(stream):
                    result.update(block)
            if _stat_key(path) != before:
                raise BackupError("source_changed")
    return result.hexdigest()


def _unchanged_skills(root: Path) -> dict[Path, str]:
    skills = root / "skills"
    manifest = skills / ".bundled_manifest"
    if not manifest.is_file():
        return {}
    if manifest.is_symlink() or manifest.stat().st_size > MAX_ENV_BYTES:
        raise BackupError("invalid_skill_manifest")
    origins = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        name, _, digest = line.strip().partition(":")
        if name:
            origins[name.strip()] = digest.strip()
    from tools.skill_usage import read_skill_name
    unchanged = {}
    for skill_md in skills.rglob("SKILL.md"):
        if skill_md.is_symlink() or any(p.is_symlink() for p in skill_md.parents if p != root.parent):
            raise BackupError("unsafe_path")
        directory = skill_md.parent
        name = read_skill_name(skill_md, directory.name)
        origin = origins.get(name) or origins.get(directory.name)
        if origin and _stream_hash_directory(directory) == origin:
            unchanged[directory] = origin
    return unchanged


def _skip_default(rel: str, path: Path, unchanged: dict[Path, str]) -> bool:
    if any(path.is_relative_to(directory) for directory in unchanged):
        return True
    if rel == "SOUL.md":
        from hermes_cli.default_soul import DEFAULT_SOUL_MD
        expected = DEFAULT_SOUL_MD.encode()
        return path.stat().st_size == len(expected) and path.read_bytes() == expected
    # Config/default selections and partly modified skills are NEVER discarded
    # merely because an individual file happens to match today's repository.
    return False


class _OwnedTempStream:
    def __init__(self, path: Path, directory: Path):
        self.stream = path.open("rb")
        self.directory = directory
    def read(self, size=-1):
        return self.stream.read(size)
    def close(self):
        self.stream.close()
        shutil.rmtree(self.directory, ignore_errors=True)


def _sqlite_snapshot(source: Path):
    if source.is_symlink():
        raise BackupError("unsafe_database")
    directory = Path(tempfile.mkdtemp(prefix="hermes-backup-"))
    target = directory / "snapshot.sqlite"
    try:
        wal, shm = Path(str(source) + "-wal"), Path(str(source) + "-shm")
        for sidecar in (wal, shm, Path(str(source) + "-journal")):
            if sidecar.is_symlink():
                raise BackupError("unsafe_database")
        with source.open("rb") as stream:
            header = stream.read(20)
        if header[:16] != b"SQLite format 3\x00":
            # An empty newly-created native database is a valid SQLite source.
            if source.stat().st_size:
                raise BackupError("invalid_sqlite")
        uri = source.as_uri() + "?mode=ro"
        if len(header) >= 20 and header[18] == 2:
            if wal.exists() and not shm.exists():
                raise BackupError("sqlite_snapshot_busy")
            if not wal.exists():
                # A quiescent WAL-mode database without sidecars would cause a
                # read-only SQLite open to create them. Copy only a proven-stable
                # core and use immutable mode on that disposable copy instead.
                before = _stat_key(source)
                copied = directory / "source.sqlite"
                shutil.copyfile(source, copied)
                if before != _stat_key(source) or wal.exists() or shm.exists():
                    raise BackupError("sqlite_snapshot_busy")
                uri = copied.as_uri() + "?mode=ro&immutable=1"
        deadline = time.monotonic() + 30
        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise BackupError("sqlite_snapshot_timeout")
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as src, closing(sqlite3.connect(target)) as dst:
            src.execute("PRAGMA query_only=ON")
            src.backup(dst, pages=128, progress=progress, sleep=.01)
            if dst.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupError("sqlite_snapshot_corrupt")
        return _OwnedTempStream(target, directory)
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("sqlite_snapshot_failed") from None


def _chunks(stream):
    while True:
        block = stream.read(CHUNK_SIZE)
        if not block:
            return
        yield block


def _resolve_owner(root: Path, owner: str | None) -> str:
    if owner is None:
        from agent.secret_scope import _decode_env_bytes, _parse_env_text
        env = root / ".env"
        if env.is_file():
            if env.is_symlink() or env.stat().st_size > MAX_ENV_BYTES:
                raise BackupError("invalid_environment")
            from agent.mongo_environment import canonical_values
            owner = canonical_values(_parse_env_text(_decode_env_bytes(env.read_bytes()))).get("OWNER_TELEGRAM_ID")
        owner = owner or _hash(str(root).encode())
    if not isinstance(owner, str) or not owner or len(owner) > 256:
        raise BackupError("invalid_owner")
    return owner


def _manifest_id(manifest: dict) -> str:
    return _hash(_packed({key: manifest[key] for key in ("schema", "owner", "owner_label", "profile_id", "items_sha256")}))


def _insert_immutable(collection, document: dict, conflict: str):
    try:
        collection.insert_one(document)
    except Exception:
        # Insert may have committed despite a lost response. Only the exact
        # immutable identity/content proves success; never replace/upsert it.
        existing = collection.find_one({"_id": document["_id"]})
        if existing != document:
            raise BackupError(conflict) from None
    if collection.find_one({"_id": document["_id"]}) != document:
        raise BackupError(conflict)


def backup_snapshot(home: str | Path, database: Any, *, owner: str | None = None) -> dict:
    source = Path(home).expanduser()
    if source.is_symlink():
        raise BackupError("unsafe_path")
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise BackupError("unsafe_path")
    owner = _resolve_owner(root, owner)
    scoped = _hash((owner + "\0" + str(root)).encode())
    # This capability is deliberately confined to TWO NEW collection names.
    chunks, manifests = database[_CHUNKS], database[_MANIFESTS]
    unchanged = _unchanged_skills(root)
    files = sorted(_iter_files(root))
    items, versions, refs_count = [], {}, 0
    for rel, path in files:
        before = _stat_key(path)
        if _skip_default(rel, path, unchanged):
            versions[path] = before
            continue
        is_sql = path.suffix.lower() in _SQL_SUFFIXES
        is_env = path.name == ".env" or path.name.startswith(".env.")
        if is_sql:
            stream = _sqlite_snapshot(path)
        elif is_env:
            if path.stat().st_size > MAX_ENV_BYTES:
                raise BackupError("environment_too_large")
            stream = BytesIO(_clean_env(path.read_bytes()))
        else:
            stream = path.open("rb")
        digest, size, refs = hashlib.sha256(), 0, []
        try:
            for block in _chunks(stream):
                sha = _hash(block)
                cid = _hash((scoped + "\0" + sha).encode())
                _insert_immutable(chunks, {"_id": cid, "owner": scoped, "sha256": sha, "size": len(block), "data": block}, "chunk_write_unverified")
                digest.update(block)
                size += len(block)
                refs.append({"id": cid, "sha256": sha, "size": len(block)})
                refs_count += 1
                if refs_count > MAX_TOTAL_REFS:
                    raise BackupError("too_many_chunks")
        finally:
            stream.close()
        if not is_sql:
            if _stat_key(path) != before:
                raise BackupError("source_changed")
            versions[path] = before
        items.append({"path": rel, "size": size, "sha256": digest.hexdigest(), "kind": "sqlite_snapshot" if is_sql else "profile_state", "chunks": refs})
    # Do not publish a file-set that lost/gained files, or a partly changed skill.
    if [rel for rel, _ in sorted(_iter_files(root))] != [rel for rel, _ in files]:
        raise BackupError("source_changed")
    if any(_stat_key(path) != version for path, version in versions.items()):
        raise BackupError("source_changed")
    if any(_stream_hash_directory(path) != digest for path, digest in unchanged.items()):
        raise BackupError("source_changed")
    manifest = {"schema": 1, "owner": scoped, "owner_label": owner, "profile_id": _hash(str(root).encode()), "items_sha256": _hash(_packed(items)), "items": items, "complete": True}
    manifest["_id"] = _manifest_id(manifest)
    if _bson_size(manifest) > MAX_MANIFEST_BYTES:
        raise BackupError("manifest_too_large")
    _insert_immutable(manifests, manifest, "manifest_write_unverified")
    return {"manifest_id": manifest["_id"], "files": len(items), "chunks": refs_count, "state": "complete"}


def _checked_manifest(database, manifest_id, owner=None, home=None):
    if not isinstance(manifest_id, str) or not _HEX.fullmatch(manifest_id):
        raise BackupError("manifest_missing")
    manifest = database[_MANIFESTS].find_one({"_id": manifest_id})
    if not manifest:
        raise BackupError("manifest_missing")
    if manifest.get("schema") != 1 or manifest.get("complete") is not True or _bson_size(manifest) > MAX_MANIFEST_BYTES:
        raise BackupError("manifest_corrupt")
    for key in ("owner", "profile_id", "items_sha256"):
        if not isinstance(manifest.get(key), str) or not _HEX.fullmatch(manifest[key]):
            raise BackupError("manifest_corrupt")
    if owner is not None and manifest.get("owner_label") != owner:
        raise BackupError("manifest_missing")
    if home is not None:
        root = Path(home).expanduser().resolve()
        label = _resolve_owner(root, owner)
        if manifest["profile_id"] != _hash(str(root).encode()) or manifest["owner"] != _hash((label + "\0" + str(root)).encode()):
            raise BackupError("manifest_missing")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) > MAX_FILES or _hash(_packed(items)) != manifest["items_sha256"] or _manifest_id(manifest) != manifest_id:
        raise BackupError("manifest_corrupt")
    paths, count = set(), 0
    for item in items:
        if not isinstance(item, dict):
            raise BackupError("manifest_corrupt")
        _relative(item.get("path"))
        if item["path"] in paths or type(item.get("size")) is not int or item["size"] < 0 or not _HEX.fullmatch(item.get("sha256", "")) or not isinstance(item.get("chunks"), list):
            raise BackupError("manifest_corrupt")
        paths.add(item["path"])
        for ref in item["chunks"]:
            count += 1
            if count > MAX_TOTAL_REFS or not isinstance(ref, dict) or type(ref.get("size")) is not int or not 0 < ref["size"] <= CHUNK_SIZE or not _HEX.fullmatch(ref.get("sha256", "")) or ref.get("id") != _hash((manifest["owner"] + "\0" + ref["sha256"]).encode()):
                raise BackupError("manifest_corrupt")
    return manifest


def _checked_chunk(database, ref, owner):
    chunk = database[_CHUNKS].find_one({"_id": ref["id"]})
    if not chunk or chunk.get("owner") != owner or chunk.get("sha256") != ref["sha256"] or chunk.get("size") != ref["size"]:
        raise BackupError("chunk_missing")
    block = chunk.get("data")
    if not isinstance(block, bytes) or len(block) != ref["size"] or _hash(block) != ref["sha256"]:
        raise BackupError("chunk_corrupt")
    return block


def verify_manifest(database: Any, manifest_id: str, *, owner=None, home=None) -> dict:
    try:
        manifest = _checked_manifest(database, manifest_id, owner, home)
        for item in manifest["items"]:
            digest, size = hashlib.sha256(), 0
            for ref in item["chunks"]:
                block = _checked_chunk(database, ref, manifest["owner"])
                digest.update(block)
                size += len(block)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise BackupError("file_corrupt")
        return {"manifest_id": manifest_id, "files": len(manifest["items"]), "state": "verified"}
    except BackupError:
        raise
    except Exception:
        raise BackupError("verification_failed") from None


def export_manifest(database: Any, manifest_id: str, destination: str | Path, *, owner=None, home=None) -> dict:
    """Explicit recovery export, never an ordinary runtime restore or merge."""
    dest = Path(destination).expanduser().absolute()
    if dest.exists() or dest.is_symlink():
        raise BackupError("destination_not_empty")
    if any(parent.is_symlink() for parent in dest.parents):
        raise BackupError("unsafe_destination")
    if home is not None and dest.resolve().is_relative_to(Path(home).expanduser().resolve()):
        raise BackupError("unsafe_destination")
    created = False
    try:
        # Keep this validated in-memory manifest; do not re-read an unchecked
        # replacement after verification. Re-verify every chunk while writing.
        manifest = _checked_manifest(database, manifest_id, owner, home)
        if any(_hash(str(parent.resolve()).encode()) == manifest["profile_id"] for parent in (dest, *dest.parents)):
            raise BackupError("unsafe_destination")
        dest.mkdir(parents=True, mode=0o700)
        created = True
        for item in manifest["items"]:
            rel = _relative(item["path"])
            target = dest.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(parent.is_symlink() for parent in target.parents) or target.is_symlink():
                raise BackupError("unsafe_path")
            for parent in target.parents:
                if not parent.is_relative_to(dest):
                    break
                parent.chmod(0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as stream:
                for ref in item["chunks"]:
                    block = _checked_chunk(database, ref, manifest["owner"])
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise BackupError("restore_checksum_failed")
        return {"manifest_id": manifest_id, "files": len(manifest["items"]), "state": "restored_verified"}
    except Exception as exc:
        if created and dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest, ignore_errors=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("restore_failed") from None


def status_json(state="unknown", *, files=0, chunks=0, error_class=None):
    return {"state": state, "files": int(files), "chunks": int(chunks), "error_class": error_class}


def run_worker(home, database, *, interval=DEFAULT_INTERVAL, once=False):
    if type(interval) is not int or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise BackupError("invalid_interval")
    while True:
        try:
            result = backup_snapshot(home, database)
            status = status_json("complete", files=result["files"], chunks=result["chunks"])
        except BackupError:
            # Do not print even an exception message: callers may inject errors
            # and driver messages can contain a URI, password, or file contents.
            status = status_json("error", error_class="BackupError")
        except Exception as exc:
            status = status_json("error", error_class=type(exc).__name__)
        print(json.dumps(status), flush=True)
        if once:
            return status
        time.sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--status", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")))
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(status_json()))
        return 0  # Unknown is honest: status does not connect or launch anything.
    if not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
        parser.error("interval must be between 30 and 86400 seconds")
    try:
        from agent.mongo_credentials import _settings
        from pymongo import MongoClient
        settings = _settings(args.home)
        with MongoClient(settings.uri, tls=True, tlsAllowInvalidCertificates=False, connect=False, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, socketTimeoutMS=10000) as client:
            result = run_worker(args.home, client.get_database(settings.database), interval=args.interval, once=args.once)
        return 0 if not args.once or result["state"] == "complete" else 1
    except Exception as exc:
        print(json.dumps(status_json("error", error_class=type(exc).__name__)), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
