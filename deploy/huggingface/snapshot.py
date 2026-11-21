#!/usr/bin/env python3
"""Offline Hermes home snapshots. Stop all writers; no network or service management."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time

MANIFEST = "SNAPSHOT-MANIFEST.json"
SECRET_NAMES = {
    ".env",
    "auth.json",
    "auth_store.json",
    "credentials",
    "credentials.json",
    "token.json",
    "secrets.json",
}
SECRET_DIRS = {"auth", "credentials", "secrets"}
TRANSIENT_NAMES = {".lock", "lock", "pid", "hermes.pid", "gateway.pid"}


def safe_path(path: Path) -> Path:
    """Reject symlink ancestors before resolving; callers still require quiescent paths."""
    path = Path(os.path.abspath(path))
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("symlink path refused")
    return path


def is_secret(rel: Path) -> bool:
    return any(
        p.lower() in SECRET_DIRS | SECRET_NAMES
        or p.lower().endswith((".pem", ".key", ".token", ".env", ".session"))
        for p in rel.parts
    )


def is_transient(rel: Path) -> bool:
    name = rel.name.lower()
    return (
        name in TRANSIENT_NAMES
        or name.endswith((".sock", ".pid", ".lock"))
        or ".lock." in name
        or name.endswith(("-wal", "-shm", "-journal"))
    )


def is_sqlite(path: Path) -> bool:
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return True
    with path.open("rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sqlite_backup(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 60

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError(
                "SQLite backup exceeded 60 seconds; check quiescence and locks"
            )

    with closing(
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=1024, progress=progress, sleep=0.05)
            result = dst.execute("PRAGMA quick_check").fetchone()
            if result != ("ok",):
                raise ValueError("SQLite backup integrity check failed")
            dst.execute("PRAGMA journal_mode=DELETE")


def create(home: Path, output: Path, secrets: bool, quiesced: bool) -> None:
    if not quiesced:
        raise ValueError(
            "--quiesced is required: stop all home writers before snapshotting"
        )
    home, output = safe_path(home), safe_path(output)
    if not home.is_dir():
        raise ValueError("--home must be an existing directory")
    if output.exists():
        raise ValueError("output already exists")
    if output == home or home in output.parents:
        raise ValueError("output must not be inside the source home")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = {
        "format": 1,
        "files": {},
        "modes": {},
        "secrets_included": bool(secrets),
        "consistency": "operator-quiesced; independent SQLite online backups",
    }
    with tempfile.TemporaryDirectory(prefix="hermes-snapshot-") as td:
        stage = Path(td)
        for root, dirs, files in os.walk(home, topdown=True, followlinks=False):
            rootp = Path(root)
            dirs.sort()
            files.sort()
            for name in list(dirs):
                p = rootp / name
                rel = p.relative_to(home)
                if p.is_symlink():
                    raise ValueError(f"symlink refused: {rel}")
                if is_transient(rel) or (not secrets and is_secret(rel)):
                    dirs.remove(name)
            for name in files:
                src = rootp / name
                rel = src.relative_to(home)
                if src.is_symlink():
                    raise ValueError(f"symlink refused: {rel}")
                if is_transient(rel) or (not secrets and is_secret(rel)):
                    continue
                if rel.as_posix() == MANIFEST:
                    raise ValueError("source contains reserved snapshot manifest name")
                if not stat.S_ISREG(src.stat().st_mode):
                    raise ValueError(f"special file refused: {rel}")
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                database = is_sqlite(src)
                if database:
                    sqlite_backup(src, dest)
                else:
                    shutil.copyfile(src, dest)
                mode = 0o700 if not database and src.stat().st_mode & 0o100 else 0o600
                os.chmod(dest, mode)
                manifest["files"][rel.as_posix()] = digest(dest)
                manifest["modes"][rel.as_posix()] = mode
        mpath = stage / MANIFEST
        mpath.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(mpath, 0o600)
        fd, tempname = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        os.close(fd)
        try:
            with tarfile.open(tempname, "w:gz") as tar:
                for p in sorted(stage.rglob("*")):
                    if p.is_file():
                        tar.add(
                            p, arcname=p.relative_to(stage).as_posix(), recursive=False
                        )
            with open(tempname, "rb") as finished:
                os.fsync(finished.fileno())
            # Atomic no-clobber publication on local POSIX disk, unlike os.replace.
            os.link(tempname, output)
        finally:
            os.unlink(tempname)


def member_path(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if (
        p.is_absolute()
        or not p.parts
        or p.as_posix() != name
        or ".." in p.parts
        or "\\" in name
    ):
        raise ValueError(f"unsafe archive path: {name!r}")
    return p


def restore(archive: Path, home: Path) -> None:
    archive, home = safe_path(archive), safe_path(home)
    parent = home.parent
    if not parent.is_dir():
        raise ValueError("destination parent must be an existing local directory")
    if home.exists() and (not home.is_dir() or any(home.iterdir())):
        raise ValueError("restore destination must be absent or an empty directory")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = set()
        for m in members:
            member_path(m.name)
            if m.name in names:
                raise ValueError("duplicate archive member")
            names.add(m.name)
            if not m.isfile():
                raise ValueError("non-regular archive member refused")
        if MANIFEST not in names:
            raise ValueError("snapshot manifest missing")
        with tar.extractfile(MANIFEST) as f:
            manifest = json.load(f)
        if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("unsupported or malformed snapshot manifest")
        expected, modes = manifest["files"], manifest.get("modes", {})
        if not isinstance(modes, dict) or set(expected) != names - {MANIFEST}:
            raise ValueError("manifest paths do not match archive contents")
        for name, checksum in expected.items():
            member_path(name)
            if (
                not isinstance(checksum, str)
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
            ):
                raise ValueError("invalid checksum entry")
            if modes.get(name, 0o600) not in (0o600, 0o700):
                raise ValueError("unsafe restored mode")
        stage = Path(tempfile.mkdtemp(prefix=f".{home.name}.restore-", dir=parent))
        try:
            for m in members:
                if m.name == MANIFEST:
                    continue
                dest = stage.joinpath(*member_path(m.name).parts)
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tar.extractfile(m) as src, dest.open("xb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(dest, modes.get(m.name, 0o600))
                if digest(dest) != expected[m.name]:
                    raise ValueError(f"checksum mismatch: {m.name}")
            for p in stage.rglob("*"):
                if p.is_file() and is_sqlite(p):
                    with closing(
                        sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
                    ) as db:
                        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            raise ValueError("SQLite quick_check failed")
            if home.exists():
                # Recheck after validation: preserve any newly arrived contents.
                home.rmdir()
            os.rename(stage, home)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser(
        "create", help="create a local tar.gz snapshot; all writers must be stopped"
    )
    c.add_argument("--home", required=True, type=Path)
    c.add_argument("--output", required=True, type=Path)
    c.add_argument(
        "--include-secrets",
        action="store_true",
        help="include known credential/session files; sensitive data can exist elsewhere too",
    )
    c.add_argument(
        "--quiesced",
        action="store_true",
        help="acknowledge all home writers have stopped",
    )
    r = sub.add_parser(
        "restore", help="restore a trusted archive into a new/empty local home"
    )
    r.add_argument("--archive", required=True, type=Path)
    r.add_argument("--home", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            create(args.home, args.output, args.include_secrets, args.quiesced)
            print(f"Snapshot created: {args.output}")
        else:
            restore(args.archive, args.home)
            print(f"Snapshot restored: {args.home}")
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError) as exc:
        parser.exit(1, f"snapshot: error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
