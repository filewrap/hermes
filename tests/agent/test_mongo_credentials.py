"""Exercise the plaintext Mongo credential schema using an offline collection."""
import copy
import threading
from types import SimpleNamespace

import pytest

from agent import mongo_credentials as store


class MemoryCollection:
    def __init__(self):
        self.documents = {}
        self.lock = threading.Lock()

    def find_one(self, query):
        return copy.deepcopy(self.documents.get(query["_id"]))

    def insert_one(self, document):
        with self.lock:
            if document["_id"] in self.documents:
                raise RuntimeError("duplicate")
            self.documents[document["_id"]] = copy.deepcopy(document)

    def replace_one(self, query, document):
        with self.lock:
            existing = self.documents.get(query["_id"])
            matched = existing is not None and existing["revision"] == query["revision"]
            if matched:
                self.documents[query["_id"]] = copy.deepcopy(document)
            return SimpleNamespace(matched_count=int(matched))


@pytest.fixture
def mongo(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text("MONGODB_URI=mongodb://example.invalid/deep\n")
    collection = MemoryCollection()
    monkeypatch.setattr(store, "_collection", lambda settings: collection)
    monkeypatch.setattr(store._REVISIONS, "values", {}, raising=False)
    return home, collection


def test_plaintext_roundtrip_schema_and_identical_write_is_noop(mongo):
    home, collection = mongo
    value = {"providers": {"test": {"api_key": "offline-test-secret", "refresh_token": "refresh-test"}}}
    store.put_record(home, "auth", value)
    first = copy.deepcopy(next(iter(collection.documents.values())))
    assert store.get_record(home, "auth") == value
    store.put_record(home, "auth", value)
    assert next(iter(collection.documents.values())) == first
    assert first["payload"] == value
    assert first["schema_version"] == 2 and first["storage"] == "plaintext"
    assert first["name"] == "auth" and first["revision"] == 1
    assert set(first) == {"_id", "schema_version", "storage", "name", "payload", "revision"}


@pytest.mark.parametrize("name", ["snapshot", "arbitrary", "", "../auth", "mcp_oauth:bad server:tokens", "mcp_oauth:srv:unknown"])
def test_record_names_rejected(mongo, name):
    with pytest.raises(store.CredentialStoreError, match="record name"):
        store.get_record(mongo[0], name)


@pytest.mark.parametrize("name", ["env", "auth", "vault", "anthropic_oauth", "mcp_oauth:server-1:tokens", "mcp_oauth:server_1.client:client"])
def test_supported_names_roundtrip(mongo, name):
    store.put_record(mongo[0], name, {"token": "value"})
    assert store.get_record(mongo[0], name) == {"token": "value"}


def test_bootstrap_reads_legacy_names_as_canonical(mongo):
    home, _ = mongo
    (home / ".env").write_text(
        "DEEP_MONGODB_URI=mongodb://legacy.invalid/hermes\n"
        "DEEP_OWNER_TELEGRAM_ID=100\n"
        "MONGODB_DATABASE=isolated_migration_test\n"
    )
    values = store._bootstrap(home)
    assert values[store.URI_ENV] == "mongodb://legacy.invalid/hermes"
    assert values["OWNER_TELEGRAM_ID"] == "100"
    assert values[store.DATABASE_ENV] == "isolated_migration_test"
    assert "DEEP_MONGODB_URI" not in values


@pytest.mark.parametrize("field,value", [("schema_version", 1), ("storage", "encrypted"), ("algorithm", "AES-256-GCM")])
def test_legacy_or_unknown_schema_rejected_without_decryption(mongo, field, value):
    home, collection = mongo
    rid = store._record_id(home, "auth")
    collection.documents[rid] = {"_id": rid, "schema_version": 1, "storage": "encrypted", "name": "auth",
                                 "revision": 1, "ciphertext": b"not-decrypted", field: value}
    with pytest.raises(store.CredentialStoreError, match="unsupported or invalid"):
        store.get_record(home, "auth")


@pytest.mark.parametrize("key", [store.URI_ENV, store.KEY_ENV])
def test_bootstrap_keys_rejected_as_payload_environment_entries(mongo, key):
    with pytest.raises(store.CredentialStoreError, match="bootstrap keys"):
        store.put_record(mongo[0], "env", {key: "not-allowed"})


def test_profiles_do_not_share_records_or_bootstrap(mongo, tmp_path, monkeypatch):
    home, _ = mongo
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_bytes((home / ".env").read_bytes())
    store.put_record(home, "env", {"TOKEN": "profile-a"})
    store.put_record(other, "env", {"TOKEN": "profile-b"})
    assert store.get_record(home, "env") == {"TOKEN": "profile-a"}
    assert store.get_record(other, "env") == {"TOKEN": "profile-b"}
    (other / ".env").unlink()
    monkeypatch.setenv(store.URI_ENV, "mongodb://launch.invalid")
    assert not store.enabled(other)


@pytest.mark.parametrize("uri", ["mongodb://example.invalid/?tls=false", "mongodb://example.invalid/?tlsInsecure=true", "mongodb://example.invalid/?tlsAllowInvalidCertificates=true"])
def test_tls_cannot_be_disabled(mongo, uri):
    (mongo[0] / ".env").write_text(f"MONGODB_URI={uri}\n")
    with pytest.raises(store.CredentialStoreError, match="verified TLS"):
        store.get_record(mongo[0], "auth")


def test_missing_configuration_never_constructs_a_client(tmp_path, monkeypatch):
    monkeypatch.delenv(store.URI_ENV, raising=False)
    monkeypatch.setattr(store, "_collection", lambda settings: pytest.fail("network attempted"))
    assert not store.enabled(tmp_path)
    with pytest.raises(store.CredentialStoreError, match="not configured"):
        store.get_record(tmp_path, "env")


def test_driver_errors_are_redacted_and_never_fall_back(mongo, monkeypatch):
    home, collection = mongo
    def fail(query):
        raise RuntimeError("mongodb://user:password@example.invalid offline-test-secret")
    monkeypatch.setattr(collection, "find_one", fail)
    with pytest.raises(store.CredentialStoreError) as error:
        store.get_record(home, "auth")
    assert "password" not in str(error.value) and "offline-test-secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_concurrent_stale_write_is_rejected_without_losing_winner(mongo):
    home, _ = mongo
    store.put_record(home, "auth", {"token": "initial"})
    store.get_record(home, "auth")
    errors = []
    def other_writer():
        try:
            store.get_record(home, "auth")
            store.put_record(home, "auth", {"token": "winner"})
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=other_writer)
    thread.start()
    thread.join()
    assert not errors
    with pytest.raises(store.CredentialStoreError, match="concurrently"):
        store.put_record(home, "auth", {"token": "stale"})
    assert store.get_record(home, "auth") == {"token": "winner"}


@pytest.mark.parametrize("path", ["", "/", "/hermes", "/custom", "/Lucy", "/deep"])
def test_database_defaults_to_hermes_regardless_of_uri_default(path, monkeypatch):
    from pymongo import MongoClient
    client = MongoClient(f"mongodb://example.invalid{path}", connect=False)
    monkeypatch.setattr(store, "_CLIENTS", {})
    monkeypatch.setattr("pymongo.MongoClient", lambda *args, **kwargs: client)
    try:
        collection = store._collection(store._Settings(f"mongodb://example.invalid{path}"))
        assert collection.database.name == "hermes" and collection.name == "deep_credentials"
    finally:
        client.close()


def test_explicit_database_overrides_uri_default(tmp_path, monkeypatch):
    from pymongo import MongoClient
    home = tmp_path / "profile"
    home.mkdir()
    (home / ".env").write_text(
        "MONGODB_URI=mongodb://example.invalid/hermes\n"
        "MONGODB_DATABASE=isolated_migration_test\n"
    )
    client = MongoClient("mongodb://example.invalid/hermes", connect=False)
    monkeypatch.setattr(store, "_CLIENTS", {})
    monkeypatch.setattr("pymongo.MongoClient", lambda *args, **kwargs: client)
    try:
        collection = store._collection(store._settings(home))
        assert collection.database.name == "isolated_migration_test"
    finally:
        client.close()


def test_network_read_retries_then_succeeds(mongo, monkeypatch):
    from pymongo.errors import AutoReconnect
    home, collection = mongo
    original = collection.find_one
    calls = 0
    def flaky(query):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise AutoReconnect("mongodb://user:password/offline-test-secret")
        return original(query)
    monkeypatch.setattr(collection, "find_one", flaky)
    assert store.get_record(home, "auth") is None and calls == 3


def test_read_exhaustion_has_safe_typed_diagnostics(mongo, monkeypatch):
    from pymongo.errors import ServerSelectionTimeoutError
    home, collection = mongo
    store.get_record(home, "auth")
    calls = 0
    def fail(query):
        nonlocal calls
        calls += 1
        error = ServerSelectionTimeoutError("mongodb://user:password/offline-test-secret")
        error.code = 91
        raise error
    monkeypatch.setattr(collection, "find_one", fail)
    with pytest.raises(store.CredentialStoreError) as caught:
        store.get_record(home, "auth")
    error = caught.value
    assert calls == 3 and error.driver_class == "ServerSelectionTimeoutError"
    assert error.category == "network" and error.code == 91 and error.retry_count == 2
    assert all(secret not in str(error) for secret in ("password", "offline-test-secret", "mongodb://"))
    assert error.__suppress_context__


def test_authentication_read_failure_is_not_retried(mongo, monkeypatch):
    from pymongo.errors import OperationFailure
    home, collection = mongo
    store.get_record(home, "auth")
    calls = 0
    def fail(query):
        nonlocal calls
        calls += 1
        raise OperationFailure("offline-test-secret", code=18)
    monkeypatch.setattr(collection, "find_one", fail)
    with pytest.raises(store.CredentialStoreError) as caught:
        store.get_record(home, "auth")
    assert calls == 1 and caught.value.category == "authentication" and caught.value.retry_count == 0


def test_ambiguous_insert_is_reconciled_without_replay(mongo, monkeypatch):
    from pymongo.errors import AutoReconnect
    home, collection = mongo
    original = collection.insert_one
    calls = 0
    def ambiguous(document):
        nonlocal calls
        calls += 1
        original(document)
        raise AutoReconnect("offline-test-secret")
    monkeypatch.setattr(collection, "insert_one", ambiguous)
    store.put_record(home, "auth", {"token": "exact"})
    assert calls == 1 and store.get_record(home, "auth") == {"token": "exact"}


def test_ambiguous_cas_refuses_different_concurrent_payload(mongo, monkeypatch):
    from pymongo.errors import AutoReconnect
    home, collection = mongo
    store.put_record(home, "auth", {"token": "initial"})
    assert store.get_record(home, "auth") == {"token": "initial"}
    original = collection.replace_one
    def competing(query, document):
        winner = copy.deepcopy(document)
        winner["revision"] += 1
        winner["payload"] = {"token": "concurrent-winner"}
        collection.documents[query["_id"]] = winner
        raise AutoReconnect("offline-test-secret")
    monkeypatch.setattr(collection, "replace_one", competing)
    with pytest.raises(store.CredentialStoreError):
        store.put_record(home, "auth", {"token": "proposed"})
    monkeypatch.setattr(collection, "replace_one", original)
    assert store.get_record(home, "auth") == {"token": "concurrent-winner"}


@pytest.mark.parametrize("key", ["TERMINAL_TIMEOUT", "TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL", "API_SERVER_PORT"])
def test_environment_records_reject_noncredential_application_settings(mongo, key):
    with pytest.raises(store.CredentialStoreError, match="Only credential"):
        store.put_record(mongo[0], "env", {key: "local-setting"})
    assert not mongo[1].documents
