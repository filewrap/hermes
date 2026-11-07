"""HF chat-state persistence uses only validated, sanitized SQLite snapshots."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
HELPERS = ROOT / "deploy/huggingface"
sys.path.insert(0, str(HELPERS))

from persistence import SnapshotStore, is_mountpoint


def make_state_db(home: Path, *, title: str = "A conversation") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
              session_key TEXT, chat_id TEXT, chat_type TEXT, thread_id TEXT,
              display_name TEXT, expiry_finalized INTEGER DEFAULT 0,
              model TEXT, model_config TEXT, system_prompt TEXT,
              system_prompt_hash TEXT, parent_session_id TEXT,
              started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
              message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
              input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
              cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
              reasoning_tokens INTEGER DEFAULT 0, billing_base_url TEXT,
              billing_provider TEXT, billing_mode TEXT, handoff_state TEXT,
              cwd TEXT, git_branch TEXT, git_repo_root TEXT, title TEXT,
              last_activity_at REAL, api_call_count INTEGER DEFAULT 0,
              archived INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
              content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
              timestamp REAL NOT NULL, token_count INTEGER, reasoning TEXT,
              reasoning_details TEXT, api_content TEXT
            );
            CREATE TABLE gateway_routing (
              scope TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL,
              entry_json TEXT NOT NULL, updated_at REAL NOT NULL,
              PRIMARY KEY(scope, session_key)
            );
            CREATE TABLE provider_secrets (name TEXT, secret TEXT);
            CREATE TABLE unrelated_cache (payload TEXT);
            """
        )
        db.execute(
            """INSERT INTO sessions
               (id,source,user_id,session_key,chat_id,chat_type,model,model_config,
                system_prompt,billing_base_url,handoff_state,cwd,title,started_at,message_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("sid-1", "telegram", "user-7", "peer-7", "chat-7", "dm",
             "openrouter/free", '{"api_key":"private-provider-secret"}',
             "system prompt containing private-provider-secret",
             "https://private-provider-secret.invalid", "private-provider-secret",
             "/home/private-provider-secret", title, 1.0, 1),
        )
        db.execute(
            "INSERT INTO messages (id,session_id,role,content,timestamp) VALUES (1,'sid-1','user','hello continuation',2.0)"
        )
        route = {
            "session_key": "peer-7", "session_id": "sid-1",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:01:00",
            "chat_type": "dm", "metadata": {
                "thread_ts": "safe-watermark", "api_key": "private-provider-secret",
                "telegram_bot_token": "private-provider-secret", "api_hash": "private-provider-secret",
                "auth_key": "private-provider-secret", "session_string": "private-provider-secret",
                "nested": {"password": "private-provider-secret", "label": "safe"},
            },
            "active_turn_token": "local-turn-id",
            "model_override": {"model": "private", "base_url": "https://private-provider-secret.invalid"},
        }
        db.execute(
            "INSERT INTO gateway_routing VALUES ('','peer-7',?,3.0)",
            (json.dumps(route),),
        )
        db.execute("INSERT INTO provider_secrets VALUES ('provider','private-provider-secret')")
        db.execute("INSERT INTO unrelated_cache VALUES ('private-provider-secret')")
        db.commit()
    return path


def mounted_store(state_home: Path, data_root: Path) -> SnapshotStore:
    data_root.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(
        state_home, data_root, mount_check=lambda _path: True,
        space_check=lambda: True, bucket_name="test-owner/test-bucket",
    )
    assert store.prepare()
    return store


def snapshots(store: SnapshotStore) -> list[Path]:
    return sorted(store.directory.glob("chat-state-*.json"))


def prohibit_bucket_sqlite(monkeypatch, data_root):
    connect = sqlite3.connect

    def local_only(database, *args, **kwargs):
        assert str(data_root) not in str(database), "SQLite must never open bucket files"
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", local_only)


def test_snapshot_is_sqlite_backup_allowlisted_scrubbed_and_checksummed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    make_state_db(home)
    store = mounted_store(home, tmp_path / "mounted-data")
    prohibit_bucket_sqlite(monkeypatch, store.data_root)

    assert store.snapshot()
    manifests = snapshots(store)
    assert len(manifests) == 1
    metadata = json.loads(manifests[0].read_text())
    snapshot = manifests[0].parent / metadata["database"]
    assert metadata["format"] == 1
    assert metadata["size"] == snapshot.stat().st_size
    payload = snapshot.read_bytes()
    assert b"private-provider-secret" not in payload
    local_inspection = tmp_path / "inspect.db"
    local_inspection.write_bytes(payload)
    with sqlite3.connect(local_inspection) as db:
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == {"sessions", "messages", "gateway_routing"}
        session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        assert {"model_config", "system_prompt", "billing_base_url"}.isdisjoint(session_columns)
        assert db.execute("SELECT title FROM sessions").fetchone() == ("A conversation",)
        assert db.execute("SELECT content FROM messages").fetchone() == ("hello continuation",)
        routing = json.loads(db.execute("SELECT entry_json FROM gateway_routing").fetchone()[0])
        assert routing["metadata"] == {"thread_ts": "safe-watermark", "nested": {"label": "safe"}}
        assert "model_override" not in routing
        assert routing["active_turn_token"] == "local-turn-id"


def test_snapshot_and_restore_fail_closed_after_mount_disappears(tmp_path):
    source_home = tmp_path / "source-home"
    make_state_db(source_home)
    data_root = tmp_path / "mounted-data"
    data_root.mkdir()
    mounted = [True]
    publisher = SnapshotStore(
        source_home, data_root, mount_check=lambda _path: mounted[0],
        space_check=lambda: True, bucket_name="test-owner/test-bucket",
    )
    assert publisher.prepare()
    assert publisher.snapshot()
    mounted[0] = False
    assert not publisher.snapshot()
    assert not publisher.enabled
    assert len(snapshots(publisher)) == 1

    restored_home = tmp_path / "restored-home"
    restored_home.mkdir()
    mounted[0] = True
    recovery = SnapshotStore(
        restored_home, data_root, mount_check=lambda _path: mounted[0],
        space_check=lambda: True, bucket_name="test-owner/test-bucket",
    )
    assert recovery.prepare()
    mounted[0] = False
    assert recovery.restore_latest() == ""
    assert not recovery.enabled
    assert not (restored_home / "state.db").exists()


def test_restore_uses_newest_valid_snapshot_without_clobbering_config_or_history(tmp_path, monkeypatch):
    data_root = tmp_path / "mounted-data"
    prohibit_bucket_sqlite(monkeypatch, data_root)
    first_home = tmp_path / "first-home"
    make_state_db(first_home, title="older history")
    first = mounted_store(first_home, data_root)
    assert first.snapshot()

    # A second valid snapshot is newer; corrupt its bytes but leave its commit
    # manifest in place to exercise newest-valid fallback rather than newest-name.
    newer_home = tmp_path / "newer-home"
    make_state_db(newer_home, title="newer history")
    newer_publisher = mounted_store(newer_home, data_root)
    assert newer_publisher.snapshot()
    newer_manifest = snapshots(newer_publisher)[-1]
    newer_metadata = json.loads(newer_manifest.read_text())
    newer_db = newer_manifest.parent / newer_metadata["database"]
    newer_db.write_bytes(newer_db.read_bytes() + b"corruption")

    restored_home = tmp_path / "restored-home"
    restored_home.mkdir()
    (restored_home / "config.yaml").write_text("model: configured\n")
    restored = mounted_store(restored_home, data_root)
    message = restored.restore_latest()
    assert "Restored the latest validated" in message
    assert (restored_home / "config.yaml").read_text() == "model: configured\n"
    with sqlite3.connect(restored_home / "state.db") as db:
        assert db.execute("SELECT title FROM sessions").fetchone() == ("older history",)
        assert db.execute("SELECT content FROM messages").fetchone() == ("hello continuation",)

    # Any real local history wins over the remote snapshot.
    local_home = tmp_path / "local-home"
    make_state_db(local_home, title="local must win")
    local = mounted_store(local_home, data_root)
    assert "preserved" in local.restore_latest()
    with sqlite3.connect(local_home / "state.db") as db:
        assert db.execute("SELECT title FROM sessions").fetchone() == ("local must win",)


def test_config_only_home_is_recoverable_and_unmounted_image_dir_stays_ephemeral(tmp_path):
    data_root = tmp_path / "mounted-data"
    source = tmp_path / "source"
    make_state_db(source)
    publisher = mounted_store(source, data_root)
    assert publisher.snapshot()

    config_only = tmp_path / "config-only"
    config_only.mkdir()
    (config_only / "config.yaml").write_text("model: configured\n")
    recovery = mounted_store(config_only, data_root)
    assert "Restored" in recovery.restore_latest()
    assert (config_only / "config.yaml").exists()
    assert (config_only / "state.db").exists()

    ordinary_directory = tmp_path / "image-data"
    ordinary_directory.mkdir()
    ephemeral = SnapshotStore(config_only, ordinary_directory)
    assert not ephemeral.prepare()
    assert "Ephemeral history" in ephemeral.status_message()
    assert not (ordinary_directory / "hermes-state").exists()


def test_bucket_name_alone_does_not_enable_snapshot_writes(tmp_path, monkeypatch):
    state_home = tmp_path / "home"
    state_home.mkdir()
    data_root = tmp_path / "mounted-data"
    data_root.mkdir()
    monkeypatch.setenv("BUCKET_NAME", "test-owner/test-bucket")
    monkeypatch.delenv("SPACE_ID", raising=False)
    store = SnapshotStore(state_home, data_root, mount_check=lambda _path: True)
    assert not store.prepare()
    assert not (data_root / "hermes-state").exists()

    monkeypatch.setenv("SPACE_ID", "test-owner/test-space")
    assert store.prepare()
    assert store.directory == data_root / "hermes-state" / "test-owner" / "test-bucket"


def test_mount_detector_requires_exact_mountpoint_and_rejects_symlink(tmp_path):
    mountpoint = tmp_path / "data mount"
    mountpoint.mkdir()
    mountinfo = tmp_path / "mountinfo"
    escaped = str(mountpoint).replace(" ", r"\040")
    mountinfo.write_text(f"44 30 0:40 / {escaped} rw,relatime - fuse test rw\n")
    assert is_mountpoint(mountpoint, mountinfo)
    assert not is_mountpoint(mountpoint / "nested", mountinfo)
    link = tmp_path / "data-link"
    link.symlink_to(mountpoint, target_is_directory=True)
    assert not is_mountpoint(link, mountinfo)


def test_live_application_database_restores_messages_and_routing(tmp_path, monkeypatch):
    """A real SessionDB can reopen, read, and continue a restored WAL-backed chat."""
    from hermes_state import SessionDB
    from gateway.session import SessionEntry

    source_home = tmp_path / "source-real"
    source_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    live = SessionDB(source_home / "state.db")
    try:
        live.create_session("conversation-1", "telegram", model="openrouter/free")
        live.append_message("conversation-1", "user", "Remember our migration plan")
        live._conn.execute(
            "UPDATE messages SET display_kind=?, display_metadata=? WHERE session_id=?",
            ("reaction", json.dumps({"reaction": "thumbs-up"}), "conversation-1"),
        )
        live._conn.execute(
            "INSERT INTO conversation_generations(source,session_key,generation) VALUES(?,?,?)",
            ("telegram", "telegram:42", 9),
        )
        live.save_gateway_routing_entry("telegram:42", json.dumps({
            "session_key": "telegram:42", "session_id": "conversation-1",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:01:00",
            "platform": "telegram", "chat_type": "dm",
        }))
        publisher = mounted_store(source_home, tmp_path / "real-mount")
        # Keep the application connection open: committed WAL data must be captured.
        assert publisher.snapshot(), publisher.last_error
    finally:
        live.close()

    restored_home = tmp_path / "restored-real"
    recovery = mounted_store(restored_home, tmp_path / "real-mount")
    assert "Restored" in recovery.restore_latest()
    monkeypatch.setenv("HERMES_HOME", str(restored_home))
    restored = SessionDB(restored_home / "state.db")
    try:
        messages = restored.get_messages("conversation-1")
        assert messages[0]["content"] == "Remember our migration plan"
        display_row = restored._conn.execute(
            "SELECT display_kind,display_metadata FROM messages WHERE session_id=?",
            ("conversation-1",),
        ).fetchone()
        assert display_row[0] == "reaction"
        assert json.loads(display_row[1]) == {"reaction": "thumbs-up"}
        generation_row = restored._conn.execute(
            "SELECT generation FROM conversation_generations WHERE source=? AND session_key=?",
            ("telegram", "telegram:42"),
        ).fetchone()
        assert generation_row[0] == 9
        routing = restored.load_gateway_routing_entries()
        entry = SessionEntry.from_dict(json.loads(routing["telegram:42"]))
        assert entry.session_id == "conversation-1"
        restored.append_message(entry.session_id, "assistant", "The plan is still here")
        assert len(restored.get_messages(entry.session_id)) == 2
    finally:
        restored.close()


def test_local_tenant_history_and_access_recover_and_continue_without_other_tenant_data(tmp_path, monkeypatch):
    from agent.local_tenants import LocalTenantStore
    from agent.mongo_tenants import Principal
    from gateway.tenant_runtime import room_key
    from hermes_state import SessionDB

    owner_id = "synthetic-owner-identity"
    source_home = tmp_path / "tenant-source"
    source_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    live = SessionDB(source_home / "state.db")
    live.create_session("owner-room", "telegram")
    live.append_message("owner-room", "user", "Retain the primary conversation too")
    tenants = LocalTenantStore(source_home, owner_id)
    owner = Principal(owner_id)
    tenants.allow_user(owner, "42", True)
    tenants.ban_user(owner, "43", True)
    tenants.put(owner, "settings", "private", {"api_key": "tenant-private-marker"})
    tenants.start_user("44")
    tenants.grant_sudo(owner, "44", "-10044")
    user = Principal("42")
    history_key = "history:" + room_key("42", "chat-42")
    original_history = [
        {"role": "user", "content": "Remember the project plan", "user_id": "42", "chat_id": "chat-42"},
        {"role": "assistant", "content": "We will resume from the milestones", "user_id": "42", "chat_id": "chat-42"},
    ]
    tenants.put(user, "profile", history_key, original_history)
    tenants.put(user, "settings", "private", {"api_key": "user-private-marker"})
    scoped_principal = tenants.principal_for_scope("44", "group", "-10044")
    scoped_history_key = "history:" + room_key("44", "-10044")
    scoped_history = [
        {"role": "user", "content": "Group-only context", "user_id": "44", "chat_id": "-10044"},
        {"role": "assistant", "content": "This remains in that group", "user_id": "44", "chat_id": "-10044"},
    ]
    tenants.put(scoped_principal, "profile", scoped_history_key, scoped_history)

    data_root = tmp_path / "tenant-mount"
    publisher = mounted_store(source_home, data_root)
    try:
        assert publisher.snapshot(), publisher.last_error
    finally:
        live.close()

    manifest = snapshots(publisher)[0]
    metadata = json.loads(manifest.read_text())
    archive = manifest.parent / metadata["database"]
    payload = archive.read_bytes()
    assert owner_id.encode() not in payload
    assert b"tenant-private-marker" not in payload
    assert b"user-private-marker" not in payload
    with sqlite3.connect(archive) as db:
        access = {row[0]: row[1:] for row in db.execute(
            "SELECT tenant_id,user_id,allowed,banned,authorization_generation,credits,weight,start_count FROM tenant_access"
        )}
        assert set(access) == {"tg-42", "tg-43", "tg-44"}
        assert access["tg-42"][0:2] == ("42", 1)
        assert access["tg-43"][0:2] == ("43", 0)
        scoped = db.execute(
            "SELECT allowed_scopes,sudo_scopes FROM tenant_access WHERE tenant_id='tg-44'"
        ).fetchone()
        assert json.loads(scoped[0]) == ["group:-10044"]
        assert json.loads(scoped[1]) == ["group:-10044"]
        history_rows = db.execute(
            "SELECT tenant_id,user_id,chat_id,room_key,payload FROM tenant_history"
        ).fetchall()
        history_by_tenant = {row[0]: row[1:] for row in history_rows}
        assert set(history_by_tenant) == {"tg-42", "tg-44"}
        assert history_by_tenant["tg-42"][:3] == ("42", "chat-42", room_key("42", "chat-42"))
        assert json.loads(history_by_tenant["tg-42"][3]) == original_history
        assert history_by_tenant["tg-44"][:3] == ("44", "-10044", room_key("44", "-10044"))
        assert json.loads(history_by_tenant["tg-44"][3]) == scoped_history

    restored_home = tmp_path / "tenant-restored"
    restored = mounted_store(restored_home, data_root)
    assert "tenant access and approved conversation history were restored" in restored.restore_latest()
    # Simulate first-run allowlist configuration in a fresh image; it cannot
    # overwrite restored authorization or resurrect a revoked/banned identity.
    reopened = LocalTenantStore(restored_home, owner_id, allowed_user_ids=["43"])
    assert reopened.get_access("42") == {"allowed": True, "banned": False}
    assert reopened.get_access("43") == {"allowed": False, "banned": True}
    assert reopened.get(Principal(owner_id), "settings", "private") is None
    resumed = reopened.get(Principal("42"), "profile", history_key)
    assert resumed == original_history
    scoped_resumed = reopened.get(
        reopened.principal_for_scope("44", "group", "-10044"), "profile", scoped_history_key
    )
    assert scoped_resumed == scoped_history
    assert reopened.access_for_scope("44", "group", "-10044")["sudo"] is True
    assert reopened.access_for_scope("44", "group", "-10045")["allowed"] is False
    continued_history = resumed + [
        {"role": "user", "content": "What was the first milestone?", "user_id": "42", "chat_id": "chat-42"},
        {"role": "assistant", "content": "We planned the data migration first", "user_id": "42", "chat_id": "chat-42"},
    ]
    reopened.put(Principal("42"), "profile", history_key, continued_history)
    assert reopened.get(Principal("42"), "profile", history_key) == continued_history
    assert reopened.get(Principal("42"), "settings", "private") is None

    # Recovery must be able to publish its next snapshot without stale tenant
    # bundle tables in state.db causing "table already exists" failures.
    assert restored.snapshot(), restored.last_error
    second_home = tmp_path / "second-recovery"
    assert "Restored" in mounted_store(second_home, data_root).restore_latest()
    assert LocalTenantStore(second_home, owner_id).get(Principal("42"), "profile", history_key) == continued_history
    # A surviving main DB must not prevent recovery of a missing tenant store.
    partial_home = tmp_path / "surviving-main"
    surviving_db = make_state_db(partial_home)
    surviving_bytes = surviving_db.read_bytes()
    partial = mounted_store(partial_home, data_root)
    assert "recovered missing tenant state" in partial.restore_latest()
    assert surviving_db.read_bytes() == surviving_bytes
    assert LocalTenantStore(partial_home, owner_id).get(Principal("42"), "profile", history_key) == continued_history

    interrupted = mounted_store(tmp_path / "interrupted", data_root)
    with monkeypatch.context() as failure:
        def unavailable(_source):
            raise OSError("synthetic tenant publication failure")
        failure.setattr(interrupted, "_restore_tenant_access", unavailable)
        assert "tenant recovery failed" in interrupted.restore_latest()
    recovered_main = interrupted.db_path.read_bytes()
    assert "recovered missing tenant state" in interrupted.restore_latest()
    assert interrupted.db_path.read_bytes() == recovered_main
    assert LocalTenantStore(interrupted.state_home, owner_id).get(Principal("42"), "profile", history_key) == continued_history

    # Existing tenant state is never replaced, even if chat state is absent.
    occupied_home = tmp_path / "tenant-occupied"
    occupied = LocalTenantStore(occupied_home, owner_id)
    occupied.allow_user(Principal(owner_id), "42", False)
    protected = mounted_store(occupied_home, data_root)
    assert "Restored" in protected.restore_latest()
    assert LocalTenantStore(occupied_home, owner_id).get_access("42") == {
        "allowed": False, "banned": False,
    }


def test_orphan_sidecar_without_state_db_blocks_restore(tmp_path):
    source_home = tmp_path / "source"
    make_state_db(source_home)
    data_root = tmp_path / "sidecar-mount"
    assert mounted_store(source_home, data_root).snapshot()
    restored_home = tmp_path / "sidecar-restored"
    restored_home.mkdir()
    wal = restored_home / "state.db-wal"
    wal.write_bytes(b"unknown uncheckpointed data")
    recovery = mounted_store(restored_home, data_root)
    assert "unreadable" in recovery.restore_latest()
    assert not recovery.db_path.exists()
    assert wal.read_bytes() == b"unknown uncheckpointed data"


def test_unwritable_mount_reports_ephemeral_and_keeps_local_history(tmp_path, monkeypatch):
    import persistence

    home = tmp_path / "permission-home"
    database = make_state_db(home)
    original = database.read_bytes()
    data_root = tmp_path / "read-only-mount"
    data_root.mkdir()
    store = SnapshotStore(
        home, data_root, mount_check=lambda _path: True,
        space_check=lambda: True, bucket_name="test-owner/test-bucket",
    )

    def denied(*_args, **_kwargs):
        raise PermissionError("mount is read-only")

    monkeypatch.setattr(persistence.tempfile, "mkstemp", denied)
    assert not store.prepare()
    assert "Ephemeral history" in store.status_message()
    assert not store.snapshot()
    assert database.read_bytes() == original


def test_interrupted_publication_preserves_prior_snapshot_and_cleans_pending(tmp_path, monkeypatch):
    import persistence

    source = tmp_path / "publish-home"
    make_state_db(source)
    data_root = tmp_path / "publish-mount"
    publisher = mounted_store(source, data_root)
    assert publisher.snapshot()
    prior_manifest = snapshots(publisher)[0]
    real_replace = persistence.os.replace

    def fail_commit_marker(source_path, target_path):
        if Path(target_path).suffix == ".json":
            raise OSError("synthetic interrupted manifest publication")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(persistence.os, "replace", fail_commit_marker)
    assert not publisher.snapshot()
    assert "Ephemeral history warning" in publisher.status_message()
    assert snapshots(publisher) == [prior_manifest]
    assert not list(publisher.directory.glob(".pending-*"))
    monkeypatch.setattr(persistence.os, "replace", real_replace)
    recovery = mounted_store(tmp_path / "publish-restored", data_root)
    assert "Restored" in recovery.restore_latest()
    # An orphaned final DB can be a delayed remote manifest listing; never delete it.
    assert len(list(recovery.directory.glob("chat-state-*.sqlite3"))) == 2
    with sqlite3.connect(recovery.db_path) as db:
        assert db.execute("SELECT content FROM messages").fetchone() == ("hello continuation",)


def test_hf_runtime_bootstrap_is_not_written_logged_or_snapshotted(tmp_path, monkeypatch, caplog):
    import tarfile
    import snapshot
    from gateway.mongo_multiuser import (
        _HF_RUNTIME_POLICY, _profile_env, enabled, hf_runtime_environment, owner_user_id,
    )

    home = tmp_path / "home"
    make_state_db(home)
    (home / "config.yaml").write_text("model: openrouter/free\n")
    owner, tester = "874512341234321", "874512341234322"
    forbidden = [
        owner.encode(), tester.encode(), b"OWNER_TELEGRAM_ID",
        b"TELEGRAM_ALLOWED_USERS", _HF_RUNTIME_POLICY.encode(),
    ]
    before = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}
    runtime = hf_runtime_environment(home, {
        "OWNER_TELEGRAM_ID": owner, "TELEGRAM_ALLOWED_USERS": tester,
    })
    monkeypatch.setenv(_HF_RUNTIME_POLICY, runtime[_HF_RUNTIME_POLICY])
    assert enabled(home) and owner_user_id(home) == owner
    assert _profile_env(home)["TELEGRAM_ALLOWED_USERS"] == tester
    assert not (home / ".env").exists()
    assert {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()} == before
    assert not any(value.decode() in caplog.text for value in forbidden)

    # Even secret-inclusive offline exports must not collect process environment.
    archive = tmp_path / "offline.tar.gz"
    snapshot.create(home, archive, secrets=True, quiesced=True)
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isfile():
                with tar.extractfile(member) as stream:
                    content = stream.read()
                assert not any(value in content for value in forbidden)

    data = tmp_path / "bucket"
    store = mounted_store(home, data)
    assert store.snapshot()
    for root in (home, data):
        for path in root.rglob("*"):
            if path.is_file():
                assert not any(value in path.read_bytes() for value in forbidden)
