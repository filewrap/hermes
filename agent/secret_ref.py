"""Typed SecretRef credential references, external-agent-shaped.

A config value that authenticates something may be a literal string or a
``SecretRef`` — ``{"source": "env"|"file"|"exec"|"store", "provider": "<alias>",
"id": "<provider-specific id>"}`` — naming the secret instead of holding it.
Refs are materialised at agent-runtime-config time (the credential resolvers in
``hermes_cli/``), so provider clients only ever receive plain values; a ref that
is still a ref at a read site is an error in ``strict`` mode and a
``configured_unavailable`` report in ``inspect`` mode (diagnostics only — never
a silent fallthrough to another account's credential).

Sources, mirroring the external agent's ``src/secrets/ref-contract.ts`` +
``src/secrets/resolve.ts``:

* ``env`` — the profile secret scope (``agent.secret_scope.get_secret``);
  provider ``default`` is the built-in one.
* ``store`` — the profile's durable credential store (``<home>/.env`` plus the
  encrypted credential record, via ``load_env_file``); no ``os.environ``
  fallthrough, so a store ref never reads the launch profile's process env.
* ``file`` — a JSON file declared by a ``secrets.providers.<name>`` entry
  (``{source: "file", path, mode: "json"|"singleValue"}``); the id is a JSON
  Pointer (``/providers/openai/apiKey``) or ``"value"``.
* ``exec`` — a command declared by a ``secrets.providers.<name>`` entry; the
  command receives ``{"protocolVersion": 1, "provider", "ids"}`` on stdin and
  answers ``{"protocolVersion": 1, "values": {id: value}}`` on stdout
  (5s timeout, 1MB output cap, empty base env + allowlisted ``pass_env`` +
  declared ``env``, cwd = the command's directory).

``$NAME`` / ``${NAME}`` strings parse as env refs, as in the external agent; a bare
literal never does, so an opaque token is never misread as a reference.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat as statmod
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

SecretRefSource = Literal["env", "file", "exec", "store"]
SecretInputStringResolutionMode = Literal["strict", "inspect"]

DEFAULT_SECRET_PROVIDER_ALIAS = "default"
SINGLE_VALUE_FILE_REF_ID = "value"

ENV_SECRET_REF_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SECRET_PROVIDER_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EXEC_SECRET_REF_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}$")
_FILE_SEGMENT_RE = re.compile(r"^(?:[^~]|~0|~1)*$")
_ENV_TEMPLATE_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]{0,127})\}$")
_ENV_SHORTHAND_RE = re.compile(r"^\$([A-Z][A-Z0-9_]{0,127})$")

# The external agent's caps, mirrored: one file read / one exec answer is bounded, and a
# batch of refs to one provider is a single provider call.
_FILE_MAX_BYTES = 1024 * 1024
_EXEC_TIMEOUT_MS = 5_000
_EXEC_MAX_OUTPUT_BYTES = 1024 * 1024
_EXEC_MAX_BATCH_BYTES = 256 * 1024


@dataclass(frozen=True)
class SecretRef:
    """A typed reference to a secret in a configured source."""

    source: SecretRefSource
    provider: str
    id: str

    def key(self) -> str:
        return f"{self.source}:{self.provider}:{self.id}"


def is_secret_ref(value: Any) -> bool:
    """True when *value* is exactly a ``{source, provider, id}`` mapping."""
    if not isinstance(value, dict) or len(value) != 3:
        return False
    return (
        value.get("source") in ("env", "file", "exec", "store")
        and isinstance(value.get("provider"), str)
        and bool(value["provider"].strip())
        and isinstance(value.get("id"), str)
        and bool(value["id"].strip())
    )


def is_valid_env_secret_ref_id(value: str) -> bool:
    return bool(ENV_SECRET_REF_ID_RE.match(value))


def is_valid_secret_provider_alias(value: str) -> bool:
    return bool(SECRET_PROVIDER_ALIAS_RE.match(value))


def is_valid_file_secret_ref_id(value: str) -> bool:
    if value == SINGLE_VALUE_FILE_REF_ID:
        return True
    if not value.startswith("/"):
        return False
    return all(_FILE_SEGMENT_RE.match(segment) for segment in value[1:].split("/"))


def is_valid_exec_secret_ref_id(value: str) -> bool:
    if not EXEC_SECRET_REF_ID_RE.match(value):
        return False
    return all(segment not in (".", "..") for segment in value.split("/"))


def is_valid_secret_ref(ref: SecretRef) -> bool:
    if not is_secret_ref({"source": ref.source, "provider": ref.provider, "id": ref.id}):
        return False
    if not is_valid_secret_provider_alias(ref.provider):
        return False
    if ref.source in ("env", "store"):
        return is_valid_env_secret_ref_id(ref.id)
    if ref.source == "file":
        return is_valid_file_secret_ref_id(ref.id)
    return is_valid_exec_secret_ref_id(ref.id)


def secret_ref_key(ref: SecretRef) -> str:
    return ref.key()


def parse_env_template_secret_ref(value: Any, provider: str = DEFAULT_SECRET_PROVIDER_ALIAS) -> Optional[SecretRef]:
    """``$NAME`` / ``${NAME}`` → env ref; anything else → None (never a bare literal)."""
    if not isinstance(value, str):
        return None
    match = _ENV_TEMPLATE_RE.match(value.strip()) or _ENV_SHORTHAND_RE.match(value.strip())
    if not match:
        return None
    return SecretRef(source="env", provider=provider.strip() or DEFAULT_SECRET_PROVIDER_ALIAS, id=match.group(1))


def _is_legacy_ref_without_provider(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("source") in ("env", "file", "exec", "store")
        and isinstance(value.get("id"), str)
        and bool(value["id"].strip())
        and value.get("provider") is None
    )


def coerce_secret_ref(value: Any, defaults: Optional[dict[str, str]] = None) -> Optional[SecretRef]:
    """Structured ref, legacy ``{source, id}`` (provider filled from *defaults*), or env shorthand."""
    if _is_legacy_ref_without_provider(value):
        provider = (defaults or {}).get(value["source"]) or DEFAULT_SECRET_PROVIDER_ALIAS
        return SecretRef(source=value["source"], provider=provider, id=value["id"])
    if is_secret_ref(value):
        return SecretRef(source=value["source"], provider=value["provider"], id=value["id"])
    return parse_env_template_secret_ref(value, (defaults or {}).get("env", DEFAULT_SECRET_PROVIDER_ALIAS))


def parse_secret_ref(value: Any, defaults: Optional[dict[str, str]] = None) -> Optional[SecretRef]:
    return coerce_secret_ref(value, defaults)


def normalize_secret_input_string(value: Any) -> Optional[str]:
    """A non-blank literal string, trimmed; None for refs, blanks, and non-strings."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


class UnresolvedSecretInputError(RuntimeError):
    """A strict secret read met a configured but unmaterialised SecretRef."""

    def __init__(self, path: str, ref: SecretRef):
        super().__init__(
            f"{path}: unresolved SecretRef \"{ref.key()}\". Resolve it against the active "
            "profile secret scope before reading it."
        )
        self.path = path
        self.ref = ref


@dataclass(frozen=True)
class SecretInputResolution:
    """Outcome of reading a secret input without necessarily materialising it."""

    status: Literal["available", "configured_unavailable", "missing"]
    value: Optional[str] = None
    ref: Optional[SecretRef] = None


def resolve_secret_input(
    value: Any, *, path: str, mode: SecretInputStringResolutionMode = "strict",
    defaults: Optional[dict[str, str]] = None,
) -> SecretInputResolution:
    """Read a secret input: a literal is available as-is; a ref throws in strict
    mode and reports ``configured_unavailable`` (with the ref, never the value)
    in inspect mode; anything else is missing."""
    ref = coerce_secret_ref(value, defaults)
    literal = normalize_secret_input_string(value)
    if literal is not None and ref is None:
        return SecretInputResolution(status="available", value=literal)
    if ref is None:
        return SecretInputResolution(status="missing")
    if mode == "strict":
        raise UnresolvedSecretInputError(path, ref)
    return SecretInputResolution(status="configured_unavailable", ref=ref)


def assert_secret_input_resolved(value: Any, *, path: str, defaults: Optional[dict[str, str]] = None) -> None:
    """Throw when a secret field still contains an unresolved SecretRef."""
    ref = coerce_secret_ref(value, defaults)
    if ref is not None:
        raise UnresolvedSecretInputError(path, ref)


# ── Providers ─────────────────────────────────────────────────────────────────


def _read_providers_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The ``secrets:`` section of the active config (``providers`` + ``defaults``).

    Reads the raw user config; any failure yields an empty section, so the
    built-in env/store providers (which need no declaration) keep working and a
    file/exec ref simply reports its provider as unconfigured.
    """
    if isinstance(config, dict):
        section = config.get("secrets")
        return section if isinstance(section, dict) else {}
    try:
        from hermes_cli.config import read_raw_config

        section = read_raw_config().get("secrets")
        return section if isinstance(section, dict) else {}
    except Exception:
        # A config read must never break credential resolution: the built-in
        # env/store providers need no declaration, and a file/exec ref simply
        # reports its provider as unconfigured.
        logger.warning("Could not read the secrets: config section", exc_info=True)
        return {}


def _provider_config(ref: SecretRef, section: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The declared provider for *ref*, or the built-in default for env/store."""
    if ref.provider == DEFAULT_SECRET_PROVIDER_ALIAS and ref.source in ("env", "store"):
        declared = (section.get("providers") or {}).get(ref.provider)
        if isinstance(declared, dict):
            return declared
        return {"source": ref.source}
    providers = section.get("providers") or {}
    declared = providers.get(ref.provider) if isinstance(providers, dict) else None
    return declared if isinstance(declared, dict) else None


def _check_provider_source(ref: SecretRef, provider_cfg: dict[str, Any]) -> None:
    """Refuse a ref whose provider declares another source (a misspelled provider
    must not silently resolve from the wrong store)."""
    declared_source = provider_cfg.get("source")
    if declared_source is not None and declared_source != ref.source:
        raise ValueError(
            f"Secret provider \"{ref.provider}\" has source \"{declared_source}\" "
            f"but ref requests \"{ref.source}\" (ref: {ref.key()})."
        )


class SecretResolutionError(RuntimeError):
    """A SecretRef could not be materialised (missing provider, missing value, bad output)."""

    def __init__(self, ref: SecretRef, message: str):
        super().__init__(f"{ref.key()}: {message}")
        self.ref = ref


def _materialize_env(ref: SecretRef) -> Optional[str]:
    from agent.secret_scope import get_secret

    return get_secret(ref.id)


def _materialize_store(ref: SecretRef) -> Optional[str]:
    """The profile's durable store: ``<home>/.env`` plus the encrypted credential
    record (``load_env_file`` merges both). No ``os.environ`` fallthrough — a
    store ref never reads the launch profile's process env."""
    from agent.secret_scope import load_env_file
    from hermes_constants import get_hermes_home

    return load_env_file(Path(get_hermes_home()) / ".env").get(ref.id)


def _read_json_pointer(payload: Any, pointer: str) -> Any:
    """RFC 6901 pointer walk (``~0`` → ``~``, ``~1`` → ``/``); KeyError/IndexError on a miss."""
    current = payload
    for raw_segment in pointer[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[segment]
        elif isinstance(current, list):
            current = current[int(segment)]
        else:
            raise KeyError(segment)
    return current


def _materialize_file(ref: SecretRef, provider_cfg: dict[str, Any]) -> Optional[str]:
    path = provider_cfg.get("path")
    if not isinstance(path, str) or not path.strip():
        raise SecretResolutionError(ref, f"file provider \"{ref.provider}\" declares no path.")
    file_path = Path(os.path.expanduser(path.strip()))
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise SecretResolutionError(ref, f"file provider \"{ref.provider}\" is unreadable: {exc}") from exc
    if len(raw) > _FILE_MAX_BYTES:
        raise SecretResolutionError(ref, f"file provider \"{ref.provider}\" exceeds {_FILE_MAX_BYTES} bytes.")
    mode = provider_cfg.get("mode", "json")
    if mode == "singleValue":
        if ref.id != SINGLE_VALUE_FILE_REF_ID:
            raise SecretResolutionError(
                ref, f"singleValue file provider \"{ref.provider}\" expects ref id \"{SINGLE_VALUE_FILE_REF_ID}\"."
            )
        return raw.decode("utf-8", "replace").rstrip("\r\n") or None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SecretResolutionError(ref, f"file provider \"{ref.provider}\" payload is not JSON: {exc}") from exc
    if ref.id == SINGLE_VALUE_FILE_REF_ID:
        return str(payload) if not isinstance(payload, (dict, list)) else None
    try:
        value = _read_json_pointer(payload, ref.id)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise SecretResolutionError(ref, f"no value at {ref.id!r} in file provider \"{ref.provider}\".") from exc
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _assert_secure_exec_command(command: str, label: str, trusted_dirs: Optional[list] = None) -> Path:
    """Mirror of the external agent's exec-provider path validation: absolute path, a real
    file (never a symlink), inside ``trusted_dirs`` when declared, not
    group/world-writable, owned by the current user."""
    command_path = Path(os.path.expanduser(command)).resolve() if command else Path()
    if not command or not command_path.is_absolute():
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""), f"{label} must be an absolute path."
        )
    try:
        stat = command_path.stat()
    except OSError as exc:
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""), f"{label} is not readable: {command_path}"
        ) from exc
    if not statmod.S_ISREG(stat.st_mode):
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""), f"{label} must be a file: {command_path}"
        )
    if command_path.is_symlink():
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""), f"{label} must not be a symlink: {command_path}"
        )
    if trusted_dirs:
        dirs = [Path(os.path.expanduser(str(d))).resolve() for d in trusted_dirs]
        if not any(str(command_path).startswith(str(d) + os.sep) or command_path == d for d in dirs):
            raise SecretResolutionError(
                SecretRef(source="exec", provider=label, id=""),
                f"{label} is outside trustedDirs: {command_path}",
            )
    if stat.st_mode & 0o022:
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""), f"{label} permissions are too open: {command_path}"
        )
    try:
        owner_uid = stat.st_uid
    except AttributeError:
        owner_uid = None
    if owner_uid is not None and hasattr(os, "getuid") and owner_uid != os.getuid():
        raise SecretResolutionError(
            SecretRef(source="exec", provider=label, id=""),
            f"{label} must be owned by the current user: {command_path}",
        )
    return command_path


def _invoke_exec_provider(
    command_path: Path, request: str, provider_cfg: dict[str, Any], ref: SecretRef,
) -> str:
    """Run one exec-provider call; returns trimmed stdout. The child gets an
    empty base env + allowlisted ``pass_env`` + declared ``env``."""
    provider_name = ref.provider
    child_env: dict[str, str] = {}
    for key in provider_cfg.get("pass_env") or []:
        if isinstance(key, str) and os.environ.get(key) is not None:
            child_env[key] = os.environ[key]
    for key, value in (provider_cfg.get("env") or {}).items():
        if isinstance(key, str):
            child_env[key] = str(value)
    timeout_ms = provider_cfg.get("timeout_ms", _EXEC_TIMEOUT_MS)
    try:
        timeout_s = max(1, int(timeout_ms) / 1000)
    except (TypeError, ValueError):
        timeout_s = _EXEC_TIMEOUT_MS / 1000
    max_output = provider_cfg.get("max_output_bytes", _EXEC_MAX_OUTPUT_BYTES)
    try:
        max_output = max(1, int(max_output))
    except (TypeError, ValueError):
        max_output = _EXEC_MAX_OUTPUT_BYTES
    args = [str(a) for a in (provider_cfg.get("args") or []) if isinstance(a, (str, int, float))]
    try:
        proc = subprocess.run(
            [str(command_path), *args],
            input=request.encode("utf-8"),
            capture_output=True,
            timeout=timeout_s,
            cwd=str(command_path.parent),
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SecretResolutionError(ref, f"exec provider \"{provider_name}\" timed out.") from exc
    except OSError as exc:
        raise SecretResolutionError(ref, f"exec provider \"{provider_name}\" failed to start: {exc}") from exc
    if len(proc.stdout) > max_output or len(proc.stderr) > max_output:
        raise SecretResolutionError(ref, f"exec provider \"{provider_name}\" output exceeded maxOutputBytes.")
    if proc.returncode != 0:
        raise SecretResolutionError(
            ref, f"exec provider \"{provider_name}\" exited with code {proc.returncode}."
        )
    trimmed = proc.stdout.decode("utf-8", "replace").strip()
    if not trimmed:
        raise SecretResolutionError(ref, f"exec provider \"{provider_name}\" returned empty stdout.")
    return trimmed


def _materialize_exec_batch(
    refs: list[SecretRef], provider_name: str, provider_cfg: dict[str, Any]
) -> dict[str, Optional[str]]:
    """One exec-provider call for a batch of ids: stdin
    ``{"protocolVersion": 1, "provider", "ids"}``, stdout
    ``{"protocolVersion": 1, "values": {id: value}, "errors": {id: {code}}}``.
    ``json_only: false`` with a single id also accepts raw stdout text."""
    command = provider_cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" declares no command.")
    command_path = _assert_secure_exec_command(
        command, f"secrets.providers.{provider_name}.command", provider_cfg.get("trusted_dirs")
    )
    ids = sorted({ref.id for ref in refs})
    request = json.dumps({"protocolVersion": 1, "provider": provider_name, "ids": ids})
    if len(request.encode("utf-8")) > _EXEC_MAX_BATCH_BYTES:
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" request exceeds maxBatchBytes.")
    trimmed = _invoke_exec_provider(command_path, request, provider_cfg, refs[0])
    json_only = provider_cfg.get("json_only", True)
    try:
        parsed = json.loads(trimmed)
    except ValueError:
        if not json_only and len(ids) == 1:
            return {ids[0]: trimmed}
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" returned invalid JSON.") from None
    if not isinstance(parsed, dict):
        if not json_only and len(ids) == 1 and isinstance(parsed, str):
            return {ids[0]: parsed}
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" response must be an object.")
    if parsed.get("protocolVersion") != 1:
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" protocolVersion must be 1.")
    values = parsed.get("values")
    if not isinstance(values, dict):
        raise SecretResolutionError(refs[0], f"exec provider \"{provider_name}\" response missing \"values\".")
    errors = parsed.get("errors") if isinstance(parsed.get("errors"), dict) else {}
    out: dict[str, Optional[str]] = {}
    for ref_id in ids:
        if ref_id in errors:
            raise SecretResolutionError(
                SecretRef(source="exec", provider=provider_name, id=ref_id),
                f"exec provider \"{provider_name}\" failed for id \"{ref_id}\".",
            )
        if ref_id not in values:
            raise SecretResolutionError(
                SecretRef(source="exec", provider=provider_name, id=ref_id),
                f"exec provider \"{provider_name}\" response missing id \"{ref_id}\".",
            )
        value = values[ref_id]
        out[ref_id] = value if isinstance(value, str) else (json.dumps(value) if value is not None else None)
    return out


def _materialize_one(ref: SecretRef, section: dict[str, Any]) -> Optional[str]:
    """Materialise one ref; env/store/file resolve directly, exec batches via the group path."""
    provider_cfg = _provider_config(ref, section)
    if provider_cfg is None:
        raise SecretResolutionError(ref, f"Secret provider \"{ref.provider}\" is not configured.")
    _check_provider_source(ref, provider_cfg)
    if ref.source == "env":
        return _materialize_env(ref)
    if ref.source == "store":
        return _materialize_store(ref)
    if ref.source == "file":
        return _materialize_file(ref, provider_cfg)
    return _materialize_exec_batch([ref], ref.provider, provider_cfg)[ref.id]


def materialize_secret_ref(ref: SecretRef, *, config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Resolve one SecretRef to its value (None when the source holds nothing)."""
    if not is_valid_secret_ref(ref):
        raise SecretResolutionError(ref, "SecretRef fails contract validation.")
    return _materialize_one(ref, _read_providers_config(config))


def normalize_and_group_secret_refs(refs: list[SecretRef]) -> list[tuple[str, str, list[SecretRef]]]:
    """Dedupe refs and group by (source, provider) so one provider call serves many ids."""
    unique: dict[str, SecretRef] = {}
    for ref in refs:
        if not is_valid_secret_ref(ref):
            raise SecretResolutionError(ref, "SecretRef fails contract validation.")
        unique[ref.key()] = ref
    grouped: dict[tuple[str, str], list[SecretRef]] = {}
    for ref in unique.values():
        grouped.setdefault((ref.source, ref.provider), []).append(ref)
    return [(source, provider, group) for (source, provider), group in grouped.items()]


def materialize_secret_refs(
    refs: list[SecretRef], *, config: Optional[dict[str, Any]] = None
) -> dict[str, Optional[str]]:
    """Materialise a batch, grouped by provider (one file read / exec call per group)."""
    section = _read_providers_config(config)
    out: dict[str, Optional[str]] = {}
    for source, provider, group in normalize_and_group_secret_refs(refs):
        if source == "exec":
            provider_cfg = _provider_config(group[0], section)
            if provider_cfg is None:
                raise SecretResolutionError(group[0], f"Secret provider \"{provider}\" is not configured.")
            _check_provider_source(group[0], provider_cfg)
            for ref_id, value in _materialize_exec_batch(group, provider, provider_cfg).items():
                out[f"exec:{provider}:{ref_id}"] = value
            continue
        for ref in group:
            out[ref.key()] = _materialize_one(ref, section)
    return out


def materialize_secret_input(
    value: Any, *, path: str, config: Optional[dict[str, Any]] = None,
    mode: SecretInputStringResolutionMode = "strict",
) -> Optional[str]:
    """Materialise a SecretInput at agent-runtime-config time: a literal passes
    through, a ref resolves via its provider, anything else is None. Strict
    mode raises on an unresolvable ref instead of silently falling through to
    another credential; inspect mode returns None and leaves the ref for the
    caller to report as ``configured_unavailable``."""
    ref = coerce_secret_ref(value)
    if ref is None:
        return normalize_secret_input_string(value)
    if mode == "inspect":
        return None
    try:
        resolved = materialize_secret_ref(ref, config=config)
    except SecretResolutionError as exc:
        raise UnresolvedSecretInputError(path, ref) from exc
    if resolved is None:
        raise UnresolvedSecretInputError(path, ref)
    return resolved
