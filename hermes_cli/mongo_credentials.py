"""Copy owned login/credential records to plaintext MongoDB, retaining local sources.

Only credentials are replicated. Application settings, sessions and SQL remain local.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from agent import mongo_credentials as store

_REFERENCE = re.compile(r"^(?:(?:Bearer|Basic|Token)\s+)?\$\{[^{}]+\}$", re.IGNORECASE)
_SECRET_NAME = re.compile(r"(?:^|[_-])(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|session)(?:$|[_-])", re.IGNORECASE)


def is_credential_env(name: str) -> bool:
    """Classify credential variables, not application settings or bootstrap secrets."""
    if name in store.BOOTSTRAP_KEYS:
        return False
    if name in {"TELEGRAM_API_ID", "TELEGRAM_API_HASH"}:
        return True
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS
    entry = OPTIONAL_ENV_VARS.get(name)
    if entry is not None:
        return bool(entry.get("password", False))
    return bool(re.search(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_KEY|API_HASH)(?:$|_)", name, re.IGNORECASE))


def _sensitive(path: tuple[str, ...], value: str) -> bool:
    from hermes_cli.config import _is_secret_config_key
    from agent.credential_persistence import _is_secret_payload_key

    name = path[-1] if path else ""
    if _is_secret_config_key(".".join(path)) or _is_secret_payload_key(name) or _SECRET_NAME.search(name):
        return True
    if path == ("model", "api"):
        return True
    if "headers" in path:
        return True
    try:
        parsed = urlsplit(value)
        return bool(parsed.password or any(_SECRET_NAME.search(key) for key, _ in parse_qsl(parsed.query)))
    except ValueError:
        return False


def split_config_secrets(data: dict) -> tuple[dict, dict[str, str]]:
    """Replace nonempty inline credential scalars with ordinary ${ENV} references."""
    values: dict[str, str] = {}
    result = copy.deepcopy(data)

    def walk(node, path=()):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                current = (*path, str(key))
                if isinstance(value, str) and value and not _REFERENCE.fullmatch(value) and _sensitive(current, value):
                    slot = "CONFIG_" + hashlib.sha256(json.dumps(current).encode()).hexdigest()[:24].upper() + "_SECRET"
                    values[slot] = value
                    node[key] = "${" + slot + "}"
                else:
                    walk(value, current)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, (*path, str(index)))
    walk(result)
    return result, values


def protect_config_write(home: Path, data):
    """Shared YAML writer seam: never write a new inline credential in Mongo mode."""
    if not isinstance(data, dict) or not store.enabled(home):
        return data
    protected, additions = split_config_secrets(data)
    if additions:
        values = store.get_record(home, "env") or {}
        values.update(additions)
        store.put_record(home, "env", values)
    return protected


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise store.CredentialStoreError("Cannot migrate an unreadable local credential record") from None


def _owned_legacy_files(home: Path) -> dict[Path, str]:
    files = {home / ".anthropic_oauth.json": "anthropic_oauth"}
    token_dir = home / "mcp-tokens"
    if token_dir.is_symlink():
        raise store.CredentialStoreError("Refusing to migrate a linked credential directory")
    if token_dir.exists():
        for path in token_dir.iterdir():
            name = store.credential_file_record_name(path)
            if name:
                if path.is_symlink():
                    raise store.CredentialStoreError("Refusing to migrate a linked credential file")
                if path.is_file():
                    files[path] = name
    return files


def sync_credentials(home: Path, *, preserve_local: bool = True,
                    _migration_authorized: bool = False) -> dict:
    """Copy owned credentials and verify all records; never modify local sources.

    ``preserve_local=False`` is accepted for compatibility but cannot enable deletion.
    """
    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    from utils import fast_safe_load

    home = Path(home).expanduser().resolve()
    if not store.enabled(home) and not _migration_authorized:
        raise store.CredentialStoreError("MongoDB is not configured; supply the URI first")
    # Validate the URI before parsing local sources; no encryption key is used.
    store._settings(home)
    env_path = home / ".env"
    try:
        legacy_files = _owned_legacy_files(home)
        sources = (env_path, home / "config.yaml", home / "auth.json",
                   home / "vault" / "vault.json.enc", home / "vault" / "vault.key", *legacy_files)
        source_bytes = {path: path.read_bytes() for path in sources if path.exists()}
        raw_env = _parse_env_text(_decode_env_bytes(env_path.read_bytes())) if env_path.exists() else {}
        config_path = home / "config.yaml"
        config = fast_safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError
    except Exception:
        raise store.CredentialStoreError("Cannot migrate unreadable credential configuration") from None
    _, config_secrets = split_config_secrets(config)
    local_values = {k: v for k, v in raw_env.items() if is_credential_env(k)}
    local_values.update(config_secrets)
    remote_values = store.get_record(home, "env") or {}
    for name, value in local_values.items():
        if name in remote_values and remote_values[name] != value:
            raise store.CredentialStoreError("Local and database credentials differ; reconcile them before syncing")
    records = {"env": {**remote_values, **local_values}}
    auth_path = home / "auth.json"
    local_auth = _read_json(auth_path)
    if local_auth is not None:
        records["auth"] = local_auth
    for path, name in legacy_files.items():
        if not path.exists():
            continue
        if path.suffix == ".cimd-off":
            records[name] = {"rejected": True}
        else:
            payload = _read_json(path)
            if name.endswith(":tokens") and payload and payload.get("expires_in") is not None and "expires_at" not in payload:
                try:
                    payload["expires_at"] = path.stat().st_mtime + int(payload["expires_in"])
                except (OSError, TypeError, ValueError):
                    raise store.CredentialStoreError("Cannot migrate token expiry metadata") from None
            records[name] = payload
    vault_path = home / "vault" / "vault.json.enc"
    if vault_path.exists():
        from cryptography.fernet import Fernet
        try:
            vault = json.loads(Fernet((home / "vault" / "vault.key").read_bytes().strip()).decrypt(vault_path.read_bytes()))
            if not isinstance(vault, dict) or not isinstance(vault.get("items"), list):
                raise ValueError
            records["vault"] = vault
        except Exception:
            raise store.CredentialStoreError("Cannot decrypt the local vault for migration") from None
    # All conflicts are discovered BEFORE any write. Keep local files intact on upload failure.
    for name, value in records.items():
        previous = store.get_record(home, name)
        if name != "env" and previous is not None and previous != value:
            raise store.CredentialStoreError("Local and database credentials differ; reconcile them before syncing")
    for name, value in records.items():
        store.put_record(home, name, value)
        if store.get_record(home, name) != value:
            raise store.CredentialStoreError("Plaintext credential verification failed; local credentials were kept")
    try:
        unchanged = all(path.exists() == (path in source_bytes) and
                        (path not in source_bytes or path.read_bytes() == source_bytes[path])
                        for path in sources)
        unchanged = unchanged and _owned_legacy_files(home) == legacy_files
    except OSError:
        unchanged = False
    if not unchanged:
        raise store.CredentialStoreError("Local credentials changed during migration; source files were kept")
    counts = {"credential_records": len(records), "config_credentials": len(config_secrets)}
    return counts


def main() -> int:
    import argparse
    import sys
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description="Copy profile credentials to plaintext MongoDB without restarting")
    parser.add_argument("--home", type=Path, default=None, help="Explicit owning profile home")
    args = parser.parse_args()
    try:
        counts = sync_credentials(args.home or get_hermes_home())
    except store.CredentialStoreError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Verified {counts['credential_records']} plaintext credential records; copied {counts['config_credentials']} inline credentials.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
