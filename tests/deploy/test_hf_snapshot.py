"""HF archival helpers operate on isolated homes, never the running service."""

import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

MODULE = Path(__file__).parents[2] / "deploy" / "huggingface" / "snapshot.py"
spec = importlib.util.spec_from_file_location("hf_snapshot", MODULE)
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


def test_create_restore_backups_wal_and_excludes_credentials(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db_path = home / "sessions?#.db"
    db = sqlite3.connect(db_path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE items(value TEXT)")
        db.execute("INSERT INTO items VALUES ('committed in wal')")
        db.commit()
        (home / ".env").write_text("SECRET=not-in-snapshot")
        (home / "auth.json").write_text("{}")
        (home / "telegram.session").write_bytes(b"session credential")
        (home / "settings.yaml").write_text("ok: true\n")
        script = home / "skills" / "tool.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        archive = tmp_path / "snapshot.tar.gz"
        snapshot.create(home, archive, secrets=False, quiesced=True)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
            assert not {".env", "auth.json", "telegram.session"} & set(names)
            assert not any(name.endswith(("-wal", "-shm")) for name in names)
            manifest = json.load(tar.extractfile(snapshot.MANIFEST))
            assert db_path.name in manifest["files"]
            assert "SECRET" not in json.dumps(manifest)
    finally:
        db.close()
    dest = tmp_path / "restored"
    snapshot.restore(archive, dest)
    with sqlite3.connect(dest / db_path.name) as restored:
        assert (
            restored.execute("SELECT value FROM items").fetchone()[0]
            == "committed in wal"
        )
    assert (dest / "settings.yaml").read_text() == "ok: true\n"
    assert (dest / "skills/tool.sh").stat().st_mode & 0o777 == 0o700
    assert dest.stat().st_mode & 0o777 == 0o700
    assert archive.stat().st_mode & 0o777 == 0o600


def test_create_requires_quiescence_and_will_not_overwrite_or_nest(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(ValueError, match="--quiesced"):
        snapshot.create(home, tmp_path / "a.tar.gz", False, False)
    with pytest.raises(ValueError, match="inside"):
        snapshot.create(home, home / "backup.tar.gz", False, True)
    existing = tmp_path / "exists"
    existing.write_text("keep")
    with pytest.raises(ValueError, match="already exists"):
        snapshot.create(home, existing, False, True)
    assert existing.read_text() == "keep"


@pytest.mark.parametrize(
    "name", ["../escape", "/escape", "./alias", "a//alias", "a/../alias"]
)
def test_restore_refuses_unsafe_tar_paths(tmp_path, name):
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="unsafe archive path"):
        snapshot.restore(bad, tmp_path / "unsafe")
    assert not (tmp_path / "unsafe").exists()


def test_restore_preserves_nonempty_destination_and_detects_corruption(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "file.txt").write_text("original")
    archive = tmp_path / "ok.tar.gz"
    snapshot.create(home, archive, False, True)
    dest = tmp_path / "nonempty"
    dest.mkdir()
    (dest / "keep").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        snapshot.restore(archive, dest)
    assert (dest / "keep").read_text() == "keep"
    corrupt = tmp_path / "corrupt.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(corrupt, "w:gz") as out:
        for member in src.getmembers():
            payload = src.extractfile(member).read()
            if member.name == "file.txt":
                payload = b"tampered"
                member.size = len(payload)
            out.addfile(member, io.BytesIO(payload))
    with pytest.raises(ValueError, match="checksum mismatch"):
        snapshot.restore(corrupt, tmp_path / "corrupt-home")
    assert not (tmp_path / "corrupt-home").exists()


def test_secret_optin_and_symlink_rejection(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("private test data")
    archive = tmp_path / "private.tar.gz"
    snapshot.create(home, archive, True, True)
    snapshot.restore(archive, tmp_path / "restored")
    assert (tmp_path / "restored/.env").read_text() == "private test data"
    linked = tmp_path / "linked"
    linked.symlink_to(home, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        snapshot.create(linked, tmp_path / "link.tar.gz", False, True)
    (home / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        snapshot.create(home, tmp_path / "escape.tar.gz", False, True)
    with pytest.raises(ValueError, match="symlink"):
        snapshot.restore(archive, linked)
