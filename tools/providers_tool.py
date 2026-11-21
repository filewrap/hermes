#!/usr/bin/env python3
"""``providers``: manage Hermes model providers — list, add, update, modify,
remove a credential, add keys, change a provider's default model, bulk-remove,
live-test, reorder, back up.

The external agent has no equivalent tool (verified 2026-11-11); this is new design on
Hermes' own provider machinery, duplicating none of it — every mutation goes
through the function that already owns that write:

- credentials: ``hermes_cli.auth_commands`` (``add_api_key_credential_for_ui``,
  ``remove_credential_for_ui``) over the credential pool;
- custom endpoints: the web custom-endpoint write path
  (``hermes_cli.web_routers.config_env._write_custom_endpoint``);
- live test: the ``/models`` probe (``hermes_cli.models.probe_api_models``)
  with discovered-model write-back guarded by
  ``hermes_cli.model_switch_providers._discovered_catalog_stale``;
- backup: the existing whole-home backup (``hermes_cli.backup.run_backup``).

Resolution follows the rows the UI renders (the honey-qwen lesson — the
provider-delete fix on main, ``remove_custom_provider_for_ui``): a provider id
resolves against the RAW config — the ``providers:`` map by stored key or entry
name, and the legacy ``custom_providers:`` list by name — slug-compared with
the ``custom:`` prefix stripped. A mixed-case stored key, a disabled entry, an
entry with no ``base_url``, and an endpoint stored twice (legacy list + keyed
map) all resolve, and a delete removes EVERY matching entry, not just the
first. The tool ports that resolution itself: the branch's
``remove_custom_provider_for_ui`` still carries the old lookup, and the fix is
not cherry-picked here by design.

List rows use the same slugging the fixed UI renders (legacy entries slug by
their ``custom:`` identity), so a row the list shows is always removable by
its slug.

Credential values are never rendered: results carry counts, labels and model
ids only. A key supplied to ``add`` / ``add_key`` / ``test`` is written to the
pool / ``.env`` and never echoed back.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from agent.credential_pool import CUSTOM_POOL_PREFIX
from tools.registry import registry

logger = logging.getLogger(__name__)

# Rows never carry full model catalogs: the list is an inventory, and the
# live catalog comes from ``test``.
_MAX_LISTED_MODELS = 25

PROVIDERS_ACTIONS = (
    "list", "add", "update", "modify", "remove_credential", "add_key",
    "set_default_model", "remove", "bulk_remove", "test", "move", "backup",
)

PROVIDERS_SCHEMA: dict[str, Any] = {
    "name": "providers",
    "description": (
        "Manage Hermes model providers. list (default): every provider — canonical "
        "catalog rows plus user-defined endpoints — with configured/enabled/current "
        "state, default model and credential counts (never key values). add: store a "
        "key for an existing provider (provider + api_key), or create a custom "
        "OpenAI-compatible endpoint (name + base_url + model, api_key optional; "
        "make_default also makes it the active route). update: re-save a custom "
        "endpoint's connection fields (name, base_url, model, api_mode, "
        "context_length, models, api_key — full row, like the settings panel). "
        "modify: patch fields on a stored user-defined entry (enabled, name, "
        "base_url, api_mode, context_length, default_model, models, "
        "discover_models). remove_credential: remove one credential from a "
        "provider (target = credential id, 1-based index or label). add_key: add "
        "another key to a provider (provider + api_key, label optional). "
        "set_default_model: change a provider's default model. remove: delete a "
        "custom provider entirely (config entries, pool credentials, key env; "
        "removes every stored copy). bulk_remove: delete several custom providers "
        "at once (providers = list of ids). test: live-probe a saved provider's "
        "/models endpoint and write discovered models back; with base_url + "
        "api_key and no provider it probes an endpoint before adding, persisting "
        "nothing. move: reorder a user-defined provider (position = 1-based index "
        "among user-defined providers). backup: back up the Hermes home "
        "(config.yaml, auth store, .env) to a zip."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(PROVIDERS_ACTIONS),
                "description": "Action to run; list when omitted.",
            },
            "provider": {
                "type": "string",
                "description": (
                    "Provider id: a canonical id (openrouter, anthropic, ...), a "
                    "custom:<key> slug, a bare stored key, or a legacy endpoint "
                    "name. add also accepts it as the new custom endpoint's id."
                ),
            },
            "providers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Provider ids for bulk_remove.",
            },
            "name": {
                "type": "string",
                "description": "Display name of a custom endpoint (add / update / modify).",
            },
            "base_url": {
                "type": "string",
                "description": "Endpoint base URL for a custom endpoint (add / update / modify), or the raw URL to probe (test).",
            },
            "model": {
                "type": "string",
                "description": "Default model for a custom endpoint (add / update), the new default model (set_default_model), or the model to probe with (test).",
            },
            "api_key": {
                "type": "string",
                "description": "API key to store (add / add_key) or to probe with (test). Never echoed back.",
            },
            "api_mode": {
                "type": "string",
                "enum": ["", "chat_completions", "codex_responses", "anthropic_messages"],
                "description": "Transport for a custom endpoint (add / update / modify / test). Empty = auto-detect at runtime.",
            },
            "context_length": {
                "type": "integer",
                "description": "Context length for a custom endpoint (add / update / modify).",
            },
            "discover_models": {
                "type": "boolean",
                "description": "Whether the endpoint's model list is probed live (add / update / modify).",
            },
            "make_default": {
                "type": "boolean",
                "description": "add / update: also make this endpoint the active model route.",
            },
            "label": {
                "type": "string",
                "description": "Credential label for add_key.",
            },
            "target": {
                "type": "string",
                "description": "Credential to remove for remove_credential: id, 1-based index, or label.",
            },
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Model ids for a custom endpoint (update / modify).",
            },
            "enabled": {
                "type": "boolean",
                "description": "Enable/disable a stored user-defined entry (modify).",
            },
            "position": {
                "type": "integer",
                "description": "1-based position among user-defined providers (move).",
            },
            "path": {
                "type": "string",
                "description": "Backup output path — a file or a directory (backup). Defaults to the backups folder under the Hermes home.",
            },
            "timeout": {
                "type": "number",
                "description": "Probe timeout in seconds for test (default 8).",
            },
        },
        "required": [],
    },
}


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, default=str)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": str(message), **fields}, default=str)


# --- Resolution: the honey-qwen lesson -------------------------------------
# Port of the provider-delete fix's approach (main, ``remove_custom_provider_for_ui``):
# resolve against the RAW config, slug-compared, ``custom:`` prefix stripped,
# every matching entry — never through the compatible view, which drops
# disabled / base_url-less entries that still render as UI rows.

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _endpoint_slug(raw: str, fallback: str = "custom") -> str:
    """Slug an endpoint id the way the web custom-endpoint routes do."""
    from hermes_cli.config import coerce_provider_id

    slug = _SLUG_RE.sub("-", coerce_provider_id(raw)).strip("-_").lower()
    return slug or fallback


def _match_key(value: str) -> str:
    """Slug an endpoint identity for resolution: the ``custom:`` prefix is
    spelling, not identity, so it is stripped before slugging — a stored key
    ``Custom:Honey-Qwen`` and the UI slug ``custom:honey-qwen`` are the same
    endpoint, but slugging the raw key would turn the colon into a dash
    (``custom-honey-qwen``) and never match."""
    lowered = (value or "").strip().lower().removeprefix(CUSTOM_POOL_PREFIX)
    return _endpoint_slug(lowered)


def _canonical_slugs() -> set[str]:
    from hermes_cli.models import CANONICAL_PROVIDERS

    # "custom" itself is exempt: it is the bare ``model.provider: custom``
    # route, a user endpoint, not a canonical provider.
    return {str(entry.slug or "").casefold() for entry in CANONICAL_PROVIDERS} - {"custom"}


def _pool_keys_for(normalized: str) -> list[str]:
    """Both spellings of a provider id: the UI slug (``custom:<key>``) and the
    bare key its credentials live under."""
    keys = {normalized}
    if normalized.startswith(CUSTOM_POOL_PREFIX):
        keys.add(normalized.removeprefix(CUSTOM_POOL_PREFIX))
    else:
        keys.add(f"{CUSTOM_POOL_PREFIX}{normalized}")
    return sorted(keys)


def _resolve_stored(cfg: dict[str, Any], raw: str) -> dict[str, Any]:
    """Resolve *raw* against the raw config.

    ``matches`` holds every stored entry for the id — keyed ``providers:``
    entries (by stored key or entry name) plus legacy ``custom_providers:``
    entries (by name) — so a delete removes all of them. ``kind`` is
    ``keyed`` / ``legacy`` / ``canonical`` / ``none``.
    """
    from hermes_cli.auth_commands import _normalize_provider
    from hermes_cli.config import coerce_provider_id

    normalized = _normalize_provider(raw)
    if not normalized:
        return {"kind": "none", "matches": [], "slug": ""}
    bare = normalized.removeprefix(CUSTOM_POOL_PREFIX)
    target = _match_key(normalized)
    matches: list[dict[str, Any]] = []
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        for stored_key in providers:
            entry = providers.get(stored_key)
            if not isinstance(entry, dict):
                continue
            if (_match_key(coerce_provider_id(stored_key)) == target
                    or _match_key(str(entry.get("name") or "")) == target):
                matches.append({"store": "providers", "key": stored_key, "entry": entry})
    legacy = cfg.get("custom_providers")
    if isinstance(legacy, list):
        for index, entry in enumerate(legacy):
            if isinstance(entry, dict) and _match_key(str(entry.get("name") or "")) == target:
                matches.append({"store": "custom_providers", "key": index, "entry": entry})
    if matches:
        kind = "keyed" if matches[0]["store"] == "providers" else "legacy"
        return {"kind": kind, "matches": matches, "slug": normalized}
    if bare in _canonical_slugs():
        return {"kind": "canonical", "matches": [], "slug": normalized}
    return {"kind": "none", "matches": [], "slug": normalized}


def _load_cfg() -> dict[str, Any]:
    from hermes_cli.config import load_config

    cfg = load_config()
    return cfg if isinstance(cfg, dict) else {}


def _model_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    model_cfg = cfg.get("model")
    return model_cfg if isinstance(model_cfg, dict) else {}


def _current_route(cfg: dict[str, Any]) -> tuple[str, str, str]:
    model_cfg = _model_cfg(cfg)
    provider = str(model_cfg.get("provider") or "openrouter").strip().lower()
    model = str(model_cfg.get("default") or model_cfg.get("name") or "")
    base_url = str(model_cfg.get("base_url") or "")
    return provider, model, base_url


def _credential_count(normalized: str) -> int:
    """Pool entries under both spellings of a provider id (display-safe count)."""
    from agent.credential_pool import load_pool

    seen: set[str] = set()
    for pool_key in _pool_keys_for(normalized):
        try:
            pool = load_pool(pool_key)
        except Exception:
            logger.debug("providers list: pool %s unreadable", pool_key, exc_info=True)
            continue
        for entry in pool.entries():
            seen.add(entry.id)
    return len(seen)


def _is_current_row(slug: str, row: dict[str, Any], current_provider: str, current_base_url: str) -> bool:
    if _match_key(current_provider) == _match_key(slug):
        return True
    # Bare ``model.provider: custom`` names no endpoint: the row whose base_url
    # the route points at is current.
    if current_provider == "custom" and current_base_url:
        base_url = str(row.get("base_url") or "").strip().rstrip("/")
        return bool(base_url) and base_url == current_base_url.strip().rstrip("/")
    return False


# --- list ------------------------------------------------------------------

def _model_ids(entry: dict[str, Any]) -> tuple[list[str], int]:
    models = entry.get("models")
    if isinstance(models, dict):
        ids = [str(m) for m in models if str(m).strip()]
    elif isinstance(models, list):
        ids = [str(m.get("id") or m) for m in models if isinstance(m, (dict, str)) and str(m.get("id") if isinstance(m, dict) else m).strip()]
    else:
        ids = []
    return ids[:_MAX_LISTED_MODELS], len(ids)


def _list_providers() -> str:
    """Every provider row the UI renders: catalog + registry + raw config."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.config import get_compatible_custom_providers, stringify_provider_map
    from hermes_cli.provider_catalog import provider_catalog
    from hermes_cli.providers import custom_provider_slug

    cfg = _load_cfg()
    current_provider, current_model, current_base_url = _current_route(cfg)
    by_slug: dict[str, dict[str, Any]] = {}

    def _row(slug: str, name: str) -> dict[str, Any]:
        return {
            "slug": slug, "name": name, "is_user_defined": False,
            "is_catalog_provider": False, "configured": False, "enabled": True,
            "is_current": False, "default_model": "", "base_url": "",
            "models": [], "total_models": 0, "credential_count": 0,
        }

    for descriptor in provider_catalog():
        slug = str(descriptor.slug or "").strip().lower()
        if not slug:
            continue
        row = _row(slug, str(descriptor.label or slug))
        row.update({
            "is_catalog_provider": True,
            "description": str(descriptor.description or ""),
            "auth_type": str(descriptor.auth_type or "api_key"),
            "signup_url": str(descriptor.signup_url or ""),
        })
        by_slug[slug] = row
    for provider in PROVIDER_REGISTRY.values():
        slug = str(getattr(provider, "id", "") or "").strip().lower()
        if slug:
            by_slug.setdefault(slug, _row(slug, str(getattr(provider, "name", "") or slug)))

    # Raw ``providers:`` map rows: slug is the lowercased stored key — every
    # entry renders, including ones the compatible view drops.
    user_providers = stringify_provider_map(cfg.get("providers"))
    for slug, entry in user_providers.items():
        if not isinstance(entry, dict):
            continue
        normalized = str(slug).strip().lower()
        if not normalized:
            continue
        row = by_slug.setdefault(normalized, _row(normalized, str(entry.get("name") or normalized)))
        row["name"] = str(entry.get("name") or row["name"])
        row["enabled"] = bool(entry.get("enabled", True))
        row["is_user_defined"] = True
        row["configured"] = True
        row["base_url"] = str(entry.get("base_url") or entry.get("url") or entry.get("api") or "")
        default_model = str(entry.get("default_model") or entry.get("model") or "").strip()
        if default_model:
            row["default_model"] = default_model
        models, total = _model_ids(entry)
        row["models"] = models
        row["total_models"] = total

    # Legacy ``custom_providers:`` rows the compatible view keeps: slug by the
    # ``custom:`` identity (the fixed UI slugging), so list and remove agree.
    for custom in get_compatible_custom_providers(cfg):
        if not isinstance(custom, dict):
            continue
        slug = (str(custom.get("provider_key") or "").strip().lower()
                or custom_provider_slug(str(custom.get("name") or "custom"), ""))
        if not slug:
            continue
        row = by_slug.setdefault(slug, _row(slug, str(custom.get("name") or slug)))
        row["configured"] = True
        row["is_user_defined"] = True
        custom_model = str(custom.get("model") or custom.get("default_model") or "").strip()
        if custom_model:
            row["default_model"] = custom_model
        if not row.get("base_url"):
            row["base_url"] = str(custom.get("base_url") or "")
        if not row.get("models"):
            models, total = _model_ids(custom)
            row["models"] = models
            row["total_models"] = total

    for slug, row in by_slug.items():
        row["credential_count"] = _credential_count(slug)
        if row["credential_count"]:
            row["configured"] = True
        row["is_current"] = _is_current_row(slug, row, current_provider, current_base_url)

    return _ok(
        action="list",
        current={"provider": current_provider, "model": current_model, "base_url": current_base_url},
        providers=list(by_slug.values()),
    )


# --- add / update ----------------------------------------------------------

def _validate_custom_fields(name: str, base_url: str, model: str) -> Optional[str]:
    if not name:
        return "adding a custom endpoint needs a name"
    if not base_url:
        return "adding a custom endpoint needs a base_url"
    if not model:
        return "adding a custom endpoint needs a model"
    return None


def _upsert_custom_endpoint(args: dict[str, Any], *, require_existing: bool) -> dict[str, Any]:
    """Create or re-save a custom endpoint through the web write path."""
    from hermes_cli.config import load_config, save_config
    from hermes_cli.web_models import CustomEndpointUpdate
    from hermes_cli.web_routers.config_env import (
        _CONFIG_MUTATION_LOCK,
        _write_custom_endpoint,
    )

    name = (args.get("name") or args.get("provider") or "").strip()
    base_url = (args.get("base_url") or "").strip()
    model = (args.get("model") or "").strip()
    if (error := _validate_custom_fields(name, base_url, model)) is not None:
        raise ValueError(error)
    try:
        body = CustomEndpointUpdate(
            id=(args.get("provider") or "").strip(),
            name=name, base_url=base_url, model=model,
            api_key=(args.get("api_key") or "").strip() or None,
            api_mode=args.get("api_mode"),
            context_length=args.get("context_length"),
            discover_models=bool(args.get("discover_models", True)),
            make_default=bool(args.get("make_default", False)),
            models=args.get("models"),
        )
    except Exception as exc:
        raise ValueError(f"invalid custom endpoint: {exc}") from exc
    with _CONFIG_MUTATION_LOCK:
        cfg = load_config()
        cfg = cfg if isinstance(cfg, dict) else {}
        created = not _resolve_stored(cfg, body.id or body.name)["matches"]
        if require_existing and created:
            raise ValueError(f"custom provider not found: {body.id or body.name}")
        try:
            endpoint_id, entry = _write_custom_endpoint(cfg, body)
        except Exception as exc:
            # The web path raises HTTPException(400) for bad rows; the tool
            # surface reports the reason, not the exception type.
            detail = getattr(exc, "detail", None) or str(exc)
            raise ValueError(str(detail)) from exc
        save_config(cfg)
    return {
        "provider": endpoint_id,
        "name": str(entry.get("name") or endpoint_id),
        "base_url": str(entry.get("base_url") or ""),
        "model": str(entry.get("model") or entry.get("default_model") or ""),
        "created": created,
        "make_default": bool(args.get("make_default", False)),
    }


def _add_provider(args: dict[str, Any]) -> str:
    provider = (args.get("provider") or "").strip()
    api_key = (args.get("api_key") or "").strip()
    # No endpoint fields: store a key on an existing provider.
    if provider and not args.get("name") and not args.get("base_url") and not args.get("model"):
        if not api_key:
            raise ValueError(
                "api_key is required to add a credential to a provider; "
                "to create a custom endpoint pass name, base_url and model")
        from hermes_cli.auth_commands import add_api_key_credential_for_ui

        result = add_api_key_credential_for_ui(
            provider, api_key, label=(args.get("label") or None))
        return _ok(action="add", provider=result["provider"],
                   credential_id=result["id"], credential_count=result["count"])
    result = _upsert_custom_endpoint(args, require_existing=False)
    return _ok(action="add", **result)


def _update_provider(args: dict[str, Any]) -> str:
    # Full re-save of a custom endpoint's connection fields, like the settings
    # panel's POST; partial patches belong to modify.
    result = _upsert_custom_endpoint(args, require_existing=True)
    return _ok(action="update", **result)


# --- modify ----------------------------------------------------------------

def _modify_provider(args: dict[str, Any]) -> str:
    from hermes_cli.config import save_config

    provider = (args.get("provider") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    patch: dict[str, Any] = {}
    for field in ("name", "base_url", "api_mode", "context_length", "default_model", "discover_models"):
        if args.get(field) is not None:
            patch[field] = args[field]
    if args.get("enabled") is not None:
        patch["enabled"] = bool(args["enabled"])
    if args.get("models") is not None:
        models = [str(m).strip() for m in args["models"] if str(m).strip()]
        patch["models"] = {model_id: {} for model_id in models}
    if not patch:
        raise ValueError("nothing to change: pass a field to patch")
    if "base_url" in patch:
        from urllib.parse import urlparse

        parsed = urlparse(str(patch["base_url"]).strip())
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("base_url must include scheme and host")

    cfg = _load_cfg()
    resolved = _resolve_stored(cfg, provider)
    if not resolved["matches"]:
        if resolved["kind"] == "canonical":
            raise ValueError(
                "canonical providers have no stored entry to patch — add a key "
                "with add_key, or change the active model with set_default_model")
        raise ValueError(f"provider not found: {provider}")
    current_provider, _current_model, _current_base_url = _current_route(cfg)
    route_updated = False
    for match in resolved["matches"]:
        entry = match["entry"]
        old_default = str(entry.get("default_model") or entry.get("model") or "")
        for key, value in patch.items():
            entry[key] = value
        if "default_model" in patch:
            # Keep the entry's two default spellings in sync, and the live
            # route when this entry is the one the route points at.
            new_default = str(patch["default_model"]).strip()
            if not entry.get("model") or str(entry.get("model")) == old_default:
                entry["model"] = new_default
            models_map = entry.get("models")
            if isinstance(models_map, dict):
                models_map.setdefault(new_default, {})
            if _is_current_row(resolved["slug"], {"base_url": entry.get("base_url")},
                               current_provider, _current_base_url):
                model_cfg = _model_cfg(cfg)
                if "default" in model_cfg:
                    model_cfg["default"] = new_default
                elif "name" in model_cfg:
                    model_cfg["name"] = new_default
                route_updated = True
    save_config(cfg)
    return _ok(action="modify", provider=resolved["slug"], kind=resolved["kind"],
               changed=sorted(patch), route_updated=route_updated,
               entries_changed=len(resolved["matches"]))


# --- credentials -----------------------------------------------------------

def _remove_credential(args: dict[str, Any]) -> str:
    from hermes_cli.auth_commands import remove_credential_for_ui

    provider = (args.get("provider") or "").strip()
    target = (args.get("target") or "").strip()
    if not provider or not target:
        raise ValueError("provider and target are required")
    result = remove_credential_for_ui(provider, target)
    return _ok(action="remove_credential", provider=result["provider"],
               credential_id=result["id"], index=result["index"],
               remaining=_credential_count(result["provider"]))


def _add_key(args: dict[str, Any]) -> str:
    from hermes_cli.auth_commands import add_api_key_credential_for_ui

    provider = (args.get("provider") or "").strip()
    api_key = (args.get("api_key") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    if not api_key:
        raise ValueError("api_key is required")
    result = add_api_key_credential_for_ui(
        provider, api_key, label=(args.get("label") or None))
    return _ok(action="add_key", provider=result["provider"],
               credential_id=result["id"], credential_count=result["count"])


# --- default model --------------------------------------------------------

def _set_default_model(args: dict[str, Any]) -> str:
    from hermes_cli.config import save_config

    provider = (args.get("provider") or "").strip()
    model = (args.get("model") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    if not model:
        raise ValueError("model is required")
    cfg = _load_cfg()
    resolved = _resolve_stored(cfg, provider)
    if resolved["matches"]:
        current_provider, _current_model, current_base_url = _current_route(cfg)
        route_updated = False
        for match in resolved["matches"]:
            entry = match["entry"]
            old_default = str(entry.get("default_model") or entry.get("model") or "")
            entry["default_model"] = model
            if not entry.get("model") or str(entry.get("model")) == old_default:
                entry["model"] = model
            models_map = entry.get("models")
            if isinstance(models_map, dict):
                models_map.setdefault(model, {})
            if _is_current_row(resolved["slug"], {"base_url": entry.get("base_url")},
                               current_provider, current_base_url):
                model_cfg = _model_cfg(cfg)
                if "default" in model_cfg:
                    model_cfg["default"] = model
                elif "name" in model_cfg:
                    model_cfg["name"] = model
                route_updated = True
        save_config(cfg)
        return _ok(action="set_default_model", provider=resolved["slug"],
                   kind=resolved["kind"], model=model, route_updated=route_updated,
                   entries_changed=len(resolved["matches"]))
    if resolved["kind"] == "canonical":
        slug = resolved["slug"].removeprefix(CUSTOM_POOL_PREFIX)
        current_provider, current_model, current_base_url = _current_route(cfg)
        if _match_key(current_provider) != _match_key(slug):
            raise ValueError(
                f"{slug} is not the active route — switch to it first, then "
                "change its default model")
        # Canonical providers keep no stored default: the default model IS the
        # active route's model, so change it through the real switch pipeline
        # (catalog validation included) instead of writing the route by hand.
        from hermes_cli.config import get_compatible_custom_providers, stringify_provider_map
        from hermes_cli.model_switch import persist_model_selection, switch_model

        result = switch_model(
            model, current_provider=current_provider, current_model=current_model,
            current_base_url=current_base_url, explicit_provider=slug,
            user_providers=stringify_provider_map(cfg.get("providers")),
            custom_providers=get_compatible_custom_providers(cfg))
        if not result.success:
            raise ValueError(result.error_message or f"could not switch to {model}")
        persist_model_selection(result)
        return _ok(action="set_default_model", provider=slug, kind="canonical",
                   model=result.new_model, route_updated=True)
    raise ValueError(f"provider not found: {provider}")


# --- remove (ported provider-delete fix) -----------------------------------

def _remove_pool_credentials(normalized: str) -> int:
    """Drop every pooled credential under both spellings of a provider id,
    running the credential sources' removal steps (external state, source
    suppression) the way the credential-pool removal path does."""
    import hermes_cli.auth as auth_mod
    from agent.credential_pool import load_pool
    from agent.credential_sources import find_removal_step, remove_provider_external_state

    removed = 0
    for pool_key in _pool_keys_for(normalized):
        pool = load_pool(pool_key)
        while pool.entries():
            entry = pool.remove_index(1)
            if entry is None:
                break
            removed += 1
            step = find_removal_step(pool_key, entry.source)
            if step is not None:
                result = step.remove_fn(pool_key, entry)
                if result.suppress:
                    auth_mod.suppress_credential_source(pool_key, entry.source)
            if not pool.entries():
                remove_provider_external_state(pool_key, entry)
    return removed


def _remove_custom_provider(raw: str) -> dict[str, Any]:
    """Delete a user-defined provider endpoint: config entries, pool entries,
    key env, and the main-route mirror when it names the provider.

    This ports the provider-delete fix's resolution (main,
    ``remove_custom_provider_for_ui``) instead of calling the branch's copy,
    which still resolves through the compatible view: the id resolves against
    the RAW config, slug-compared with the ``custom:`` prefix stripped, and
    EVERY matching entry is removed — a mixed-case stored key, a disabled or
    base_url-less entry, and an endpoint stored twice (legacy list + keyed map)
    all go, so the provider cannot come back after the delete.
    """
    from hermes_cli.auth_commands import _normalize_provider
    from hermes_cli.config import (
        custom_endpoint_key_env, load_config, remove_env_value, save_config,
    )

    normalized = _normalize_provider(raw)
    if not normalized:
        raise ValueError("Custom provider not found")
    bare = normalized.removeprefix(CUSTOM_POOL_PREFIX)
    # Canonical providers have no config row to remove; "custom" itself is
    # exempt — it is the bare-``model.provider: custom`` route, a user endpoint.
    if bare in _canonical_slugs():
        raise ValueError("Only custom providers can be deleted")

    removed_credentials = _remove_pool_credentials(normalized)

    cfg = load_config()
    cfg = cfg if isinstance(cfg, dict) else {}
    removed_config = False
    provider_keys: list[str] = []
    removed_entries: list[tuple[str, Optional[dict[str, Any]]]] = []
    route_detached = False
    # Resolve against the raw config, not the compatible view: the UI renders
    # a row for every entry — including ones the view drops (disabled, no
    # base_url) and ones stored under a mixed-case key the UI slug lowercases
    # — and one endpoint stored twice (legacy list + keyed map) renders two
    # rows for one provider, so every matching entry goes, not just the first.
    target = _match_key(normalized)
    providers = cfg.get("providers")
    if isinstance(providers, dict):
        from hermes_cli.config import coerce_provider_id

        for stored_key in list(providers):
            stored_entry = providers.get(stored_key)
            if not isinstance(stored_entry, dict):
                continue
            if (_match_key(coerce_provider_id(stored_key)) == target
                    or _match_key(str(stored_entry.get("name") or "")) == target):
                providers.pop(stored_key, None)
                cfg["providers"] = providers
                removed_config = True
                provider_keys.append(coerce_provider_id(stored_key))
                removed_entries.append((coerce_provider_id(stored_key), stored_entry))
    legacy = cfg.get("custom_providers")
    if isinstance(legacy, list):
        kept = []
        for legacy_entry in legacy:
            if (isinstance(legacy_entry, dict)
                    and _match_key(str(legacy_entry.get("name") or "")) == target):
                removed_entries.append((target, legacy_entry))
            else:
                kept.append(legacy_entry)
        if len(kept) != len(legacy):
            cfg["custom_providers"] = kept
            removed_config = True
            provider_keys.append(target)
    if removed_config:
        for key in dict.fromkeys(provider_keys):
            remove_env_value(custom_endpoint_key_env(key))
        # Drop the main-slot mirror when it names the deleted provider, or
        # the agent keeps authenticating to the deleted host with the deleted
        # key (web DELETE's ``_detach_main_model_from_provider``). The entry
        # rides along so a ``custom:<name>`` route spelling detaches too.
        from hermes_cli.web_routers.config_env import _detach_main_model_from_provider

        model_cfg = _model_cfg(cfg)
        for key, entry in removed_entries:
            before = dict(model_cfg)
            _detach_main_model_from_provider(cfg, key, entry)
            if _model_cfg(cfg) != before:
                route_detached = True
        save_config(cfg)
    if not removed_config and removed_credentials == 0:
        raise ValueError("Custom provider not found")
    return {
        "provider": normalized,
        "provider_key": provider_keys[0] if provider_keys else "",
        "removed_config": removed_config,
        "removed_credentials": removed_credentials,
        "route_detached": route_detached,
    }


def _remove_provider(args: dict[str, Any]) -> str:
    provider = (args.get("provider") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    return _ok(action="remove", **_remove_custom_provider(provider))


def _bulk_remove(args: dict[str, Any]) -> str:
    providers = args.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ValueError("providers is required: a non-empty list of provider ids")
    results: list[dict[str, Any]] = []
    removed = 0
    for raw in providers:
        slug = str(raw or "").strip()
        if not slug:
            results.append({"provider": str(raw), "removed": False, "error": "empty provider id"})
            continue
        try:
            result = _remove_custom_provider(slug)
            result["removed"] = True
            removed += 1
        except ValueError as exc:
            result = {"provider": slug, "removed": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("providers bulk_remove %s failed", slug, exc_info=True)
            result = {"provider": slug, "removed": False, "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
    return _ok(action="bulk_remove", removed=removed, attempted=len(providers), results=results)


# --- test ------------------------------------------------------------------

def _resolve_provider_key(slug: str, entry: Optional[dict[str, Any]]) -> tuple[Optional[str], bool]:
    """A usable key for a probe, without ever rendering it: entry ``key_env``
    via the secret scope, then the credential pool (both spellings), then a
    legacy inline ``api_key`` on the entry."""
    entry = entry or {}
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret_str

        value = get_secret_str(key_env, "")
        if value:
            return value, True
    from agent.credential_pool import load_pool

    for pool_key in _pool_keys_for(slug):
        try:
            pool = load_pool(pool_key)
        except Exception:
            logger.debug("providers test: pool %s unreadable", pool_key, exc_info=True)
            continue
        for credential in pool.entries():
            if credential.access_token:
                return credential.access_token, True
    inline = str(entry.get("api_key") or "").strip()
    if inline.startswith("${") and inline.endswith("}"):
        from agent.secret_scope import get_secret_str

        value = get_secret_str(inline[2:-1], "")
        if value:
            return value, True
    elif inline:
        return inline, True
    return None, False


def _canonical_base_url(slug: str) -> str:
    from hermes_cli.auth import PROVIDER_REGISTRY

    registry_entry = PROVIDER_REGISTRY.get(slug)
    if registry_entry is not None:
        url = str(getattr(registry_entry, "inference_base_url", "") or "").strip()
        if url:
            return url
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(slug)
        url = str(getattr(profile, "base_url", "") or "").strip()
        if url:
            return url
    except Exception:
        logger.debug("providers test: no provider profile for %s", slug, exc_info=True)
    return ""


def _write_back_models(cfg: dict[str, Any], resolved: dict[str, Any], model_ids: list[str]) -> bool:
    """Persist a successful ``/models`` probe into the matching stored
    entries — keyed ``providers:`` entries as well as legacy ones — under the
    same staleness guard the picker write-back uses, preserving per-model
    metadata already on a discovered row. User-curated mappings are never
    replaced."""
    from hermes_cli.config import save_config
    from hermes_cli.model_switch_providers import _discovered_catalog_stale

    changed = False
    for match in resolved["matches"]:
        entry = match["entry"]
        if not _discovered_catalog_stale(entry, list(model_ids)):
            continue
        existing = entry.get("models")
        preserved = existing if isinstance(existing, dict) else {}
        entry["models"] = {
            model_id: (preserved.get(model_id) if isinstance(preserved.get(model_id), dict) else {})
            for model_id in model_ids
        }
        entry["models_discovered"] = True
        changed = True
    if changed:
        save_config(cfg)
    return changed


def _probe_report(result: dict[str, Any], *, provider: str, key_used: bool) -> dict[str, Any]:
    models = result.get("models")
    reachable = models is not None
    listed = [str(m) for m in (models or []) if str(m).strip()]
    if not reachable:
        message = "no model list answered (unreachable, or the key was rejected)"
    elif not listed:
        message = "endpoint answered but advertised no models"
    else:
        message = ""
    return {
        "provider": provider,
        "reachable": reachable,
        "models": listed[:_MAX_LISTED_MODELS],
        "total_models": len(listed),
        "probed_url": result.get("probed_url") or "",
        "resolved_base_url": result.get("resolved_base_url") or "",
        "key_used": key_used,
        "message": message,
    }


def _test_provider(args: dict[str, Any]) -> str:
    from hermes_cli.models import probe_api_models

    timeout = args.get("timeout")
    try:
        timeout_s = float(timeout) if timeout is not None else 8.0
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number of seconds") from exc
    if timeout_s <= 0:
        raise ValueError("timeout must be positive")

    # Test-before-add: a raw endpoint, nothing persisted.
    raw_base_url = (args.get("base_url") or "").strip()
    if raw_base_url and not (args.get("provider") or "").strip():
        api_key = (args.get("api_key") or "").strip() or None
        result = probe_api_models(
            api_key, raw_base_url, timeout=timeout_s,
            api_mode=(args.get("api_mode") or None))
        report = _probe_report(result, provider="", key_used=bool(api_key))
        if result.get("models"):
            return _ok(action="test", persisted=False, **report)
        fields = {key: value for key, value in report.items() if key != "message"}
        return _err(report["message"], action="test", persisted=False, **fields)

    provider = (args.get("provider") or "").strip()
    if not provider:
        raise ValueError("provider or base_url is required")
    cfg = _load_cfg()
    resolved = _resolve_stored(cfg, provider)
    api_key: Optional[str] = None
    key_used = False
    if resolved["matches"]:
        entry = resolved["matches"][0]["entry"]
        base_url = str(entry.get("base_url") or entry.get("url") or entry.get("api") or "").strip()
        api_mode = str(entry.get("api_mode") or entry.get("transport") or "").strip() or None
        api_key, key_used = _resolve_provider_key(resolved["slug"], entry)
    elif resolved["kind"] == "canonical":
        slug = resolved["slug"].removeprefix(CUSTOM_POOL_PREFIX)
        base_url = _canonical_base_url(slug)
        api_mode = None
        api_key, key_used = _resolve_provider_key(resolved["slug"], None)
    else:
        raise ValueError(f"provider not found: {provider}")
    if not base_url:
        raise ValueError(f"provider {provider} has no base_url to probe")
    if not key_used:
        logger.debug("providers test %s: probing without a stored key", provider)
    result = probe_api_models(api_key, base_url, timeout=timeout_s, api_mode=api_mode)
    report = _probe_report(result, provider=resolved["slug"], key_used=key_used)
    models = result.get("models")
    if not models:
        fields = {key: value for key, value in report.items() if key != "message"}
        return _err(report["message"], action="test", persisted=True,
                    models_written_back=False, **fields)
    written_back = _write_back_models(cfg, resolved, [str(m) for m in models if str(m).strip()])
    return _ok(action="test", persisted=True, models_written_back=written_back, **report)


# --- move / backup ---------------------------------------------------------

def _move_provider(args: dict[str, Any]) -> str:
    from hermes_cli.config import save_config

    provider = (args.get("provider") or "").strip()
    position = args.get("position")
    if not provider:
        raise ValueError("provider is required")
    if not isinstance(position, int) or isinstance(position, bool) or position < 1:
        raise ValueError("position is required: a 1-based index among user-defined providers")
    cfg = _load_cfg()
    resolved = _resolve_stored(cfg, provider)
    if not resolved["matches"]:
        if resolved["kind"] == "canonical":
            raise ValueError("canonical providers have no stored order to move")
        raise ValueError(f"provider not found: {provider}")
    match = resolved["matches"][0]
    if match["store"] == "providers":
        providers = cfg.get("providers")
        keys = list(providers)
        keys.remove(match["key"])
        pos = min(position, len(keys) + 1) - 1
        keys.insert(pos, match["key"])
        order = [str(key) for key in keys]
        if [str(key) for key in providers] == order:
            return _ok(action="move", provider=resolved["slug"], position=pos + 1, order=order)
        # The comment-preserving writer merges onto the on-disk document and
        # keeps existing key order, so a reordered map cannot land in one
        # save: drop the moved key and everything after its target slot,
        # then re-add them in order — dropped keys append in source order.
        # Both saves run back-to-back under the config mutation lock; the
        # order is verified against the file afterwards, never assumed.
        from hermes_cli.web_routers.config_env import _CONFIG_MUTATION_LOCK

        prefix = {key: providers[key] for key in keys[:pos]}
        full = {key: providers[key] for key in keys}
        with _CONFIG_MUTATION_LOCK:
            cfg["providers"] = prefix
            save_config(cfg)
            cfg["providers"] = full
            save_config(cfg)
        on_disk = _providers_key_order()
        if on_disk != order:
            return _err(
                f"reorder did not land on disk (on-disk order is {on_disk})",
                action="move", provider=resolved["slug"], position=pos + 1, order=on_disk)
        return _ok(action="move", provider=resolved["slug"], position=pos + 1, order=order)
    legacy = cfg.get("custom_providers")
    entry = legacy.pop(match["key"])
    pos = min(position, len(legacy) + 1) - 1
    legacy.insert(pos, entry)
    cfg["custom_providers"] = legacy
    order = [str(item.get("name") or "") for item in legacy if isinstance(item, dict)]
    save_config(cfg)
    return _ok(action="move", provider=resolved["slug"], position=pos + 1, order=order)


def _providers_key_order() -> list[str]:
    """Stored-key order as written in config.yaml (the order the UI renders),
    re-read through the config system after the save — the writer produces a
    fresh inode, so the signature cache invalidates and this sees the file
    the move actually landed, not the in-memory dict."""
    from hermes_cli.config import read_raw_config

    try:
        raw = read_raw_config()
    except Exception:
        logger.debug("providers move: raw config re-read failed", exc_info=True)
        return []
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return []
    return [str(key) for key in providers]


def _backup_filename() -> str:
    return f"hermes-backup-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.zip"


def _backup_providers(args: dict[str, Any]) -> str:
    from hermes_cli.backup import run_backup
    from hermes_constants import get_hermes_home

    out = (args.get("path") or "").strip()
    if out:
        out_path = Path(out).expanduser()
        if out_path.is_dir() or not out_path.suffix:
            out_path.mkdir(parents=True, exist_ok=True)
            out_path = out_path / _backup_filename()
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if out_path.suffix.lower() != ".zip":
                out_path = out_path.with_suffix(out_path.suffix + ".zip")
    else:
        backups_dir = Path(get_hermes_home()) / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        out_path = backups_dir / _backup_filename()
    try:
        complete = run_backup(SimpleNamespace(output=str(out_path)))
    except SystemExit as exc:
        raise ValueError(f"backup failed (exit {exc.code})") from exc
    except Exception as exc:
        raise ValueError(f"backup failed: {exc}") from exc
    if not complete:
        return _err("backup finished incomplete", path=str(out_path))
    size = out_path.stat().st_size if out_path.exists() else 0
    return _ok(action="backup", path=str(out_path), size_bytes=size, complete=True)


# --- dispatch --------------------------------------------------------------

def _handle_providers(args: dict[str, Any], **_kw: Any) -> str:
    action = (args.get("action") or "list").strip()
    handlers = {
        "list": lambda: _list_providers(),
        "add": lambda: _add_provider(args),
        "update": lambda: _update_provider(args),
        "modify": lambda: _modify_provider(args),
        "remove_credential": lambda: _remove_credential(args),
        "add_key": lambda: _add_key(args),
        "set_default_model": lambda: _set_default_model(args),
        "remove": lambda: _remove_provider(args),
        "bulk_remove": lambda: _bulk_remove(args),
        "test": lambda: _test_provider(args),
        "move": lambda: _move_provider(args),
        "backup": lambda: _backup_providers(args),
    }
    handler = handlers.get(action)
    if handler is None:
        return _err(f"unknown action: {action}", action=action)
    try:
        return handler()
    except ValueError as exc:
        # A failed provider action reports its real reason, not the bare
        # exception type (provider-delete fix).
        logger.warning("providers %s failed: %s", action, exc)
        return _err(str(exc) or action, action=action)
    except Exception as exc:
        logger.warning("providers %s failed", action, exc_info=True)
        return _err(f"{action} failed: {exc}", action=action)
    except BaseException as exc:
        # ConfigBackendUnavailable is a SystemExit: a non-file backend has no
        # config.yaml to write, so report it instead of dying.
        from hermes_cli.config_backend import ConfigBackendUnavailable

        if isinstance(exc, ConfigBackendUnavailable):
            logger.warning("providers %s failed: %s", action, exc)
            return _err(str(exc) or action, action=action)
        raise


registry.register(
    name="providers",
    toolset="providers",
    schema=PROVIDERS_SCHEMA,
    handler=_handle_providers,
    check_fn=None,
    requires_env=[],
    is_async=False,
    emoji="🔌")
