"""Tests for full durable HF state snapshots; never use the real /data path."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "deploy/huggingface"
sys.path.insert(0, str(HELPERS))
from full_state import FullStateStore


def store(home: Path, data: Path, *, mounted=True) -> FullStateStore:
    data.mkdir(parents=True, exist_ok=True)
    return FullStateStore(home, data, mount_check=lambda _path: mounted,
                          space_check=lambda: True,
                          bucket_name="test-owner/test-bucket")


def test_gate_requires_mount_and_successful_write_readback(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "bucket"
    home.mkdir()
    assert not store(home, data, mounted=False).prepare()
    assert not (data / "hermes-full-state").exists()
    assert not FullStateStore(home, data, mount_check=lambda _path: True,
                              space_check=lambda: False,
                              bucket_name="test-owner/test-bucket").prepare()

    mounted = store(home, data)
    assert mounted.prepare()
    real_read_bytes = Path.read_bytes

    def failed_readback(path):
        if path.name.startswith(".probe-"):
            return b"not the probe"
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", failed_readback)
    assert not mounted.prepare()


def test_bucket_name_alone_does_not_enable_full_profile_writes(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "bucket"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("BUCKET_NAME", "test-owner/test-bucket")
    monkeypatch.delenv("SPACE_ID", raising=False)
    store = FullStateStore(home, data, mount_check=lambda _path: True)
    assert not store.prepare()
    assert not (data / "hermes-full-state").exists()

    monkeypatch.setenv("SPACE_ID", "test-owner/test-space")
    assert store.prepare()
    assert store.directory == data / "hermes-full-state" / "test-owner" / "test-bucket"


def test_snapshot_includes_durable_files_and_sqlite_backup_and_restores_absent_only(tmp_path):
    home = tmp_path / "source"
    home.mkdir()
    (home / ".env").write_text("PROVIDER_API_KEY=secret\n")
    (home / "provider_credentials.json").write_text('{"key":"secret"}')
    (home / "config.yaml").write_text("model: example\n")
    nested = home / "profiles" / "work"
    nested.mkdir(parents=True)
    (nested / "settings.toml").write_text("enabled = true\n")
    executable = home / "skills" / "custom" / "run.sh"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\necho custom skill\n")
    executable.chmod(0o755)
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE records (value TEXT)")
        db.execute("INSERT INTO records VALUES ('committed durable value')")
        db.commit()
    session_path = home / "telegram.session"
    with sqlite3.connect(session_path) as db:
        db.execute("CREATE TABLE sessions (auth_key BLOB)")
        db.execute("INSERT INTO sessions VALUES (x'CAFE')")
        db.commit()
    (home / "other.sqlite-wal").write_bytes(b"must not be included")
    (home / ".cache").mkdir()
    (home / ".cache" / "junk").write_text("transient")
    (home / "debug.log").write_text("transient")

    data = tmp_path / "data"
    publisher = store(home, data)
    archive = publisher.snapshot()
    assert archive is not None and archive.is_file()
    assert archive.stat().st_mode & 0o777 == 0o600
    marker = archive.with_name(archive.stem + ".commit.json")
    assert marker.is_file()
    commit = json.loads(marker.read_text())
    assert commit["archive"] == archive.name
    assert len(commit["sha256"]) == 64
    with zipfile.ZipFile(archive) as zf:
        members = set(zf.namelist())
        assert {".env", "provider_credentials.json", "config.yaml", "profiles/work/settings.toml", "skills/custom/run.sh", "state.db", "telegram.session", "manifest.json"} <= members
        assert "other.sqlite-wal" not in members
        assert not any(".cache" in name or name.endswith(".log") for name in members)
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["format"] == 1
        assert {item["path"] for item in manifest["files"]} == members - {"manifest.json"}
        assert all(len(item["sha256"]) == 64 and item["size"] >= 0 and item["mode"] & 0o077 == 0 for item in manifest["files"])

    restored_home = tmp_path / "restored"
    restored_home.mkdir()
    (restored_home / "config.yaml").write_text("local config wins\n")
    recovery = store(restored_home, data)
    assert "Restored" in recovery.restore_latest()
    assert (restored_home / ".env").read_text() == "PROVIDER_API_KEY=secret\n"
    assert (restored_home / "config.yaml").read_text() == "local config wins\n"
    with sqlite3.connect(restored_home / "state.db") as db:
        assert db.execute("SELECT value FROM records").fetchone() == ("committed durable value",)
    with sqlite3.connect(restored_home / "telegram.session") as db:
        assert db.execute("SELECT auth_key FROM sessions").fetchone() == (b"\xca\xfe",)
    restored_tool = restored_home / "skills" / "custom" / "run.sh"
    assert restored_tool.read_text().startswith("#!/bin/sh")
    assert restored_tool.stat().st_mode & 0o777 == 0o700
    assert "preserved" in recovery.restore_latest()


def test_snapshots_are_unique_and_latest_valid_falls_back_when_archive_corrupt(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    target = store(home, data)
    (home / "file").write_text("old")
    first = target.snapshot()
    (home / "file").write_text("new")
    second = target.snapshot()
    assert first != second
    second.write_bytes(b"corrupt")
    restored = tmp_path / "restore"
    result = store(restored, data).restore_latest()
    assert "Restored" in result
    assert (restored / "file").read_text() == "old"


def test_identical_profiles_are_deduplicated_and_only_three_snapshots_retained(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    data = tmp_path / "data"
    target = store(home, data)
    state = home / "settings.json"
    state.write_text('{"revision":0}')
    first = target.snapshot()
    assert target.snapshot() == first
    for revision in range(1, 5):
        state.write_text(f'{{"revision":{revision}}}')
        assert target.snapshot() is not None
    assert len(list(target.directory.glob("hermes-full-state-*.zip"))) == 3
    restored = tmp_path / "latest"
    assert "Restored" in store(restored, data).restore_latest()
    assert (restored / "settings.json").read_text() == '{"revision":4}'


def test_rejects_symlinks_and_preserves_invalid_or_unsafe_archive_members(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / "outside"
    external.write_text("not to follow")
    (home / "link").symlink_to(external)
    target = store(home, tmp_path / "data")
    import pytest
    with pytest.raises(ValueError, match="symlink"):
        target.snapshot()
    (home / "link").unlink()

    archive = target.snapshot()
    assert archive
    # Alter the archive bytes while keeping the marker: it is never restored.
    archive.write_bytes(archive.read_bytes() + b"tamper")
    destination = tmp_path / "destination"
    assert "No valid" in store(destination, tmp_path / "data").restore_latest()
    assert not destination.exists()


def test_restore_rejects_preexisting_symlink_parent(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "file").write_text("safe")
    data = tmp_path / "data"
    assert store(source, data).snapshot()
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "nested").symlink_to(outside, target_is_directory=True)
    import pytest
    with pytest.raises(ValueError, match="symlink"):
        store(destination, data).restore_latest()
    assert not (outside / "file").exists()
