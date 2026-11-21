"""Compatibility-named facade for profile-scoped Telegram multiuser state.

Local SQLite remains the default. A verified migration marker selects Mongo as
primary; a URI or the retired MULTIUSER_ENABLED flag alone never does.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
import re
import threading
from typing import Mapping
from agent.mongo_environment import canonical_values

logger = logging.getLogger(__name__)


# Internal child-process handoff, installed only by the HF supervisor. Plain
# process owner/allowlist variables must never become a global profile fallback.
_HF_RUNTIME_POLICY = "_HERMES_HF_MULTIUSER_RUNTIME_POLICY"
_OWNER = "OWNER_TELEGRAM_ID"
_LEGACY_OWNER = "DEEP_OWNER_TELEGRAM_ID"
_ALLOWED = "TELEGRAM_ALLOWED_USERS"
_POLICY_CACHE_LIMIT = 128
_POLICY_CACHE: OrderedDict[str, tuple[bytes, dict[str, str]]] = OrderedDict()
_POLICY_CACHE_LOCK = threading.Lock()


def _numeric_id(value: str, key: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError(f"HF startup blocked: invalid {key}; canonical positive numeric Telegram IDs required")
    return value


def _numeric_allowlist(value: str) -> set[str]:
    return {_numeric_id(part, _ALLOWED) for part in re.split(r"[,\s]+", value.strip()) if part}


def hf_runtime_environment(home: Path, environment: Mapping[str, str]) -> dict[str, str]:
    """Prepare an HF child's env without writing policy to disk or mutating the parent."""
    result = dict(environment)
    result.pop(_HF_RUNTIME_POLICY, None)
    owner = environment.get(_OWNER, environment.get(_LEGACY_OWNER))
    if owner is None:
        return result
    policy = {_OWNER: _numeric_id(owner, _OWNER)}
    if _ALLOWED in environment:
        policy[_ALLOWED] = ",".join(sorted(_numeric_allowlist(environment[_ALLOWED])))
    result[_HF_RUNTIME_POLICY] = json.dumps({
        "home": os.path.abspath(home), "policy": policy,
    })
    # Validate conflicts before launch, and again at every read in the child.
    _profile_env(home, _runtime_environment=result)
    return result


def _profile_env(home: Path, *, _runtime_environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read local policy, plus an HF-only runtime overlay for exactly its launch home."""
    from agent.secret_scope import _decode_env_bytes, _parse_env_text
    absolute_home = os.path.abspath(home)
    path = Path(absolute_home) / ".env"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        values = {}
    except OSError:
        raise RuntimeError("Cannot read multiuser bootstrap policy") from None
    else:
        digest = hashlib.sha256(raw).digest()
        with _POLICY_CACHE_LOCK:
            cached = _POLICY_CACHE.get(absolute_home)
            if cached is not None and cached[0] == digest:
                _POLICY_CACHE.move_to_end(absolute_home)
                values = dict(cached[1])
            else:
                values = None
        if values is None:
            # Parse only on a new byte digest. Retain the two policy fields only;
            # credentials and every other .env value die with this local parse.
            parsed = _parse_env_text(_decode_env_bytes(raw))
            canonical = canonical_values(parsed)
            policy = {key: canonical[key] for key in (_OWNER,) if key in canonical}
            if _ALLOWED in parsed:
                policy[_ALLOWED] = parsed[_ALLOWED]
            with _POLICY_CACHE_LOCK:
                _POLICY_CACHE[absolute_home] = (digest, policy)
                _POLICY_CACHE.move_to_end(absolute_home)
                while len(_POLICY_CACHE) > _POLICY_CACHE_LIMIT:
                    _POLICY_CACHE.popitem(last=False)
            values = dict(policy)
    environment = os.environ if _runtime_environment is None else _runtime_environment
    payload = environment.get(_HF_RUNTIME_POLICY)
    if payload is None:
        return values
    try:
        runtime = json.loads(payload)
        if not isinstance(runtime, dict) or set(runtime) != {"home", "policy"} or not isinstance(runtime["home"], str):
            raise ValueError
        launch_home = runtime["home"]
        if not os.path.isabs(launch_home) or os.path.abspath(launch_home) != launch_home:
            raise ValueError
        policy = runtime["policy"]
        if not isinstance(policy, dict) or _OWNER not in policy or set(policy) - {_OWNER, _ALLOWED}:
            raise ValueError
        if not all(isinstance(value, str) for value in policy.values()):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("HF startup blocked: invalid runtime multiuser policy") from None
    if os.path.abspath(home) != launch_home:
        return values
    incoming = {_OWNER: _numeric_id(policy[_OWNER], _OWNER)}
    if _ALLOWED in policy:
        incoming[_ALLOWED] = ",".join(sorted(_numeric_allowlist(policy[_ALLOWED])))
    for key, value in incoming.items():
        if key in values:
            matches = (values[key].strip() == value) if key == _OWNER else (_numeric_allowlist(values[key]) == _numeric_allowlist(value))
            if not matches:
                raise ValueError(f"HF startup blocked: {key} conflict between Space environment and profile .env")
        else:
            values[key] = value
    return values


def enabled(home: str | Path) -> bool:
    # A configured owner enables isolation without consulting the backup server.
    # There is intentionally no Mongo flag or availability fallback here.
    return bool(_profile_env(Path(home)).get("OWNER_TELEGRAM_ID", "").strip())


def _safe_store_reason(exc: BaseException) -> str:
    """Short failure kind for a log line. TenantStoreError and
    CredentialStoreError messages are curated safe (no URI, no driver
    payload); anything else is reduced to its type name so a driver or
    config error can never leak credentials into the logs."""
    from agent.mongo_credentials import CredentialStoreError
    from agent.mongo_tenants import TenantStoreError

    if isinstance(exc, (TenantStoreError, CredentialStoreError)):
        return str(exc) or type(exc).__name__
    return type(exc).__name__


def runtime_ready(home: str | Path | None = None) -> bool:
    """Check the currently selected local or Mongo store without fallback."""
    ready, _reason = runtime_readiness(home)
    return ready


def runtime_readiness(home: str | Path | None = None) -> tuple[bool, str]:
    """``(ready, reason)`` — the diagnosable form of :func:`runtime_ready`.

    ``reason`` is ``""`` when ready, otherwise a short safe explanation of
    which stage failed: a missing/invalid owner, an unverifiable Mongo
    primary activation marker, or an unavailable Mongo tenant store. The
    old bare ``except Exception: return False`` turned all of these into an
    indistinguishable "multiuser simply isn't ready", invisible in every
    log; callers that block on readiness must log the reason.
    """
    try:
        if home is None:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        try:
            owner_user_id(home)
        except Exception:
            return False, "OWNER_TELEGRAM_ID missing or invalid in the profile .env"
        from agent.storage_mode import mongo_primary_enabled
        try:
            mongo_primary = mongo_primary_enabled(home)
        except Exception as exc:
            return False, f"Mongo primary activation marker cannot be verified ({type(exc).__name__})"
        if mongo_primary:
            try:
                store_for_home(home).ensure_owner()
            except Exception as exc:
                return False, f"Mongo tenant store unavailable ({_safe_store_reason(exc)})"
            return True, ""
        from agent.local_tenants import LocalTenantStore
        return True, ""
    except Exception as exc:
        return False, type(exc).__name__


def owner_user_id(home: str | Path) -> str:
    return _owner_user_id_from_values(_profile_env(Path(home)))


def _owner_user_id_from_values(values: Mapping[str, str]) -> str:
    value = values.get(_OWNER, "").strip()
    if not value.isdecimal():
        raise RuntimeError("Telegram multiuser requires OWNER_TELEGRAM_ID in the profile .env or HF runtime policy")
    return value


def store_for_home(home: str | Path):
    home = Path(home)
    values = _profile_env(home)
    owner = _owner_user_id_from_values(values)
    from agent.storage_mode import mongo_primary_enabled
    try:
        mongo_primary = mongo_primary_enabled(home)
    except Exception as exc:
        # The funnel for every tenant consumer (Telegram controls, profile
        # store, memory tool): a marker failure must leave a breadcrumb
        # instead of surfacing only as a silent False or a bare raise.
        logger.warning("Mongo tenant store unavailable (activation marker: %s)", type(exc).__name__)
        raise
    if mongo_primary:
        from agent.mongo_tenants import MongoTenantStore
        try:
            store = MongoTenantStore(home, owner)
            store.ensure_owner()
        except Exception as exc:
            logger.warning("Mongo tenant store unavailable (%s)", _safe_store_reason(exc))
            raise
        return store
    from agent.local_tenants import LocalTenantStore
    # Seed only explicit numeric identities from this profile's policy.
    # The SQLite backend performs this once atomically; future bans stay banned.
    raw = values.get("TELEGRAM_ALLOWED_USERS", "")
    allowed = [part for part in re.split(r"[,\s]+", raw.strip()) if part.isdecimal()]
    return LocalTenantStore(home, owner, allowed_user_ids=allowed)


def principal(user_id: str | int):
    from agent.mongo_tenants import Principal
    return Principal(user_id)
