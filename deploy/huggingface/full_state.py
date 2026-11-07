"""Full durable HERMES_HOME snapshots for mounted Hugging Face storage.

This module is intentionally independent of the chat-history-only persistence
store. It never activates from environment variables: both exact mountpoint
validation and an actual read-back probe are required.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from typing import Callable

from persistence import (bucket_directory, configured_bucket_name, is_hf_space,
                         is_mountpoint, _prepare_bucket_directory)

FORMAT_VERSION = 1
ARCHIVE_PREFIX = "hermes-full-state-"
RETAIN_SNAPSHOTS = 3
EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".cache", "cache", "logs", "log", "tmp", "temp"}
EXCLUDED_SUFFIXES = (".lock", ".tmp", ".temp", ".swp", ".swo", ".log", "~")
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_hf_space() -> bool:
    """Require the platform-provided, canonical Space identity in addition to a mount."""
    return is_hf_space()


def _excluded(relative: PurePosixPath, *, is_dir: bool) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS or part.endswith((".egg-info",)) for part in parts):
        return True
    name = relative.name.lower()
    if name.endswith(EXCLUDED_SUFFIXES) or name.endswith(SIDECAR_SUFFIXES):
        return True
    if name.startswith(".") and name.endswith((".pid", ".sock")):
        return True
    return False


def _checked_relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or
            any(part in ("", ".", "..") for part in path.parts) or
            path.as_posix() != name):
        raise ValueError(f"unsafe snapshot path: {name!r}")
    return path


class FullStateStore:
    """Create and restore complete HERMES_HOME file snapshots.

    ``data_root`` and ``mount_check`` are injectable for isolated tests. In
    production the store is enabled only when ``data_root`` (normally /data)
    is itself a mountpoint and a write/readback probe succeeds.
    """

    def __init__(self, hermes_home: Path, data_root: Path = Path("/data"),
                 mount_check: Callable[[Path], bool] = is_mountpoint,
                 space_check: Callable[[], bool] = _is_hf_space,
                 bucket_name: str | None = None):
        self.home = Path(hermes_home)
        self.data_root = Path(data_root)
        self.mount_check = mount_check
        self.space_check = space_check
        self._bucket_name_injected = bucket_name is not None
        self.bucket_name = configured_bucket_name(bucket_name)
        self.directory = bucket_directory(self.data_root, "hermes-full-state", self.bucket_name)

    def _bucket_configuration_valid(self) -> bool:
        if self.bucket_name is None:
            return False
        return self._bucket_name_injected or configured_bucket_name() == self.bucket_name

    def prepare(self) -> bool:
        if self.data_root.is_symlink() or not self.data_root.is_dir():
            return False
        try:
            if (not self.space_check() or not self._bucket_configuration_valid()
                    or not self.mount_check(self.data_root)):
                return False
            self.directory = _prepare_bucket_directory(
                self.data_root, "hermes-full-state", self.bucket_name,
            )
            probe = self.directory / (".probe-" + secrets.token_hex(12))
            payload = secrets.token_bytes(32)
            try:
                with probe.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if probe.read_bytes() != payload:
                    return False
            finally:
                probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def _inventory(self) -> list[tuple[str, Path, bool]]:
        if self.home.is_symlink() or not self.home.is_dir():
            raise OSError("HERMES_HOME must be a real directory")
        entries: list[tuple[str, Path, bool]] = []
        for root, dirs, files in os.walk(self.home, topdown=True, followlinks=False):
            base = Path(root)
            dirs.sort()
            files.sort()
            kept_dirs = []
            for name in dirs:
                path = base / name
                rel = PurePosixPath(path.relative_to(self.home).as_posix())
                if _excluded(rel, is_dir=True):
                    continue
                if path.is_symlink() or not path.is_dir():
                    raise ValueError(f"symlink or special file in HERMES_HOME: {rel}")
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in files:
                path = base / name
                rel = PurePosixPath(path.relative_to(self.home).as_posix())
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode):
                    raise ValueError(f"symlink in HERMES_HOME: {rel}")
                if _excluded(rel, is_dir=False):
                    continue
                if not stat.S_ISREG(mode):
                    raise ValueError(f"special file in HERMES_HOME: {rel}")
                entries.append((rel.as_posix(), path, path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".session"}))
        return entries

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        src = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
            result = dst.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise ValueError(f"SQLite integrity check failed for {source.name}")
        finally:
            dst.close()
            src.close()

    def snapshot(self) -> Path | None:
        if not self.prepare():
            return None
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.directory))
        token = f"{time.time_ns()}-{secrets.token_hex(8)}"
        archive = self.directory / f"{ARCHIVE_PREFIX}{token}.zip"
        marker = self.directory / f"{ARCHIVE_PREFIX}{token}.commit.json"
        try:
            inventory = self._inventory()
            records = []
            payload_dir = stage / "payload"
            payload_dir.mkdir()
            for rel, source, is_sqlite in inventory:
                target = payload_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                mode = (stat.S_IMODE(source.stat().st_mode) & 0o700) | 0o400
                if is_sqlite:
                    self._sqlite_backup(source, target)
                else:
                    shutil.copyfile(source, target)
                    shutil.copystat(source, target, follow_symlinks=False)
                os.chmod(target, mode)
                records.append({"path": rel, "size": target.stat().st_size,
                                "sha256": _sha256_file(target), "sqlite": is_sqlite,
                                "mode": mode})
            content_digest = hashlib.sha256(json.dumps(
                [{"path": item["path"], "size": item["size"], "sha256": item["sha256"],
                  "mode": item["mode"]}
                 for item in records], sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            manifest = {"format": FORMAT_VERSION, "created_ns": time.time_ns(),
                        "content_sha256": content_digest, "files": records}
            existing = list(self._valid_snapshots())
            if existing and existing[0][1].get("content_sha256") == content_digest:
                return existing[0][0]
            manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("manifest.json", manifest_bytes)
                for record in records:
                    output.write(payload_dir / record["path"], record["path"])
            archive.chmod(0o600)
            with archive.open("rb") as stream:
                os.fsync(stream.fileno())
            # Re-open and verify every archived byte before publishing commit marker.
            with zipfile.ZipFile(archive, "r") as check:
                self._validate_archive(check)
                if check.read("manifest.json") != manifest_bytes:
                    raise ValueError("archive manifest read-back mismatch")
                for record in records:
                    data = check.read(record["path"])
                    if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                        raise ValueError("archive payload read-back mismatch")
            checksum = _sha256_file(archive)
            marker_bytes = json.dumps({"format": FORMAT_VERSION, "archive": archive.name,
                                       "sha256": checksum, "size": archive.stat().st_size},
                                      sort_keys=True).encode()
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(marker_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            if marker.read_bytes() != marker_bytes:
                raise ValueError("commit marker read-back mismatch")
            fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._prune_old_snapshots()
            return archive
        except Exception:
            archive.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _validate_archive(archive: zipfile.ZipFile) -> dict:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise ValueError("duplicate archive member")
        if "manifest.json" not in names:
            raise ValueError("manifest missing")
        manifest = json.loads(archive.read("manifest.json"))
        if (not isinstance(manifest, dict) or type(manifest.get("format")) is not int
                or manifest.get("format") != FORMAT_VERSION
                or not isinstance(manifest.get("files"), list)):
            raise ValueError("unsupported snapshot manifest")
        expected = {"manifest.json"}
        seen = set()
        for record in manifest["files"]:
            rel = _checked_relative(record["path"])
            name = rel.as_posix()
            if name in seen:
                raise ValueError("duplicate manifest path")
            seen.add(name)
            expected.add(name)
            info = archive.getinfo(name)
            if not info.is_dir() and (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                raise ValueError("symlink in archive")
            if info.file_size != record.get("size"):
                raise ValueError("file size mismatch")
            mode = record.get("mode")
            if type(mode) is not int or mode & ~0o700 or not mode & 0o400:
                raise ValueError("invalid owner-only restore mode")
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                raise ValueError("file checksum mismatch")
            if record.get("sqlite"):
                with tempfile.NamedTemporaryFile(suffix=".db") as temp:
                    temp.write(data)
                    temp.flush()
                    db = sqlite3.connect(f"file:{temp.name}?mode=ro", uri=True)
                    try:
                        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise ValueError("invalid SQLite snapshot")
                    finally:
                        db.close()
        if set(names) != expected:
            raise ValueError("unexpected archive member")
        content_digest = hashlib.sha256(json.dumps(
            [{"path": item["path"], "size": item["size"], "sha256": item["sha256"],
              "mode": item["mode"]} for item in manifest["files"]],
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if manifest.get("content_sha256") != content_digest:
            raise ValueError("profile content digest mismatch")
        return manifest

    def _valid_snapshots(self):
        for marker in sorted(self.directory.glob(ARCHIVE_PREFIX + "*.commit.json"), reverse=True):
            try:
                if marker.is_symlink() or not marker.is_file():
                    continue
                commit = json.loads(marker.read_text(encoding="utf-8"))
                name = commit["archive"]
                if Path(name).name != name or not name.startswith(ARCHIVE_PREFIX) or not name.endswith(".zip"):
                    continue
                archive_path = self.directory / name
                if archive_path.is_symlink() or not archive_path.is_file():
                    continue
                if archive_path.stat().st_size != commit["size"] or _sha256_file(archive_path) != commit["sha256"]:
                    continue
                with zipfile.ZipFile(archive_path) as archive:
                    manifest = self._validate_archive(archive)
                yield archive_path, manifest
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, json.JSONDecodeError):
                continue

    def restore_latest(self) -> str:
        if not self.prepare():
            return "Full-state snapshot storage is unavailable."
        candidates = list(self._valid_snapshots())
        if not candidates:
            return "No valid full-state snapshot is available."
        archive_path, manifest = candidates[0]
        if self.home.is_symlink():
            raise OSError("HERMES_HOME must not be a symlink")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        restored = 0
        with zipfile.ZipFile(archive_path) as archive:
            for record in manifest["files"]:
                rel = _checked_relative(record["path"])
                target = self.home.joinpath(*rel.parts)
                current = self.home
                for part in rel.parts[:-1]:
                    current = current / part
                    if current.is_symlink():
                        raise ValueError(f"symlink in restore path: {current}")
                    if current.exists() and not current.is_dir():
                        raise FileExistsError(current)
                    if not current.exists():
                        current.mkdir(mode=0o700)
                # O_EXCL provides no-clobber behavior even under races.
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    fd = os.open(target, flags, 0o600)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(fd, "wb") as output:
                        output.write(archive.read(rel.as_posix()))
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(target, record["mode"])
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                restored += 1
        return f"Restored {restored} absent full-state file(s); existing local files were preserved."

    def _prune_old_snapshots(self) -> None:
        """Keep only recent validated Hermes-owned snapshots; never touch unknown files."""
        valid = list(self._valid_snapshots())
        for archive, _manifest in valid[RETAIN_SNAPSHOTS:]:
            marker = archive.with_name(archive.name.removesuffix(".zip") + ".commit.json")
            try:
                marker.unlink()
                archive.unlink()
            except OSError:
                continue
