"""Filesystem/SQLite contract tests for local Hugging Face diagnostics."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.error import HTTPError

import pytest

from plugins.platforms.telegram import hf_diagnostics as diagnostics


@pytest.fixture(autouse=True)
def no_real_hf_credential(monkeypatch):
    monkeypatch.setattr(diagnostics, "_hf_token", lambda: "")


def _make_state_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT)")
        db.executemany("INSERT INTO sessions(id) VALUES (?)", [("s1",), ("s2",)])
        db.executemany("INSERT INTO messages(session_id) VALUES (?)", [("s1",), ("s1",), ("s2",)])


def _make_tenant_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tenant_documents(collection TEXT, id TEXT, body TEXT)")
        db.execute("CREATE TABLE tenant_metadata(key TEXT, value TEXT)")
        db.executemany("INSERT INTO tenant_documents VALUES (?, ?, ?)", [
            ("hermes_tenant_profiles", "owner", '{"_id":"owner"}'),
            ("hermes_tenant_data", "alice", '{"_id":"alice"}'),
        ])
        db.execute("INSERT INTO tenant_metadata VALUES ('ready', '1')")


def test_report_checks_real_local_state_and_snapshot_pairs_without_leaking_config(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    _make_state_db(home / "state.db")
    _make_tenant_db(home / "tenant-state.db")
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: openrouter/free\n"
        "  base_url: https://user:password@example.invalid/v1\n"
        "api_key: sk-proj-abcdefghijklmnopqrstuvwxyz1234567890\n",
        encoding="utf-8",
    )

    data = tmp_path / "data"
    snapshots = data / "hermes-state"
    snapshots.mkdir(parents=True)
    (snapshots / "chat-state-0001.sqlite3").write_bytes(b"snapshot-bytes")
    (snapshots / "chat-state-0001.json").write_text("{}", encoding="utf-8")
    (snapshots / "chat-state-symlink.sqlite3").symlink_to(snapshots / "chat-state-0001.sqlite3")
    (snapshots / "chat-state-symlink.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "DATA_ROOT", data)
    monkeypatch.setattr(diagnostics, "_is_mountpoint", lambda _path: True)
    monkeypatch.setattr(diagnostics, "_hf_runtime", lambda: False)

    class ConnectedClient:
        def is_connected(self):
            return True

    class Adapter:
        _client = ConnectedClient()

    before_state = (home / "state.db").read_bytes()
    before_tenant = (home / "tenant-state.db").read_bytes()
    report = diagnostics.collect_hf_diagnostics(home, Adapter())

    headings = ["## STARTUP", "## DATABASE", "## TELEGRAM", "## MODEL / PROVIDERS"]
    assert [report.index(heading) for heading in headings] == sorted(report.index(h) for h in headings)
    assert "`sessions` 2" in report and "`messages` 3" in report
    assert "`tenant_documents` 2" in report and "`tenant_metadata` 1" in report
    assert "1 complete non-symlink pair(s)" in report
    assert "combined database + manifest size 16 bytes" in report
    assert "write probe: **passed**" in report
    assert "xkailashhh/hermes-artifacts" in report
    assert "expected, NOT verified identity" in report
    assert "Remote bucket listing, account quotas, and remote connectivity: **unknown**" in report
    assert "provider `openrouter`, model `openrouter/free`" in report
    assert "connected" in report and "local transport status only" in report
    assert "example.invalid" not in report
    assert "password" not in report
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890" not in report
    assert (home / "state.db").read_bytes() == before_state
    assert (home / "tenant-state.db").read_bytes() == before_tenant
    assert not list(data.glob(".hermes-diagnostics-write-check-*"))
    assert not list(snapshots.glob(".hermes-diagnostics-write-check-*"))


def test_supervisor_readyz_reads_http_error_body_but_reports_only_liveness(monkeypatch):
    error_body = json.dumps({
        "status": "failed",
        "detail": "Gateway process exited.",
        "note": "Process liveness only; messaging, model and database readiness are not tested.",
    }).encode()

    error = HTTPError("http://127.0.0.1:7860/readyz", 503, "unavailable", {}, None)
    error.read = lambda _size=-1: error_body
    monkeypatch.setattr(diagnostics, "urlopen", lambda url, timeout: (_ for _ in ()).throw(error))
    result = diagnostics._supervisor_status()
    assert "HTTP 503" in result
    assert "state `failed`" in result
    assert "process liveness only" in result
    assert "does not establish Telegram, model, or database readiness" in result


def test_missing_databases_and_unverified_mount_are_reported_without_creating_state(tmp_path, monkeypatch):
    home = tmp_path / "absent-home"
    data = tmp_path / "not-mounted"
    data.mkdir()
    monkeypatch.setattr(diagnostics, "DATA_ROOT", data)
    monkeypatch.setattr(diagnostics, "_is_mountpoint", lambda _path: False)
    monkeypatch.setattr(diagnostics, "_hf_runtime", lambda: False)

    report = diagnostics.collect_hf_diagnostics(home, None)
    assert "Live session database (`state.db`): check unavailable" in report
    assert "Local Telegram tenant database (`tenant-state.db`): check unavailable" in report
    assert "Dedicated `/data` mount: **not verified**" in report
    assert "write probe: **not run**" in report
    assert "`/readyz`: **not queried**" in report
    assert not home.exists()
    assert list(data.iterdir()) == []


@pytest.mark.parametrize("status,metadata,expected", [
    (200, {"id": diagnostics.EXPECTED_BUCKET, "private": True, "size": 54321, "totalFiles": 7}, "54,321 bytes across 7 files"),
    (200, {"id": diagnostics.EXPECTED_BUCKET, "private": False, "size": 0, "totalFiles": 0}, "PUBLIC — expected private"),
    (200, {"id": "wrong/bucket", "private": True, "size": 5, "totalFiles": 1}, "metadata validation failed"),
    (403, {"error": "sensitive response must not be exposed"}, "HTTP 403"),
    (302, {}, "HTTP 302"),
])
def test_remote_bucket_check_is_targeted_bounded_and_does_not_disclose_credentials(monkeypatch, status, metadata, expected):
    import httpx
    client_class = httpx.Client
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url == "https://huggingface.co/api/buckets/xkailashhh/hermes-artifacts"
        assert request.headers["authorization"] == "Bearer test-hf-credential"
        return httpx.Response(status, json=metadata)
    monkeypatch.setattr(diagnostics, "_hf_token", lambda: "test-hf-credential")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs))
    report = diagnostics._remote_bucket_status()
    assert expected in report
    assert len(requests) == 1
    assert "test-hf-credential" not in report
    assert "sensitive response" not in report


def test_snapshot_scan_limit_is_explicit_not_reported_as_empty(tmp_path, monkeypatch):
    directory = tmp_path / 'data' / 'hermes-state'
    directory.mkdir(parents=True)
    for number in range(4):
        (directory / f'unrelated-{number}').touch()
    monkeypatch.setattr(diagnostics, 'MAX_SNAPSHOT_DIRECTORY_ENTRIES', 2)
    monkeypatch.setattr(diagnostics, 'DATA_ROOT', directory.parent)
    monkeypatch.setattr(diagnostics, '_is_mountpoint', lambda _path: False)
    monkeypatch.setattr(diagnostics, '_hf_runtime', lambda: False)
    report = diagnostics.collect_hf_diagnostics(tmp_path / 'home', None)
    assert '**unavailable** (directory scan limit exceeded)' in report
    assert '0 complete non-symlink pair(s)' not in report
