"""Opt-in plaintext credential persistence; bootstrap secrets never enter MongoDB.

Records use the explicit plaintext schema version 2. MongoDB access requires verified TLS.
No key generation or connection takes place on import, or without an explicit URI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from agent.mongo_environment import BOOTSTRAP_KEYS as _ALL_BOOTSTRAP_KEYS
from agent.mongo_environment import canonical_values

URI_ENV = "MONGODB_URI"
KEY_ENV = "CREDENTIAL_KEY"  # Legacy bootstrap key name; deliberately unused.
DATABASE_ENV = "MONGODB_DATABASE"
BOOTSTRAP_KEYS = _ALL_BOOTSTRAP_KEYS
_COLLECTION = "deep_credentials"
_SCHEMA_VERSION = 2
_CLIENTS: dict[tuple[str, str], Any] = {}
_CLIENT_LOCK = threading.Lock()
_REVISIONS = threading.local()


class CredentialStoreError(RuntimeError):
    """Safe to display: never includes driver errors, payloads, URIs or keys."""
    def __init__(self, message: str, *, driver_class: str | None = None,
                 category: str | None = None, code: int | None = None,
                 retry_count: int = 0):
        super().__init__(message)
        self.driver_class = driver_class
        self.category = category
        self.code = code
        self.retry_count = retry_count


def _driver_diagnostics(exc: Exception, retries: int) -> dict:
    """Extract only stable, non-sensitive driver metadata."""
    name = type(exc).__name__
    try:
        from pymongo import errors as mongo_errors
        categories = (
            ("network", (mongo_errors.NetworkTimeout, mongo_errors.AutoReconnect,
                         mongo_errors.ServerSelectionTimeoutError, mongo_errors.ConnectionFailure)),
            ("authentication", ()),
            ("validation", (mongo_errors.WriteError, mongo_errors.DocumentTooLarge)),
        )
        for category, classes in categories:
            if isinstance(exc, classes):
                break
        else:
            category = ("authentication" if getattr(exc, "code", None) in {13, 18}
                        else "driver" if type(exc).__module__.startswith("pymongo") else "unknown")
    except (ImportError, AttributeError):
        category = "unknown"
    raw_code = getattr(exc, "code", None)
    code = raw_code if type(raw_code) is int and -(2**31) <= raw_code < 2**31 else None
    return {"driver_class": name[:80], "category": category, "code": code, "retry_count": retries}


def _is_transient(exc: Exception) -> bool:
    try:
        from pymongo import errors as mongo_errors
        return isinstance(exc, (mongo_errors.NetworkTimeout, mongo_errors.AutoReconnect,
                                mongo_errors.ServerSelectionTimeoutError, mongo_errors.ConnectionFailure))
    except (ImportError, AttributeError):
        return False


def _mongo_read(operation, callback):
    """Run a read callback with at most three attempts for transient driver failures."""
    for attempt in range(3):
        try:
            return callback()
        except CredentialStoreError:
            raise
        except Exception as exc:
            if not _is_transient(exc) or attempt == 2:
                details = _driver_diagnostics(exc, attempt)
                raise CredentialStoreError(f"Cannot {operation} plaintext credentials from MongoDB", **details) from None
            time.sleep(0.05 * (2 ** attempt))


@dataclass(frozen=True)
class _Settings:
    uri: str = field(repr=False)
    database: str = field(default="hermes", repr=False)


def credential_file_record_name(path: Path) -> str | None:
    """Names for the owned legacy files consumed by migration and OAuth adapters."""
    path = Path(path)
    if path.name == ".anthropic_oauth.json":
        return "anthropic_oauth"
    if path.parent.name == "mcp-tokens":
        for suffix, kind in ((".client.json", "client"), (".meta.json", "meta"),
                             (".json", "tokens"), (".cimd-off", "cimd")):
            if path.name.endswith(suffix):
                server = path.name[:-len(suffix)]
                return f"mcp_oauth:{server}:{kind}" if server else None
    return None


def _bootstrap(home: Path) -> dict[str, str]:
    # Do NOT use load_env_file: it calls this backend to hydrate stored secrets.
    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    from hermes_constants import get_process_hermes_home

    home = Path(home).expanduser().resolve()
    try:
        raw = (home / ".env").read_bytes()
        local = (_parse_env_text(_decode_env_bytes(raw))
                 if any(name.encode() in raw for name in BOOTSTRAP_KEYS) else {})
    except FileNotFoundError:
        local = {}
    except OSError:
        raise CredentialStoreError("Cannot read credential-store bootstrap configuration") from None
    values = canonical_values(local)
    if home == get_process_hermes_home().expanduser().resolve():
        values = canonical_values({**os.environ, **values})
    return values


def enabled(home: Path) -> bool:
    # A URI is now only a backup destination, never credential authority.
    from agent.storage_mode import mongo_primary_enabled
    return mongo_primary_enabled(home) and bool(_bootstrap(home).get(URI_ENV, "").strip())


def _settings(home: Path) -> _Settings:
    values = _bootstrap(home)
    uri = values.get(URI_ENV, "").strip()
    database = values.get(DATABASE_ENV, "hermes").strip()
    if not uri:
        raise CredentialStoreError("MongoDB credential storage is not configured")
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,63}", database):
            raise ValueError
        parsed = urlsplit(uri)
        if parsed.scheme not in {"mongodb", "mongodb+srv"} or not parsed.netloc:
            raise ValueError
        options = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
        for option in ("tls", "ssl"):
            if any(v.lower() != "true" for v in options.get(option, [])):
                raise ValueError
        for option in ("tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames"):
            if any(v.lower() != "false" for v in options.get(option, [])):
                raise ValueError
    except ValueError:
        raise CredentialStoreError("MongoDB credential storage requires a valid URI and verified TLS") from None
    return _Settings(uri=uri, database=database)


def _collection(settings: _Settings):
    try:
        from pymongo import MongoClient
        with _CLIENT_LOCK:
            slot = (str(os.getpid()), settings.uri)
            client = _CLIENTS.get(slot)
            if client is None:
                client = MongoClient(settings.uri, connect=False, tls=True,
                                     appname="Deep plaintext credentials",
                                     serverSelectionTimeoutMS=3000, connectTimeoutMS=3000,
                                     socketTimeoutMS=20000, retryWrites=True, w="majority")
                _CLIENTS[slot] = client
        # The setting overrides URI path defaults so migration can target a
        # unique, non-production database without changing credentials.
        return client[settings.database][_COLLECTION]
    except ImportError:
        raise CredentialStoreError("Install the mongodb extra to use MongoDB credentials") from None
    except Exception:
        raise CredentialStoreError("Cannot initialize MongoDB credential storage") from None


_BASE_NAMES = frozenset({"env", "auth", "vault", "anthropic_oauth"})
_MCP_NAME = re.compile(r"mcp_oauth:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}:(?:client|meta|tokens|cimd)\Z")


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or (name not in _BASE_NAMES and not _MCP_NAME.fullmatch(name)):
        raise CredentialStoreError("Invalid credential record name")


def _record_id(home: Path, name: str) -> str:
    _validate_name(name)
    identity = json.dumps([str(Path(home).expanduser().resolve()), name], separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _versions() -> dict:
    if not hasattr(_REVISIONS, "values"):
        _REVISIONS.values = {}
    return _REVISIONS.values


def _slot(settings: _Settings, record_id: str) -> tuple:
    return (hashlib.sha256((settings.uri + "\0" + settings.database).encode()).digest(), record_id)


def _payload(document: dict, record_id: str, name: str) -> dict:
    # Encrypted v1 records intentionally fail closed; never attempt legacy decryption.
    try:
        if (document["_id"] != record_id or document["schema_version"] != _SCHEMA_VERSION
                or document["storage"] != "plaintext" or document["name"] != name):
            raise ValueError
        revision = document["revision"]
        payload = document["payload"]
        if type(revision) is not int or revision < 1 or not isinstance(payload, dict):
            raise ValueError
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if name == "env":
            from hermes_cli.mongo_credentials import is_credential_env
            if any(not is_credential_env(key) for key in payload):
                raise ValueError
        return payload
    except Exception:
        raise CredentialStoreError("Credential record has an unsupported or invalid storage schema") from None


def get_record(home: Path, name: str) -> dict | None:
    settings = _settings(home)
    record_id = _record_id(home, name)
    document = _mongo_read("read", lambda: _collection(settings).find_one({"_id": record_id}))
    if document is None:
        _versions()[_slot(settings, record_id)] = 0
        return None
    payload = _payload(document, record_id, name)
    _versions()[_slot(settings, record_id)] = document["revision"]
    return payload


def put_record(home: Path, name: str, payload: dict) -> None:
    """Compare-and-swap the last read revision; conflicts never silently lose credentials."""
    settings = _settings(home)
    record_id = _record_id(home, name)
    slot = _slot(settings, record_id)
    if slot not in _versions():
        get_record(home, name)
    previous = _versions()[slot]
    try:
        if not isinstance(payload, dict):
            raise ValueError
        plaintext = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        normalized = json.loads(plaintext)
        if not isinstance(normalized, dict) or any(key in BOOTSTRAP_KEYS for key in normalized):
            raise ValueError
    except (TypeError, ValueError):
        raise CredentialStoreError("Credential payload must be a JSON mapping without bootstrap keys") from None
    if name == "env":
        from hermes_cli.mongo_credentials import is_credential_env
        if any(not is_credential_env(key) for key in normalized):
            raise CredentialStoreError("Only credential variables may be stored in the MongoDB environment record")
    collection = _collection(settings)
    current_document = _mongo_read("read", lambda: collection.find_one({"_id": record_id}))
    if current_document is not None:
        if _payload(current_document, record_id, name) == normalized:
            return
    revision = previous + 1
    document = {"_id": record_id, "schema_version": _SCHEMA_VERSION,
                "storage": "plaintext", "name": name, "payload": normalized,
                "revision": revision}
    try:
        if previous == 0:
            collection.insert_one(document)
        else:
            result = collection.replace_one({"_id": record_id, "revision": previous}, document)
            if result.matched_count != 1:
                raise CredentialStoreError("Credentials changed concurrently; reload and retry")
    except CredentialStoreError:
        raise
    except Exception as exc:
        details = _driver_diagnostics(exc, 0)
        if _is_transient(exc):
            try:
                stored = _mongo_read("read", lambda: collection.find_one({"_id": record_id}))
                if stored == document and _payload(stored, record_id, name) == normalized:
                    _versions()[slot] = revision
                    return
            except CredentialStoreError:
                pass
        raise CredentialStoreError("Cannot persist plaintext credentials; reload before retrying", **details) from None
    _versions()[slot] = revision
