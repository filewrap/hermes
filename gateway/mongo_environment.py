"""Mongo-authoritative non-secret settings alongside privileged credential records."""
from pathlib import Path
from agent.mongo_environment import BOOTSTRAP_KEYS

# These are connection/identity/deployment inputs, not user profile preferences.
BOOTSTRAP_SETTINGS = BOOTSTRAP_KEYS | frozenset({"HERMES_HOME", "HERMES_PROFILE"})


def environment_inventory(home: Path) -> dict[str, str]:
    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    from hermes_cli.mongo_credentials import is_credential_env
    path = Path(home) / ".env"
    values = _parse_env_text(_decode_env_bytes(path.read_bytes())) if path.exists() else {}
    return {key: value for key, value in values.items()
            if key not in BOOTSTRAP_SETTINGS and not is_credential_env(key)}


def authoritative_environment(home: Path, local: dict) -> dict:
    from gateway.mongo_multiuser import store_for_home, owner_user_id, principal
    from agent.mongo_tenants import TenantStoreError
    store = store_for_home(home)
    remote = store.get(principal(owner_user_id(home)), "settings", "environment")
    if not isinstance(remote, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in remote.items()):
        raise TenantStoreError("Durable owner environment settings are missing or invalid")
    from hermes_cli.mongo_credentials import is_credential_env
    if any(key in BOOTSTRAP_SETTINGS or is_credential_env(key) for key in remote):
        raise TenantStoreError("Invalid durable environment boundary")
    return {**remote, **{key: value for key, value in local.items() if key in BOOTSTRAP_SETTINGS}}


def save_environment_setting(home: Path, key: str, value: str) -> bool:
    """Persist before any local cache write; returns false for credentials/bootstrap."""
    from agent.storage_mode import mongo_primary_enabled
    if not mongo_primary_enabled(home):
        return False
    from gateway.mongo_multiuser import enabled, store_for_home, owner_user_id, principal
    from hermes_cli.mongo_credentials import is_credential_env
    if key in BOOTSTRAP_SETTINGS or is_credential_env(key) or not enabled(home):
        return False
    store = store_for_home(home)
    actor = principal(owner_user_id(home))
    current = authoritative_environment(home, {})
    current[key] = str(value)
    doc = next((d for d in store.enumerate_tenant_data(actor, "owner")
                if d["kind"] == "settings" and d["key"] == "environment"), None)
    store.put(actor, "settings", "environment", current, expected_revision=doc["revision"] if doc else 0)
    if store.get(actor, "settings", "environment") != current:
        from agent.mongo_tenants import TenantStoreError
        raise TenantStoreError("Durable environment settings read-back failed")
    return True
