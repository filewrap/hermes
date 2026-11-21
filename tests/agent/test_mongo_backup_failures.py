"""Regression coverage for failure and isolation properties of Mongo backups.

All database tests use mongomock; no worker CLI, network, or external service is used.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import threading
from pathlib import Path

import mongomock
import pytest

from agent import mongo_backup as backup
from agent.mongo_backup import (
    BackupError,
    backup_snapshot,
    export_manifest,
    verify_manifest,
)


@pytest.fixture
def database():
    return mongomock.MongoClient().get_database("isolated")


def _sqlite(path: Path, values=("one", "two")):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO records(value) VALUES (?)", [(x,) for x in values])


def _manifest(db, result):
    return db.hermes_backup_manifests.find_one({"_id": result["manifest_id"]})


def test_open_writer_wal_snapshot_preserves_source_and_restores_committed_rows(tmp_path, database):
    source = tmp_path / "state.sqlite"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE records (value TEXT)")
    writer.executemany("INSERT INTO records VALUES (?)", [("committed-a",), ("committed-b",)])
    writer.commit()
    before = source.read_bytes()
    sidecars = {p.name: p.read_bytes() for p in tmp_path.glob("state.sqlite-wal")}
    try:
        result = backup_snapshot(tmp_path, database, owner="wal-test")
        assert source.read_bytes() == before
        assert {p.name: p.read_bytes() for p in tmp_path.glob("state.sqlite-wal")} == sidecars
        destination = tmp_path.parent / "wal-restore"
        export_manifest(database, result["manifest_id"], destination, owner="wal-test", home=tmp_path)
        with sqlite3.connect(destination / source.name) as restored:
            assert restored.execute("SELECT value FROM records ORDER BY value").fetchall() == [("committed-a",), ("committed-b",)]
    finally:
        writer.close()


def test_concurrent_sqlite_commits_snapshot_only_consistent_pairs(tmp_path, database):
    source = tmp_path / "paired.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE pairs (n INTEGER, side TEXT)")
    stop = threading.Event()

    def writer():
        with sqlite3.connect(source, timeout=5) as conn:
            i = 0
            while not stop.is_set():
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("INSERT INTO pairs VALUES (?, ?)", [(i, "left"), (i, "right")])
                conn.commit()
                i += 1

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        result = backup_snapshot(tmp_path, database, owner="concurrent")
    finally:
        stop.set()
        thread.join(timeout=5)
    destination = tmp_path.parent / "paired-restore"
    export_manifest(database, result["manifest_id"], destination, owner="concurrent", home=tmp_path)
    with sqlite3.connect(destination / source.name) as conn:
        rows = conn.execute("SELECT n, side FROM pairs").fetchall()
    by_id = {}
    for ident, side in rows:
        by_id.setdefault(ident, set()).add(side)
    assert all(sides == {"left", "right"} for sides in by_id.values())


def test_unknown_credentials_collection_is_never_touched(tmp_path, database, monkeypatch):
    legacy = database.deep_credentials
    legacy.insert_many([{"_id": f"old-{i}", "credential": f"secret-{i}"} for i in range(9)])
    original_find = legacy.find
    before = list(original_find())
    forbidden = AssertionError("legacy credential collection must not be read or written")
    for method in ("find", "find_one", "count_documents", "aggregate", "insert_one", "insert_many", "update_one", "delete_many"):
        monkeypatch.setattr(legacy, method, lambda *a, _e=forbidden, **kw: (_ for _ in ()).throw(_e))
    (tmp_path / "safe.txt").write_text("safe")
    backup_snapshot(tmp_path, database, owner="credentials")
    assert database.list_collection_names() == ["deep_credentials", "hermes_backup_chunks", "hermes_backup_manifests"]
    # Verify persistence directly from mongomock's internal store, without invoking the patched API.
    assert list(original_find()) == before


def test_identical_content_profiles_are_owner_scoped_and_restore_independently(tmp_path, database):
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        home.mkdir()
        (home / "same.txt").write_bytes(b"identical bytes")
    a = backup_snapshot(homes[0], database, owner="profile-A")
    b = backup_snapshot(homes[1], database, owner="profile-B")
    assert a["manifest_id"] != b["manifest_id"]
    assert verify_manifest(database, a["manifest_id"], owner="profile-A", home=homes[0])["state"] == "verified"
    with pytest.raises(BackupError):
        verify_manifest(database, a["manifest_id"], owner="profile-B", home=homes[1])
    out = tmp_path / "restored-b"
    export_manifest(database, b["manifest_id"], out, owner="profile-B", home=homes[1])
    assert (out / "same.txt").read_bytes() == b"identical bytes"


def test_chunk_tamper_fails_verification_and_restore(tmp_path, database):
    (tmp_path / "payload.bin").write_bytes(b"payload")
    result = backup_snapshot(tmp_path, database, owner="tamper")
    item = next(i for i in _manifest(database, result)["items"] if i["path"] == "payload.bin")
    chunk = item["chunks"][0]
    database.hermes_backup_chunks.update_one({"_id": chunk["id"]}, {"$set": {"data": b"changed"}})
    with pytest.raises(BackupError):
        verify_manifest(database, result["manifest_id"], owner="tamper", home=tmp_path)
    with pytest.raises(BackupError):
        export_manifest(database, result["manifest_id"], tmp_path.parent / "tampered-out", owner="tamper", home=tmp_path)


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "a/../../escape", "a\\..\\escape"])
def test_manifest_path_traversal_is_rejected(tmp_path, database, bad_path):
    (tmp_path / "safe.txt").write_text("safe")
    result = backup_snapshot(tmp_path, database, owner="traversal")
    database.hermes_backup_manifests.update_one(
        {"_id": result["manifest_id"]}, {"$set": {"items.0.path": bad_path}}
    )
    with pytest.raises(BackupError):
        verify_manifest(database, result["manifest_id"], owner="traversal", home=tmp_path)
    out = tmp_path.parent / "traversal-out"
    with pytest.raises(BackupError):
        export_manifest(database, result["manifest_id"], out, owner="traversal", home=tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_actual_skill_manifest_customization_is_included_and_bundle_is_skipped(tmp_path, database):
    from tools.skills_sync import dir_hash

    skills = tmp_path / "skills"
    bundled = skills / "bundled-tool"
    custom = skills / "locally-modified"
    bundled.mkdir(parents=True)
    custom.mkdir()
    (bundled / "SKILL.md").write_text("bundled content\n")
    (custom / "SKILL.md").write_text("custom original\n")
    (skills / ".bundled_manifest").write_text(f"bundled-tool:{dir_hash(bundled)}\ncustom-origin:{'0' * 32}\n")
    (custom / "SKILL.md").write_text("modified by user\n")
    result = backup_snapshot(tmp_path, database, owner="skills")
    paths = {item["path"] for item in _manifest(database, result)["items"]}
    assert "skills/bundled-tool/SKILL.md" not in paths
    assert "skills/locally-modified/SKILL.md" in paths
    assert "skills/.bundled_manifest" in paths


def test_worker_failure_returns_nonzero_and_sanitized_status(monkeypatch):
    if not hasattr(backup, "run_worker"):
        pytest.skip("worker API not present")
    class BrokenDatabase:
        def __getattr__(self, _name):
            raise RuntimeError("connection failed: mongodb://user:private-secret@host")
    monkeypatch.setattr(backup, "backup_snapshot", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("private-secret")))
    monkeypatch.setattr(backup, "time", type("Clock", (), {"sleep": staticmethod(lambda _n: (_ for _ in ()).throw(KeyboardInterrupt()))})(), raising=False)
    result = backup.run_worker(home=Path("/isolated"), database=BrokenDatabase(), interval=backup.MIN_INTERVAL, once=True)
    rendered = json.dumps(result, default=str)
    assert isinstance(result, dict) and result["state"] == "error"
    assert "private-secret" not in rendered


def test_status_command_does_not_import_mongo_client(monkeypatch):
    # status_json is a pure formatter: importing/calling it must not load PyMongo.
    import sys
    before = sys.modules.get("pymongo")
    backup.status_json("ok")
    assert sys.modules.get("pymongo") is before


def test_snapshot_rejects_source_symlink(tmp_path, database):
    target = tmp_path / "target.txt"
    target.write_text("outside source")
    link = tmp_path / "linked.txt"
    link.symlink_to(target)
    with pytest.raises(BackupError):
        backup_snapshot(tmp_path, database, owner="symlink")
    assert database.hermes_backup_manifests.count_documents({}) == 0


def test_interrupted_chunk_stage_resumes_immutably_without_deletes(tmp_path, database, monkeypatch):
    data = b"a" * backup.CHUNK_SIZE + b"b" * backup.CHUNK_SIZE + b"tail"
    (tmp_path / "large.bin").write_bytes(data)
    chunks = database.hermes_backup_chunks
    original = chunks.insert_one
    inserts = 0
    def fail_second(document):
        nonlocal inserts
        inserts += 1
        if inserts == 2:
            raise RuntimeError("injected network outage; private-secret")
        return original(document)
    monkeypatch.setattr(chunks, "insert_one", fail_second)
    with pytest.raises(BackupError):
        backup_snapshot(tmp_path, database, owner="interrupted")
    staged = list(chunks.find())
    assert len(staged) == 1
    assert database.hermes_backup_manifests.count_documents({}) == 0
    monkeypatch.setattr(chunks, "insert_one", original)
    result = backup_snapshot(tmp_path, database, owner="interrupted")
    assert chunks.find_one({"_id": staged[0]["_id"]}) == staged[0]
    assert chunks.count_documents({}) == 3
    assert backup_snapshot(tmp_path, database, owner="interrupted") == result
    dest = tmp_path.parent / "interrupted-restore"
    export_manifest(database, result["manifest_id"], dest, owner="interrupted", home=tmp_path)
    assert (dest / "large.bin").read_bytes() == data


def test_lost_manifest_insert_reply_is_verified_not_overwritten(tmp_path, database, monkeypatch):
    (tmp_path / "note.txt").write_text("durable")
    collection = database.hermes_backup_manifests
    insert = collection.insert_one
    def commit_then_fail(document):
        insert(document)
        raise RuntimeError("lost reply")
    monkeypatch.setattr(collection, "insert_one", commit_then_fail)
    result = backup_snapshot(tmp_path, database, owner="lost-reply")
    assert result["state"] == "complete"
    assert collection.count_documents({}) == 1
    assert verify_manifest(database, result["manifest_id"], owner="lost-reply", home=tmp_path)["state"] == "verified"


def test_changed_file_cannot_publish_a_mixed_backup(tmp_path, database, monkeypatch):
    source = tmp_path / "custom.txt"
    source.write_text("before")
    collection = database.hermes_backup_chunks
    insert = collection.insert_one
    def change_during_stage(document):
        result = insert(document)
        source.write_text("changed during backup")
        return result
    monkeypatch.setattr(collection, "insert_one", change_during_stage)
    with pytest.raises(BackupError, match="source_changed"):
        backup_snapshot(tmp_path, database)
    assert database.hermes_backup_manifests.count_documents({}) == 0
    assert source.read_text() == "changed during backup"


def test_multiline_bootstrap_secret_is_excluded_and_provider_value_roundtrips(tmp_path, database):
    from dotenv import dotenv_values
    (tmp_path / ".env").write_text(
        'MONGODB_URI="mongodb://private-secret\ncontinuation-secret"\n'
        'PROVIDER_KEY="line-one\nline-two"\n'
        "CUSTOM_SETTING='value with spaces'\n"
    )
    result = backup_snapshot(tmp_path, database)
    dest = tmp_path.parent / "filtered-env-restore"
    export_manifest(database, result["manifest_id"], dest, home=tmp_path)
    raw = (dest / ".env").read_bytes()
    values = dotenv_values(dest / ".env", interpolate=False)
    assert "MONGODB_URI" not in values
    assert b"private-secret" not in raw and b"continuation-secret" not in raw
    assert values["PROVIDER_KEY"] == "line-one\nline-two"
    assert values["CUSTOM_SETTING"] == "value with spaces"


def test_export_never_targets_source_profile_even_without_home_argument(tmp_path, database):
    (tmp_path / "note.txt").write_text("original")
    result = backup_snapshot(tmp_path, database)
    with pytest.raises(BackupError, match="unsafe_destination"):
        export_manifest(database, result["manifest_id"], tmp_path / "nested-restore")
    assert not (tmp_path / "nested-restore").exists()
    assert (tmp_path / "note.txt").read_text() == "original"


def test_export_failure_does_not_delete_a_raced_existing_directory(tmp_path, database, monkeypatch):
    (tmp_path / "note.txt").write_text("original")
    result = backup_snapshot(tmp_path, database)
    dest = tmp_path.parent / "raced-destination"
    original_mkdir = Path.mkdir
    def race(path, *args, **kwargs):
        if path == dest:
            original_mkdir(path)
            (path / "existing.txt").write_text("must survive")
            raise FileExistsError("simulated race")
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", race)
    with pytest.raises(BackupError):
        export_manifest(database, result["manifest_id"], dest, home=tmp_path)
    assert (dest / "existing.txt").read_text() == "must survive"


def test_export_files_and_directories_are_private(tmp_path, database):
    import stat
    directory = tmp_path / "knowledge" / "nested"
    directory.mkdir(parents=True)
    (directory / "private.txt").write_text("custom knowledge")
    result = backup_snapshot(tmp_path, database)
    dest = tmp_path.parent / "private-restore"
    export_manifest(database, result["manifest_id"], dest, home=tmp_path)
    for path in [dest, *dest.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600)


def test_checksum_consistent_traversal_manifest_is_still_rejected(tmp_path, database):
    (tmp_path / "safe.txt").write_text("safe")
    result = backup_snapshot(tmp_path, database, owner="traversal")
    manifest = _manifest(database, result)
    manifest["items"][0]["path"] = "../outside.txt"
    manifest["items_sha256"] = backup._hash(backup._packed(manifest["items"]))
    manifest["_id"] = backup._manifest_id(manifest)
    database.hermes_backup_manifests.insert_one(manifest)
    with pytest.raises(BackupError, match="unsafe_path"):
        verify_manifest(database, manifest["_id"], owner="traversal", home=tmp_path)


def test_status_cli_is_honestly_unknown_and_does_not_connect(monkeypatch, capsys):
    monkeypatch.setattr("agent.mongo_credentials._settings", lambda *a: pytest.fail("status read Mongo configuration"))
    monkeypatch.setattr("pymongo.MongoClient.__init__", lambda *a, **k: pytest.fail("status connected"))
    assert backup.main(["--status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"
