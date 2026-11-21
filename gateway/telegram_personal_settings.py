"""Profile-local, Telegram-user-scoped personal preferences.

This store deliberately lives outside config.yaml: a Telegram user's preferences
must not silently change the shared profile settings used by other people.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from utils import atomic_json_write

logger = logging.getLogger(__name__)

DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES = 200 * 1024 * 1024
ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES = tuple(value * 1024 * 1024 for value in (50, 100, 200, 500))
DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES = tuple(
    value * 1024 * 1024 for value in (200, 512, 2048, 4096)
)

_DEFAULTS: dict[str, Any] = {
    "general_instructions": "",
    "search_instructions": "",
    "memory_enabled": True,
    "self_learning_enabled": True,
    "reactions_enabled": True,
    "text_reactions_enabled": False,
    "activity_trail_enabled": False,
    "attachment_download_mode": "auto",
    "attachment_auto_download_threshold_bytes": DEFAULT_ATTACHMENT_DOWNLOAD_THRESHOLD_BYTES,
    "attachment_explicit_download_limit_bytes": DEFAULT_ATTACHMENT_EXPLICIT_DOWNLOAD_LIMIT_BYTES,
    "default_model": "",
    "default_provider": "",
    "mcp_mode": "inherit",
    "selected_mcp_servers": [],
    "personal_mcp_servers": {},
    "selected_skills": [],
    "personal_skills": [],
    "updated_at": "",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_MCP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_CREDENTIAL_NAME_RE = re.compile(r"(?i)(?:authorization|credential|token|secret|api[-_]?key|password|passwd|access[-_]?key|client[-_]?secret|signature|jwt)")
_CREDENTIAL_ARG_RE = re.compile(r"(?i)^--?(?:authorization|auth|token|secret|api[-_]?key|password|passwd|access[-_]?key|client[-_]?secret)(?:=|$)")
_CREDENTIAL_HEADER_RE = re.compile(
    r"(?i)(?:authorization|proxy[-_]?authorization|cookie|set[-_]?cookie|"
    r"[A-Za-z0-9_-]*(?:api[-_]?key|access[-_]?key|client[-_]?secret|token|secret|password|signature|jwt))\s*[:=]\s*(.+)$"
)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def personal_settings_path(profile_home: str | Path, user_id: str | int) -> Path:
    """Return the settings file path for one numeric Telegram user in one profile."""
    uid = str(user_id or "").strip()
    if not uid.isdigit() or not 1 <= len(uid) <= 20 or int(uid) <= 0:
        raise ValueError("A valid Telegram user ID is required")
    home = Path(profile_home).expanduser().resolve()
    requested = home / "personal" / "telegram" / "users" / uid
    if any(path.is_symlink() for path in (requested, *requested.parents) if path.is_relative_to(home)):
        raise ValueError("Personal settings path cannot use symlinks")
    root = requested.resolve()
    if not root.is_relative_to(home):
        raise ValueError("Personal settings path escaped the active profile")
    target = root / "settings.json"
    if target.is_symlink():
        raise ValueError("Personal settings path cannot use symlinks")
    return target


def _normalize_names(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    output: list[str] = []
    for raw in value:
        name = str(raw or "").strip()
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid {field} entry")
        if name not in output:
            output.append(name)
        if len(output) > 100:
            raise ValueError(f"{field} may contain at most 100 entries")
    return output


def _is_credential_reference(value: Any) -> bool:
    raw = str(value or "").strip()
    return bool(
        _ENV_REF_RE.fullmatch(raw)
        or re.fullmatch(rf"(?i)(?:Bearer|Basic|Token)\s+{_ENV_REF_RE.pattern}", raw)
    )


def validate_personal_mcp_config(name: str, config: dict[str, Any]) -> None:
    """Reject chat-supplied secrets while allowing profile environment references."""
    if not isinstance(config, dict):
        raise ValueError(f"MCP `{name}` configuration must be an object")
    env = config.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"MCP `{name}` env must be an object")
    for key, value in env.items():
        if not isinstance(value, str):
            raise ValueError(f"MCP `{name}` env values must be text")
        if _CREDENTIAL_NAME_RE.search(str(key)) and value and not _ENV_REF_RE.fullmatch(value.strip()):
            raise ValueError(f"MCP `{name}` credential env values must use `${'{'}ENV_NAME{'}'}` references, not secrets")

    headers = config.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError(f"MCP `{name}` headers must be an object")
    for key, value in headers.items():
        if _CREDENTIAL_NAME_RE.search(str(key)):
            if not _is_credential_reference(value):
                raise ValueError(f"MCP `{name}` credential headers must use `${'{'}ENV_NAME{'}'}` references")

    endpoint = str(config.get("url") or "")
    if endpoint:
        parsed = urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"MCP `{name}` endpoint cannot contain embedded username/password credentials; use headers and profile environment references")
        if any(_CREDENTIAL_NAME_RE.search(key) for key, _value in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError(f"MCP `{name}` endpoint query cannot contain credential parameters; use headers and profile environment references")

    args = config.get("args") or []
    if isinstance(args, str):
        args = args.split()
    if not isinstance(args, (list, tuple)):
        raise ValueError(f"MCP `{name}` args must be a list of strings")
    for index, raw_arg in enumerate(args):
        arg = str(raw_arg).strip()
        header_match = _CREDENTIAL_HEADER_RE.search(arg)
        if header_match:
            header_value = header_match.group(1).strip().strip("\"'")
            if not _is_credential_reference(header_value):
                raise ValueError(f"MCP `{name}` must not put plaintext credentials in header arguments; use a profile environment reference")
            continue
        if not _CREDENTIAL_ARG_RE.match(arg):
            continue
        if "=" in arg:
            value = arg.split("=", 1)[1].strip()
        else:
            value = str(args[index + 1]).strip() if index + 1 < len(args) else ""
        if value and not _is_credential_reference(value):
            raise ValueError(f"MCP `{name}` must not put plaintext credentials in command arguments; use an environment variable")

    def check_nested(value: Any, path: str = "config") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                if key_text.lower() in {"env", "headers", "url", "args"}:
                    continue
                if _CREDENTIAL_NAME_RE.search(key_text):
                    if not isinstance(item, str) or not _ENV_REF_RE.fullmatch(item.strip()):
                        raise ValueError(f"MCP `{name}` credential field `{path}.{key_text}` must use a profile environment reference")
                check_nested(item, f"{path}.{key_text}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_nested(item, f"{path}[{index}]")

    check_nested(config)
    from hermes_cli.mcp_security import validate_mcp_server_entry
    issues = validate_mcp_server_entry(name, config)
    if issues:
        raise ValueError("MCP security validation rejected this server: " + "; ".join(str(item) for item in issues[:5]))


def _validated_changes(changes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise ValueError("Preference changes must be a mapping")
    allowed = set(_DEFAULTS) - {"updated_at"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported personal preference: {sorted(unknown)[0]}")
    # Generic preference writes may select safe owner-configured skills, but
    # cannot create provider or MCP configuration. MCP changes use the dedicated
    # profile/user-private writer below.
    forbidden = {
        key for key in ("default_provider", "personal_mcp_servers", "mcp_mode")
        if key in changes and changes[key] != _DEFAULTS[key]
    }
    if forbidden:
        raise ValueError(f"Per-user configuration cannot change {sorted(forbidden)[0]}")
    result: dict[str, Any] = {}
    for key, value in changes.items():
        if key in {"general_instructions", "search_instructions"}:
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            result[key] = value.strip()
        elif key in {"memory_enabled", "self_learning_enabled", "reactions_enabled", "text_reactions_enabled",
                     "activity_trail_enabled"}:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
            result[key] = value
        elif key == "attachment_download_mode":
            if value not in {"auto", "explicit"}:
                raise ValueError("attachment_download_mode must be 'auto' or 'explicit'")
            result[key] = value
        elif key == "attachment_auto_download_threshold_bytes":
            if isinstance(value, bool) or not isinstance(value, int) or value not in ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES:
                raise ValueError("attachment auto-download threshold must be one of the supported MiB choices")
            result[key] = value
        elif key == "attachment_explicit_download_limit_bytes":
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value not in ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES):
                raise ValueError("explicit attachment download limit must be one of the supported MiB choices")
            result[key] = value
        elif key in {"default_model", "default_provider"}:
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError(f"{key} must be text of at most 256 characters")
            result[key] = value.strip()
        elif key == "mcp_mode":
            if value not in {"inherit", "selected"}:
                raise ValueError("mcp_mode must be 'inherit' or 'selected'")
            result[key] = value
        elif key in {"selected_mcp_servers", "selected_skills", "personal_skills"}:
            result[key] = _normalize_names(value, key)
        elif key == "personal_mcp_servers":
            if not isinstance(value, dict) or len(value) > 50:
                raise ValueError("personal_mcp_servers must be an object with at most 50 servers")
            clean_servers: dict[str, Any] = {}
            for name, raw_config in value.items():
                name = str(name or "").strip()
                if not _MCP_NAME_RE.fullmatch(name) or not isinstance(raw_config, dict):
                    raise ValueError("Invalid personal MCP server name or configuration")
                config = json.loads(json.dumps(raw_config))
                validate_personal_mcp_config(name, config)
                clean_servers[name] = config
            result[key] = clean_servers
    return result


def _validated_private_mcp_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate only MCP fields for the dedicated profile/user-private store path."""
    if not isinstance(changes, dict):
        raise ValueError("Private MCP settings must be a mapping")
    allowed = {"mcp_mode", "selected_mcp_servers", "personal_mcp_servers"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported private MCP setting: {sorted(unknown)[0]}")
    result: dict[str, Any] = {}
    if "mcp_mode" in changes:
        value = changes["mcp_mode"]
        if value not in {"inherit", "selected"}:
            raise ValueError("mcp_mode must be 'inherit' or 'selected'")
        result["mcp_mode"] = value
    if "selected_mcp_servers" in changes:
        names = _normalize_names(changes["selected_mcp_servers"], "selected_mcp_servers")
        if any(not _MCP_NAME_RE.fullmatch(name) for name in names):
            raise ValueError("Invalid selected MCP server name")
        result["selected_mcp_servers"] = names
    if "personal_mcp_servers" in changes:
        servers = changes["personal_mcp_servers"]
        if not isinstance(servers, dict) or len(servers) > 50:
            raise ValueError("personal_mcp_servers must be an object with at most 50 servers")
        clean_servers: dict[str, Any] = {}
        for raw_name, raw_config in servers.items():
            name = str(raw_name or "").strip()
            if not _MCP_NAME_RE.fullmatch(name) or not isinstance(raw_config, dict):
                raise ValueError("Invalid personal MCP server name or configuration")
            config = json.loads(json.dumps(raw_config))
            validate_personal_mcp_config(name, config)
            clean_servers[name] = config
        result["personal_mcp_servers"] = clean_servers
    return result


def load_personal_settings(profile_home: str | Path, user_id: str | int) -> dict[str, Any]:
    """Load one user's settings; return safe defaults if the file is absent or unreadable."""
    path = personal_settings_path(profile_home, user_id)

    def _validated_record(raw: dict[str, Any]) -> dict[str, Any]:
        clean = dict(_DEFAULTS)
        source = {k: v for k, v in raw.items() if k in _DEFAULTS and k != "updated_at"}
        mcp_keys = {"mcp_mode", "selected_mcp_servers", "personal_mcp_servers"}
        mcp_changes = {key: source.pop(key) for key in tuple(source) if key in mcp_keys}
        clean.update(_validated_changes(source))
        try:
            clean.update(_validated_private_mcp_changes(mcp_changes))
        except Exception:
            logger.warning("Could not read private MCP settings for user %s; using MCP defaults", user_id, exc_info=True)
        clean["updated_at"] = str(raw.get("updated_at") or "")
        return clean

    from agent.storage_mode import mongo_primary_enabled
    from gateway import mongo_multiuser
    if mongo_primary_enabled(profile_home):
        remote = mongo_multiuser.store_for_home(profile_home).get(
            mongo_multiuser.principal(user_id), "settings", "personal"
        )
        if remote is None:
            return dict(_DEFAULTS)
        if not isinstance(remote, dict):
            raise RuntimeError("Invalid Mongo personal settings record")
        return _validated_record(remote)
    with _lock_for(path):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root is not an object")
            return _validated_record(raw)
        except FileNotFoundError:
            return dict(_DEFAULTS)
        except Exception:
            logger.warning("Could not read Telegram personal settings for user %s; using defaults", user_id, exc_info=True)
            return dict(_DEFAULTS)


def effective_auto_load_skill_names(
    profile_home: str | Path, user_id: str | int, *, settings: dict[str, Any] | None = None,
) -> list[str]:
    """Return profile auto-load skills plus this user's explicit additions for display.

    Profile defaults are derived from the bundled provenance manifest; custom, private, and Hub
    skills are not inferred as defaults. Explicit profile ``auto_load: []`` and personal selections
    remain unchanged.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    home_token = set_hermes_home_override(str(profile_home))
    try:
        from agent.skill_commands import resolve_profile_auto_load_skill_names
        profile_names = resolve_profile_auto_load_skill_names()
    except Exception:
        profile_names = []
    finally:
        reset_hermes_home_override(home_token)
    personal = settings if isinstance(settings, dict) else load_personal_settings(profile_home, user_id)
    names = [*profile_names, *(personal.get("selected_skills") or [])]
    return list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))


def save_personal_settings(
    profile_home: str | Path, user_id: str | int, changes: dict[str, Any]
) -> dict[str, Any]:
    """Atomically update validated fields for one Telegram user in one profile."""
    path = personal_settings_path(profile_home, user_id)
    clean_changes = _validated_changes(changes)
    from agent.storage_mode import mongo_primary_enabled
    from gateway import mongo_multiuser
    if mongo_primary_enabled(profile_home):
        store = mongo_multiuser.store_for_home(profile_home)
        actor = mongo_multiuser.principal(user_id)
        current = load_personal_settings(profile_home, user_id)
        current.update(clean_changes)
        current["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        store.put(actor, "settings", "personal", current)
        return current
    with _lock_for(path):
        current = load_personal_settings(profile_home, user_id)
        current.update(clean_changes)
        current["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        atomic_json_write(path, current, indent=2)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return current


def save_personal_mcp_settings(
    profile_home: str | Path, user_id: str | int, changes: dict[str, Any]
) -> dict[str, Any]:
    """Persist MCP-only preferences to one Telegram user's private profile store.

    This separate path does not relax ``save_personal_settings``: callers can only
    change MCP mode, selected names, and validated personal MCP server entries.
    """
    path = personal_settings_path(profile_home, user_id)
    clean_changes = _validated_private_mcp_changes(changes)
    if not clean_changes:
        raise ValueError("At least one private MCP setting is required")
    from agent.storage_mode import mongo_primary_enabled
    from gateway import mongo_multiuser
    if mongo_primary_enabled(profile_home):
        store = mongo_multiuser.store_for_home(profile_home)
        actor = mongo_multiuser.principal(user_id)
        current = load_personal_settings(profile_home, user_id)
        current.update(clean_changes)
        current["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        store.put(actor, "settings", "personal", current)
        return current
    with _lock_for(path):
        current = load_personal_settings(profile_home, user_id)
        current.update(clean_changes)
        current["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        atomic_json_write(path, current, indent=2)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return current


def public_personal_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Return the allowlisted preference payload suitable for display/export."""
    output = {key: settings.get(key, default) for key, default in _DEFAULTS.items()}
    servers = output.get("personal_mcp_servers")
    if not isinstance(servers, dict):
        return output

    def redact_config(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if lowered == "url" and isinstance(item, str):
                    parsed = urlsplit(item)
                    if parsed.username is not None or parsed.password is not None or any(
                        _CREDENTIAL_NAME_RE.search(query_key)
                        for query_key, _query_value in parse_qsl(parsed.query, keep_blank_values=True)
                    ) or _CREDENTIAL_NAME_RE.search(parsed.fragment):
                        redacted[key] = "[REDACTED credential-bearing endpoint]"
                    else:
                        redacted[key] = item
                elif lowered == "env" and isinstance(item, dict):
                    redacted[key] = {
                        env_key: (env_value if isinstance(env_value, str) and _ENV_REF_RE.fullmatch(env_value.strip()) else "[REDACTED]")
                        for env_key, env_value in item.items()
                    }
                elif lowered == "headers" and isinstance(item, dict):
                    redacted[key] = {
                        header: (header_value if _is_credential_reference(header_value) else "[REDACTED]")
                        for header, header_value in item.items()
                    }
                elif lowered == "args" and isinstance(item, (list, tuple)):
                    args = [str(arg) for arg in item]
                    for index, arg in enumerate(args):
                        header_match = _CREDENTIAL_HEADER_RE.search(arg)
                        if header_match:
                            header_value = header_match.group(1).strip().strip("\"'")
                            if not _is_credential_reference(header_value):
                                args[index] = arg[:header_match.start(1)] + "[REDACTED]"
                                continue
                        if _CREDENTIAL_ARG_RE.match(arg):
                            if "=" in arg:
                                flag, secret = arg.split("=", 1)
                                if not _ENV_REF_RE.fullmatch(secret.strip()):
                                    args[index] = flag + "=[REDACTED]"
                            elif index + 1 < len(args) and not _ENV_REF_RE.fullmatch(args[index + 1].strip()):
                                args[index + 1] = "[REDACTED]"
                    redacted[key] = args
                elif _CREDENTIAL_NAME_RE.search(key_text):
                    redacted[key] = item if isinstance(item, str) and _ENV_REF_RE.fullmatch(item.strip()) else "[REDACTED]"
                else:
                    redacted[key] = redact_config(item)
            return redacted
        if isinstance(value, list):
            return [redact_config(item) for item in value]
        return value

    output["personal_mcp_servers"] = {name: redact_config(config) for name, config in servers.items()}
    return output


# A context-local identity is set only while handling an authorized Telegram DM.
# It follows asyncio.to_thread/context propagation but cannot bleed into another
# concurrent user's turn or the profile's ordinary group/CLI sessions.
from contextvars import ContextVar

_PERSONAL_MEMORY_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "telegram_personal_memory_context", default=None,
)


def set_personal_memory_context(
    profile_home: str | Path, user_id: str | int, *,
    memory_enabled: bool = True, self_learning_enabled: bool = True,
):
    """Set the private-memory identity for one authorized Telegram DM turn."""
    path = personal_settings_path(profile_home, user_id).parent / "memory"
    if path.is_symlink():
        raise ValueError("Personal memory path cannot use symlinks")
    path = path.resolve()
    home = Path(profile_home).expanduser().resolve()
    if not path.is_relative_to(home):
        raise ValueError("Personal memory path escaped the active profile")
    return _PERSONAL_MEMORY_CONTEXT.set({
        "home": str(home), "user_id": str(user_id), "memory_dir": str(path),
        "memory_enabled": bool(memory_enabled),
        "self_learning_enabled": bool(self_learning_enabled),
    })


def reset_personal_memory_context(token) -> None:
    _PERSONAL_MEMORY_CONTEXT.reset(token)


def current_personal_memory_context() -> dict[str, Any] | None:
    """Return the current Telegram DM memory scope, if one is active."""
    return _PERSONAL_MEMORY_CONTEXT.get()


def current_personal_memory_dir() -> Path | None:
    context = current_personal_memory_context()
    return Path(context["memory_dir"]) if context and context.get("memory_dir") else None


def current_personal_memory_flags() -> tuple[bool, bool] | None:
    context = current_personal_memory_context()
    if context is None:
        return None
    enabled = bool(context.get("memory_enabled", True))
    return enabled, enabled


def current_personal_learning_enabled() -> bool | None:
    context = current_personal_memory_context()
    if context is None:
        return None
    return bool(context.get("self_learning_enabled", True))


def apply_personal_settings_to_user_config(user_config: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """Copy and overlay one user's defaults; never mutate the shared profile config."""
    import copy

    config = copy.deepcopy(user_config) if isinstance(user_config, dict) else {}
    model = config.get("model")
    if not isinstance(model, dict):
        model = {}
        config["model"] = model
    if settings.get("default_model"):
        model["default"] = str(settings["default_model"])
        if settings.get("default_provider"):
            model["provider"] = str(settings["default_provider"])
    memory = config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        config["memory"] = memory
    if not settings.get("memory_enabled", True):
        memory["memory_enabled"] = False
        memory["user_profile_enabled"] = False
    # Profile servers remain profile-owned. A private Telegram user may also add
    # validated personal servers; only selected servers enter this turn's config.
    if settings.get("mcp_mode") == "selected":
        servers = config.get("mcp_servers")
        servers = dict(servers) if isinstance(servers, dict) else {}
        personal_servers = settings.get("personal_mcp_servers")
        if isinstance(personal_servers, dict):
            servers.update(personal_servers)
        selected = set(settings.get("selected_mcp_servers") or [])
        config["mcp_servers"] = {name: value for name, value in servers.items() if name in selected}
    return config
