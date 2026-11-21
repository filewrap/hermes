import mongomock
import pytest

from gateway import mongo_release


@pytest.fixture
def release_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OWNER_TELEGRAM_ID=100\nMULTIUSER_ENABLED=false\n")
    monkeypatch.setattr(mongo_release, "source_state", lambda: ("a" * 40, True))
    return tmp_path, mongomock.MongoClient().hermes


def record(home, db, **overrides):
    kwargs = dict(inventory_digest="b" * 64, records_verified=3,
                  regression={"passed": 10, "failed": 0, "errors": 0, "skipped": 0},
                  migration_verified=True, database=db)
    kwargs.update(overrides)
    return mongo_release.record_verified_release(home, "100", **kwargs)


def test_no_release_until_explicit_verified_migration(release_env):
    home, db = release_env
    assert not mongo_release.release_ready(home, database=db)
    record(home, db)
    assert mongo_release.release_ready(home, database=db)


def test_dirty_or_changed_source_denies_start(release_env, monkeypatch):
    home, db = release_env
    record(home, db)
    monkeypatch.setattr(mongo_release, "source_state", lambda: ("a" * 40, False))
    assert not mongo_release.release_ready(home, database=db)
    monkeypatch.setattr(mongo_release, "source_state", lambda: ("c" * 40, True))
    assert not mongo_release.release_ready(home, database=db)


@pytest.mark.parametrize("overrides", [
    {"migration_verified": False}, {"records_verified": 0},
    {"inventory_digest": "not-a-digest"},
    {"regression": {"passed": 315, "failed": 1, "errors": 0}},
    {"regression": {"passed": 0, "failed": 0, "errors": 0}},
])
def test_partial_or_failed_evidence_never_attests(release_env, overrides):
    home, db = release_env
    with pytest.raises(mongo_release.ReleaseGateError):
        record(home, db, **overrides)
    assert db.hermes_release.count_documents({}) == 0


def test_nonowner_and_other_owner_marker_are_denied(release_env):
    home, db = release_env
    with pytest.raises(mongo_release.ReleaseGateError, match="Only"):
        mongo_release.record_verified_release(home, "200", inventory_digest="b" * 64,
            records_verified=3, regression={"passed": 10, "failed": 0, "errors": 0},
            migration_verified=True, database=db)
    marker = record(home, db)
    marker["owner_id"] = "200"
    db.hermes_release.replace_one({"_id": marker["_id"]}, marker)
    assert not mongo_release.release_ready(home, database=db)


def test_marker_database_failure_is_fail_closed(release_env, monkeypatch):
    home, _db = release_env
    def unavailable(_home):
        raise RuntimeError("mongodb://user:private-password@example.invalid")
    monkeypatch.setattr(mongo_release, "_database", unavailable)
    assert not mongo_release.release_ready(home)
    with pytest.raises(mongo_release.ReleaseGateError) as error:
        mongo_release.assert_release_ready(home)
    assert "private-password" not in str(error.value)
