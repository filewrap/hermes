"""Validation of an exported external-agent tree.

Checks run after apply, before the export is reported as done:

- ``openclaw.json`` and ``agents/<id>/models.json`` parse as JSON and
  carry the sections the export promised.
- Every ``models.providers.<id>`` entry has the row shape the forward
  provider emits (``baseUrl``, ``api``, ``models[]`` with ``id``/``name``/
  ``api``); the default model's provider exists in the emitted providers.
- No harvested secret value appears in any *live* file (openclaw.json,
  models.json, workspace files, ``.env.example``, the report). Archive
  files are excluded by design — the state.db snapshot may hold secrets
  that were inside sessions — and the report says so.
- Every workspace file is byte-identical to its Hermes source.

Secret values are harvested from the source ``.env`` and from inline
``api_key``/``apiKey`` strings in ``config.yaml``. Values are held in
memory only, never written, and validation failures name files, never
values. Short values (< 8 chars) are skipped to avoid false positives
on values like ``true`` or short model names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from hermes_cli.export_agent_model import read_env_reference

# External-agent model api values (forward contract's model-api type).
MODEL_APIS = {
    "anthropic-messages",
    "openai-completions",
    "openai-responses",
    "openai-chatgpt-responses",
}

_MIN_SECRET_LEN = 8

# Live files are scanned for secret values; archive/ is excluded by design.
LIVE_SCAN_SUFFIXES = {".json", ".md", ".example", ".txt"}


def harvest_secret_values(config: dict[str, Any], env: dict[str, str]) -> list[str]:
    """Secret values that must never appear in a live exported file."""
    values: set[str] = set()
    for value in env.values():
        if isinstance(value, str) and len(value.strip()) >= _MIN_SECRET_LEN:
            values.add(value.strip())
    for section in (config.get("providers"),):
        if isinstance(section, dict):
            for raw in section.values():
                if isinstance(raw, dict):
                    for key in ("api_key", "apiKey"):
                        candidate = raw.get(key)
                        if (
                            isinstance(candidate, str)
                            and not read_env_reference(candidate)
                            and len(candidate) >= _MIN_SECRET_LEN
                        ):
                            values.add(candidate)
    custom = config.get("custom_providers")
    if isinstance(custom, list):
        for raw in custom:
            if isinstance(raw, dict):
                for key in ("api_key", "apiKey"):
                    candidate = raw.get(key)
                    if (
                        isinstance(candidate, str)
                        and not read_env_reference(candidate)
                        and len(candidate) >= _MIN_SECRET_LEN
                    ):
                        values.add(candidate)
    model = config.get("model")
    if isinstance(model, dict):
        for key in ("api_key", "apiKey"):
            candidate = model.get(key)
            if (
                isinstance(candidate, str)
                and not read_env_reference(candidate)
                and len(candidate) >= _MIN_SECRET_LEN
            ):
                values.add(candidate)
    return sorted(values)


def _is_live_file(path: Path, output_dir: Path) -> bool:
    try:
        relative = path.relative_to(output_dir)
    except ValueError:
        return False
    if relative.parts and relative.parts[0] == "archive":
        return False
    return path.suffix in LIVE_SCAN_SUFFIXES or path.name in ("export-report.json",)


def scan_live_files_for_secrets(
    output_dir: Path, secret_values: list[str]
) -> list[str]:
    """Live files containing a harvested secret value (relative paths)."""
    offenders: list[str] = []
    if not secret_values:
        return offenders
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or not _is_live_file(path, output_dir):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        for value in secret_values:
            if value in text:
                offenders.append(str(path.relative_to(output_dir)))
                break
    return offenders


def validate_export_document(document: dict[str, Any]) -> list[str]:
    """Shape errors in an ``openclaw.json`` document (empty = valid)."""
    errors: list[str] = []
    agents = document.get("agents")
    if agents is not None and not isinstance(agents, dict):
        errors.append("openclaw.json: 'agents' must be an object")
    defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
    model = defaults.get("model") if isinstance(defaults, dict) else None
    primary: Optional[str] = None
    if isinstance(model, str):
        primary = model
    elif isinstance(model, dict):
        primary = model.get("primary")
        if primary is not None and not isinstance(primary, str):
            errors.append(
                "openclaw.json: 'agents.defaults.model.primary' must be a string"
            )
        fallbacks = model.get("fallbacks")
        if fallbacks is not None and (
            not isinstance(fallbacks, list)
            or not all(isinstance(f, str) for f in fallbacks)
        ):
            errors.append(
                "openclaw.json: 'agents.defaults.model.fallbacks' must be a list of strings"
            )
    models = document.get("models")
    providers: dict[str, Any] = {}
    if models is not None:
        if not isinstance(models, dict):
            errors.append("openclaw.json: 'models' must be an object")
        else:
            providers = models.get("providers", {})
            if not isinstance(providers, dict):
                errors.append("openclaw.json: 'models.providers' must be an object")
                providers = {}
            if models.get("mode") is not None and models.get("mode") not in (
                "merge",
                "replace",
            ):
                errors.append(
                    "openclaw.json: 'models.mode' must be 'merge' or 'replace'"
                )
    errors.extend(_validate_providers(providers, "openclaw.json"))
    if primary:
        provider = primary.split("/")[0] if "/" in primary else ""
        if provider and provider not in providers:
            errors.append(
                f"openclaw.json: default model '{primary}' names provider '{provider}' with no models.providers entry"
            )
    mcp = document.get("mcp")
    if mcp is not None:
        if not isinstance(mcp, dict) or not isinstance(mcp.get("servers", {}), dict):
            errors.append("openclaw.json: 'mcp.servers' must be an object")
        else:
            for name, server in mcp["servers"].items():
                if not isinstance(server, dict):
                    errors.append(
                        f"openclaw.json: mcp server '{name}' must be an object"
                    )
                elif not server.get("command") and not server.get("url"):
                    errors.append(
                        f"openclaw.json: mcp server '{name}' has neither command nor url"
                    )
    return errors


def _validate_providers(providers: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    for provider_id, entry in providers.items():
        where = f"{label}: provider '{provider_id}'"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        if not isinstance(entry.get("baseUrl"), str) or not entry["baseUrl"].strip():
            errors.append(f"{where} is missing baseUrl")
        if entry.get("api") not in MODEL_APIS:
            errors.append(f"{where} has unknown api {entry.get('api')!r}")
        models = entry.get("models")
        if not isinstance(models, list) or not models:
            errors.append(f"{where} must list at least one model")
            continue
        for row in models:
            if not isinstance(row, dict) or not row.get("id"):
                errors.append(f"{where} has a model row without an id")
            elif row.get("api") not in MODEL_APIS:
                errors.append(
                    f"{where}: model '{row.get('id')}' has unknown api {row.get('api')!r}"
                )
    return errors


def validate_export_tree(
    output_dir: Path, *, source_files: dict[str, Path], secret_values: list[str]
) -> list[str]:
    """Full post-apply validation of an export tree. Empty = valid."""
    errors: list[str] = []
    exported_config_path = output_dir / "openclaw.json"
    try:
        document = json.loads(exported_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"openclaw.json is not readable JSON: {exc}"]
    if not isinstance(document, dict):
        return ["openclaw.json must contain a JSON object"]
    errors.extend(validate_export_document(document))
    agent_dirs = (
        [child for child in (output_dir / "agents").iterdir() if child.is_dir()]
        if (output_dir / "agents").is_dir()
        else []
    )
    for agent_dir in agent_dirs:
        models_path = agent_dir / "models.json"
        if not models_path.is_file():
            errors.append(f"agents/{agent_dir.name}/models.json is missing")
            continue
        try:
            models_doc = json.loads(models_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(
                f"agents/{agent_dir.name}/models.json is not readable JSON: {exc}"
            )
            continue
        if not isinstance(models_doc, dict) or not isinstance(
            models_doc.get("providers"), dict
        ):
            errors.append(
                f"agents/{agent_dir.name}/models.json must contain a 'providers' object"
            )
            continue
        errors.extend(
            _validate_providers(
                models_doc["providers"], f"agents/{agent_dir.name}/models.json"
            )
        )
        # models.json providers must match openclaw.json providers.
        exported_providers = (document.get("models") or {}).get("providers", {})
        if set(models_doc["providers"]) != set(exported_providers):
            errors.append(
                f"agents/{agent_dir.name}/models.json providers differ from openclaw.json models.providers"
            )
    for name, source_path in source_files.items():
        target = None
        for agent_dir in agent_dirs:
            candidate = agent_dir / name
            if candidate.is_file():
                target = candidate
                break
        if target is None:
            errors.append(f"workspace file {name} was not written")
            continue
        try:
            if target.read_bytes() != source_path.read_bytes():
                errors.append(f"workspace file {name} differs from its Hermes source")
        except OSError as exc:
            errors.append(f"workspace file {name} is not readable: {exc}")
    for offender in scan_live_files_for_secrets(output_dir, secret_values):
        errors.append(f"secret value found in live file: {offender}")
    return errors
