"""Failure-safety and idempotence tests for Mongo SQL-cache publication."""

import sqlite3

import mongomock
import pytest
from pymongo.errors import NetworkTimeout, OperationFailure

from agent import mongo_sql_cache


@pytest.fixture

def snapshot():
    """Build a small deterministic snapshot without touching a home database."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany("INSERT INTO sample VALUES (?, ?)", [(1, "synthetic"), (2, "second"), (3, "third")])
        return mongo_sql_cache._snapshot(connection)
    finally:
        connection.close()


def _database():
    return mongomock.MongoClient().hermes


def _cache_error(call):
    with pytest.raises(mongo_sql_cache.SQLCacheError) as raised:
        call()
    return raised.value


def test_versions_update_operation_failure_has_sanitized_stage_diagnostics(snapshot):
    db = _database()
    versions = db.hermes_sql_versions
    original_update = versions.update_one

    def fail_update(*args, **kwargs):
        raise OperationFailure("secret URI and payload", code=1234)

    versions.update_one = fail_update
    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))

    assert error.stage == "stage_manifest"
    assert error.driver_class == "OperationFailure"
    assert error.code == 1234
    assert "secret URI" not in str(error)
    assert "payload" not in str(error)
    assert "secret URI" not in repr(error)
    assert db.hermes_sql_heads.count_documents({}) == 0
    versions.update_one = original_update


def test_missing_row_after_failed_insert_retains_stage_rows_diagnostic(snapshot):
    db = _database()
    rows = db.hermes_sql_rows

    def fail_insert(*args, **kwargs):
        raise OperationFailure("safe injected insert diagnostic", code=1234)

    rows.insert_many = fail_insert
    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))

    assert error.stage == "stage_rows"
    assert error.driver_class == "OperationFailure"
    assert error.code == 1234
    assert db.hermes_sql_heads.count_documents({}) == 0


def test_partial_rows_and_lost_insert_ack_resume_without_unrelated_mutation(snapshot):
    db = _database()
    rows, versions, heads = mongo_sql_cache._collections(db)
    unrelated_row = {"_id": "unrelated-row", "payload": {"value": "keep"}}
    unrelated_version = {"_id": "unrelated-version", "marker": "keep"}
    unrelated_head = {"_id": "state:unrelated", "revision": 9, "version": "unrelated-version"}
    rows.insert_one(unrelated_row)
    versions.insert_one(unrelated_version)
    heads.insert_one(unrelated_head)

    # Seed a proper subset of expected immutable blobs, then emulate the server
    # applying the rest of an insert while the acknowledgement is lost.
    blobs = {}
    for table in snapshot["tables"].values():
        for row in table["rows"].values():
            blobs[mongo_sql_cache._digest(row)] = row
    expected = list(blobs.items())
    if expected:
        rows.insert_one({"_id": expected[0][0], "payload": expected[0][1]})
    original_insert = rows.insert_many
    calls = {"count": 0}
    original_snapshot_row = rows.find_one({"_id": "unrelated-row"})

    def lose_first_ack(documents, *args, **kwargs):
        calls["count"] += 1
        result = original_insert(documents, *args, **kwargs)
        if calls["count"] == 1:
            raise OperationFailure("lost acknowledgement")
        return result

    rows.insert_many = lose_first_ack
    assert mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True) == 1
    # Repeating bootstrap with the same snapshot is idempotent.
    assert mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True) == 1
    assert calls["count"] == 1

    assert rows.count_documents({}) == len(set(blobs) | {"unrelated-row"})
    assert versions.find_one({"_id": "unrelated-version"}) == unrelated_version
    assert heads.find_one({"_id": "state:unrelated"}) == unrelated_head
    assert rows.find_one({"_id": "unrelated-row"}) == original_snapshot_row
    assert heads.find_one({"_id": "state:ns"})["revision"] == 1
    assert mongo_sql_cache._resolve_manifest(
        versions.find_one({"_id": heads.find_one({"_id": "state:ns"})["version"]}), rows, "ns"
    ) == snapshot


def test_mismatching_staged_row_fails_before_head_publication(snapshot):
    db = _database()
    rows, _, heads = mongo_sql_cache._collections(db)
    table = next(iter(snapshot["tables"].values()))
    row = next(iter(table["rows"].values()))
    rid = mongo_sql_cache._digest(row)
    rows.insert_one({"_id": rid, "payload": {"id": 999, "value": "wrong"}})

    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))

    assert error.stage == "verify_rows"
    assert heads.count_documents({}) == 0


def test_existing_row_read_timeout_has_sanitized_diagnostics(snapshot):
    db = _database()
    rows = db.hermes_sql_rows

    def fail_find(*args, **kwargs):
        raise NetworkTimeout("secret endpoint")

    rows.find = fail_find
    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))

    assert error.stage == "read_existing_rows"
    assert error.category == "network"
    assert error.driver_class == "NetworkTimeout"
    assert "secret endpoint" not in str(error)
    assert "secret endpoint" not in repr(error)


def test_large_publication_uses_bounded_queries_and_resolves_idempotently():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO sample VALUES (?, ?)", ((i, f"row-{i}") for i in range(150)))
    snapshot = mongo_sql_cache._snapshot(connection)
    connection.close()

    db = _database()
    rows = db.hermes_sql_rows
    original_find = rows.find
    original_insert = rows.insert_many
    expected_ids = {mongo_sql_cache._digest(row) for table in snapshot["tables"].values() for row in table["rows"].values()}
    query_sizes = []
    insert_sizes = []

    def bounded_find(query, *args, **kwargs):
        identifiers = query["_id"]["$in"]
        assert set(identifiers) <= expected_ids
        query_sizes.append(len(identifiers))
        if len(identifiers) > 64:
            raise NetworkTimeout("secret endpoint")
        return original_find(query, *args, **kwargs)

    def bounded_insert(documents, *args, **kwargs):
        assert all(document["_id"] in expected_ids for document in documents)
        assert 1 <= len(documents) <= 64
        insert_sizes.append(len(documents))
        return original_insert(documents, *args, **kwargs)

    rows.find = bounded_find
    rows.insert_many = bounded_insert
    assert mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True) == 1
    head = db.hermes_sql_heads.find_one({"_id": "state:ns"})
    manifest = db.hermes_sql_versions.find_one({"_id": head["version"]})
    resolved = mongo_sql_cache._resolve_manifest(manifest, rows, "ns")

    assert mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True) == 1
    assert all(1 <= size <= 64 for size in query_sizes)
    assert insert_sizes == [64, 64, 22]
    assert len(expected_ids) == 150
    assert rows.count_documents({}) == 150
    assert resolved == snapshot
    assert head["revision"] == 1


@pytest.mark.parametrize("failure_stage", ["verify_rows", "verify_manifest", "publish_head"])
def test_publication_failures_report_stage_and_safe_diagnostics(snapshot, failure_stage):
    db = _database()
    rows, versions, heads = mongo_sql_cache._collections(db)
    target = {"verify_rows": rows, "verify_manifest": versions, "publish_head": heads}[failure_stage]
    method = "find" if failure_stage == "verify_rows" else "find_one" if failure_stage == "verify_manifest" else "update_one"
    original = getattr(target, method)
    calls = {"count": 0}

    def fail(*args, **kwargs):
        if failure_stage == "verify_rows":
            calls["count"] += 1
            if calls["count"] == 1:
                return original(*args, **kwargs)
        raise NetworkTimeout("secret endpoint")

    setattr(target, method, fail)
    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))
    setattr(target, method, original)

    assert error.stage == failure_stage
    assert error.category == "network"
    assert error.driver_class == "NetworkTimeout"
    assert "secret endpoint" not in str(error)
    assert "secret endpoint" not in repr(error)


def test_permanent_staging_failure_stops_after_one_bounded_batch_without_retry():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    connection.executemany("INSERT INTO sample VALUES (?, ?)", ((i, f"row-{i}") for i in range(150)))
    snapshot = mongo_sql_cache._snapshot(connection)
    connection.close()
    db = _database()
    calls = []

    def denied(documents, *args, **kwargs):
        calls.append(len(documents))
        raise OperationFailure("secret server details", code=13)

    db.hermes_sql_rows.insert_many = denied
    error = _cache_error(lambda: mongo_sql_cache._publish(db, snapshot, None, "ns", create_only=True))
    assert calls == [64]
    assert error.stage == "stage_rows" and error.code == 13
    assert error.driver_class == "OperationFailure" and error.retry_count == 0
    assert "secret server details" not in str(error)
    assert db.hermes_sql_heads.count_documents({}) == 0
    assert db.hermes_sql_versions.count_documents({}) == 0
