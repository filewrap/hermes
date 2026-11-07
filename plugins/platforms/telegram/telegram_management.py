"""Interactive Telegram model, provider, and dashboard menus.

Provider credentials are accepted only in the requesting user's private chat. The inbound
credential message must be deletable before it is persisted; values are never echoed or logged.
"""
from __future__ import annotations

import asyncio
from html import escape
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .mtproto_rich import edit_request, inline_markup

logger = logging.getLogger(__name__)
_STATE_TTL_SECONDS = 30 * 60
_KEY_TTL_SECONDS = 10 * 60
_PAGE_SIZE = 8
_MODEL_PAGE_SIZE = 8


def _clean_label(value: Any, limit: int = 48) -> str:
    import re
    text = " ".join(str(value or "").split())
    return re.sub(r"[`*_\[\]<>]", "", text)[:limit]


def _is_safe_endpoint_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        _ = parsed.port
        if (len(value) > 2048 or parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(char.isspace() for char in value)):
            return False
        if parsed.scheme == "http":
            try:
                from agent.model_metadata import is_local_endpoint
                return bool(is_local_endpoint(value))
            except Exception:
                return parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        return True
    except (TypeError, ValueError):
        return False


def _state_store(adapter: Any) -> dict[str, dict[str, Any]]:
    state = getattr(adapter, "_telegram_management_state", None)
    if not isinstance(state, dict):
        state = {}
        adapter._telegram_management_state = state
    now = time.monotonic()
    for token, item in list(state.items()):
        if now - float(item.get("created", 0)) > _STATE_TTL_SECONDS:
            state.pop(token, None)
    return state


def _new_state(adapter: Any, source: Any, **payload: Any) -> str:
    state = _state_store(adapter)
    while len(state) >= 40:
        oldest = min(state, key=lambda token: state[token].get("created", 0))
        state.pop(oldest, None)
    token = secrets.token_urlsafe(6)
    state[token] = {
        "created": time.monotonic(), "chat_id": str(source.chat_id),
        "user_id": str(getattr(source, "user_id", "") or ""), **payload,
    }
    return token


def _get_state(adapter: Any, source: Any, token: str) -> dict[str, Any] | None:
    item = _state_store(adapter).get(token)
    if not item:
        return None
    if (str(item.get("chat_id")) != str(source.chat_id)
            or str(item.get("user_id")) != str(getattr(source, "user_id", "") or "")):
        return None
    if time.monotonic() - float(item.get("created", 0)) > _STATE_TTL_SECONDS:
        _state_store(adapter).pop(token, None)
        return None
    return item


def _is_explicit_admin(adapter: Any, source: Any) -> bool:
    user_id = str(getattr(source, "user_id", "") or "")
    if callable(getattr(adapter, "_is_owner_user", None)) and adapter._is_owner_user(user_id):
        return True
    try:
        return bool(adapter._is_explicitly_allowed(source))
    except Exception:
        return False


def _slash_denial(adapter: Any, source: Any, command: str) -> str | None:
    runner = getattr(adapter, "gateway_runner", None)
    checker = getattr(runner, "_check_slash_access", None)
    if not callable(checker):
        return None
    try:
        return checker(source, command)
    except Exception:
        logger.warning("Telegram slash access check failed closed for /%s", command, exc_info=True)
        return f"⛔ /{command} is unavailable because its access check failed."


@asynccontextmanager
async def _profile_scope(adapter: Any, source: Any):
    """Bind config and credentials to the profile that owns this Telegram conversation."""
    home = None
    resolver = getattr(getattr(adapter, "gateway_runner", None), "_resolve_profile_home_for_source", None)
    if callable(resolver):
        try:
            home = resolver(source)
        except Exception:
            home = None
    if home is None:
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"
    try:
        from gateway.run import _async_profile_runtime_scope
    except Exception:
        yield Path(home)
        return
    async with _async_profile_runtime_scope(Path(home)):
        yield Path(home)


async def _send(adapter: Any, source: Any, text: str, markup: Any = None,
                *, reply_to: int | None = None) -> None:
    await adapter._send_rich_ui(source, text, markup, reply_to=reply_to)


async def _edit(adapter: Any, event: Any, source: Any, text: str, markup: Any = None) -> None:
    helper = getattr(adapter, "_edit_rich_ui_message", None)
    if callable(helper):
        await helper(event, source, text, markup)
        return
    try:
        await adapter._client(edit_request(
            adapter._peer_for(source.chat_id), int(getattr(event, "message_id", 0) or 0),
            text, reply_markup=markup,
        ))
    except Exception:
        logger.debug("Telegram menu edit failed; sending a replacement", exc_info=True)
        await _send(adapter, source, text, markup)


def _dashboard_public_url() -> str:
    try:
        from hermes_cli.dashboard_auth.prefix import resolve_public_url
        raw = str(resolve_public_url() or "").strip()
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return ""
        return raw.rstrip("/")
    except Exception:
        return ""


def _dashboard_page_url(base: str, path: str, fragment: str = "") -> str:
    """Join a dashboard route while preserving any configured reverse-proxy prefix."""
    try:
        parsed = urlsplit(base)
        prefix = parsed.path.rstrip("/")
        route = prefix + "/" + str(path or "").lstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, route, "", fragment))
    except Exception:
        return ""


def _url_markup(label: str, url: str, *, back_callback: str = "") -> Any:
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        from telethon import Button, types
        rows = [[Button.url(label, url)]]
        if back_callback:
            rows.append([Button.inline("‹ Provider", back_callback.encode("utf-8"))])
        rows.append([Button.inline("Close", b"hm:management:close")])
        return types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=row) for row in rows])
    except Exception:
        return None


def _provider_catalog_sync() -> list[dict[str, Any]]:
    """Describe every provider from the same catalog used by ``hermes model``."""
    from hermes_cli.config import load_config
    from hermes_cli.provider_catalog import provider_catalog

    cfg = load_config()
    catalog: dict[str, dict[str, Any]] = {}
    for descriptor in provider_catalog():
        slug = str(descriptor.slug).strip().lower()
        if not slug:
            continue
        catalog[slug] = {
            "slug": slug, "name": str(descriptor.label or slug),
            "description": str(descriptor.description or ""),
            "auth_type": str(descriptor.auth_type or "api_key"),
            "env_vars": [str(value) for value in descriptor.api_key_env_vars if value],
            "base_url_env_var": str(descriptor.base_url_env_var or ""),
            "signup_url": str(descriptor.signup_url or ""),
            "source": str(descriptor.tab or "keys"),
        }
        if descriptor.auth_type == "aws_sdk":
            catalog[slug]["setup_vars"] = ["AWS_REGION", "AWS_PROFILE"]
        elif descriptor.auth_type == "vertex":
            catalog[slug]["setup_vars"] = ["VERTEX_CREDENTIALS_PATH"]

    user_providers = cfg.get("providers") if isinstance(cfg, dict) else None
    if isinstance(user_providers, dict):
        for raw_slug, detail in user_providers.items():
            slug = str(raw_slug).strip().lower()
            if not slug or not isinstance(detail, dict):
                continue
            row = catalog.setdefault(slug, {
                "slug": slug, "name": slug, "auth_type": "api_key", "env_vars": [],
                "source": "custom", "description": "User-configured provider",
            })
            row["name"] = str(detail.get("display_name") or detail.get("name") or row["name"])
            row["source"] = "configured"
            # Only retain presence flags here: custom endpoints may contain query credentials.
            row["configured_base_url"] = bool(detail.get("base_url") or detail.get("api"))
            row["enabled"] = detail.get("enabled", True) is not False
            row["configured_inline_key"] = bool(isinstance(detail.get("api_key"), str) and detail.get("api_key"))
            row["custom_endpoint"] = "discover_models" in detail or isinstance(detail.get("models"), dict)
            if row["custom_endpoint"] and detail.get("key_env"):
                env_var = str(detail["key_env"])
                row["env_vars"] = list(dict.fromkeys([*(row.get("env_vars") or []), env_var]))
    custom = cfg.get("custom_providers") if isinstance(cfg, dict) else None
    if isinstance(custom, list):
        for detail in custom:
            if not isinstance(detail, dict):
                continue
            slug = str(detail.get("provider_key") or detail.get("name") or "custom").strip().lower()
            if not slug:
                continue
            row = catalog.setdefault(slug, {
                "slug": slug, "name": slug, "auth_type": "api_key", "env_vars": [],
                "source": "custom endpoint", "description": "User-configured custom endpoint",
            })
            row["name"] = str(detail.get("name") or row["name"])
            row["source"] = "custom endpoint"
            row["configured_base_url"] = bool(detail.get("base_url") or detail.get("url"))
            row["custom_endpoint"] = True
    try:
        from hermes_cli.web_routers.oauth import _build_oauth_catalog
        for entry in _build_oauth_catalog():
            slug = str(entry.get("id") or "").strip().lower()
            if slug in catalog:
                catalog[slug]["oauth_flow"] = str(entry.get("flow") or "external")
                catalog[slug]["oauth_cli_command"] = str(entry.get("cli_command") or "")
                catalog[slug]["oauth_docs_url"] = str(entry.get("docs_url") or "")
            elif slug:
                catalog[slug] = {
                    "slug": slug, "name": str(entry.get("name") or slug),
                    "auth_type": "oauth_external", "env_vars": [], "source": "accounts",
                    "description": "Account sign-in", "oauth_flow": str(entry.get("flow") or "external"),
                    "oauth_cli_command": str(entry.get("cli_command") or ""),
                    "oauth_docs_url": str(entry.get("docs_url") or ""),
                }
    except Exception:
        pass
    return sorted(catalog.values(), key=lambda row: (str(row["name"]).casefold(), row["slug"]))


def _provider_is_configured(adapter: Any, item: dict[str, Any]) -> bool:
    if item.get("configured_inline_key") is True:
        return True
    for env_var in item.get("env_vars") or []:
        try:
            from .telethon_adapter import _secret
            if _secret(str(env_var)):
                return True
        except Exception:
            continue
    for env_var in [item.get("base_url_env_var"), *(item.get("setup_vars") or [])]:
        if not env_var:
            continue
        try:
            from .telethon_adapter import _secret
            if _secret(str(env_var)):
                return True
        except Exception:
            continue
    if "pool_configured" in item:
        if item.get("pool_configured"):
            return True
    else:
        try:
            from agent.credential_pool import load_pool
            if load_pool(str(item.get("slug") or "")).has_credentials():
                return True
        except Exception:
            pass
    if item.get("oauth_status_loaded"):
        if item.get("oauth_logged_in"):
            return True
    elif str(item.get("auth_type") or "").startswith(("oauth_", "external_process", "copilot")):
        try:
            from hermes_cli.web_routers.oauth import _resolve_provider_status
            if _resolve_provider_status(str(item.get("slug") or ""), None).get("logged_in"):
                return True
        except Exception:
            pass
    if item.get("configured_base_url"):
        return True
    return False


def _provider_current_slug() -> str:
    try:
        from hermes_cli.inventory import load_picker_context
        return str(load_picker_context().current_provider or "").strip().lower()
    except Exception:
        return ""


def _enrich_provider_status(adapter: Any, item: dict[str, Any], current_slug: str) -> None:
    """Attach presence-only status; never place raw keys, tokens, or URLs in a menu state."""
    from .telethon_adapter import _secret

    item["is_current"] = str(item.get("slug") or "").lower() == current_slug
    item["env_status"] = {str(var): bool(_secret(str(var))) for var in item.get("env_vars") or []}
    item["setup_status"] = {str(var): bool(_secret(str(var))) for var in item.get("setup_vars") or []}
    base_var = str(item.get("base_url_env_var") or "")
    item["base_url_configured"] = bool(_secret(base_var)) if base_var else bool(item.get("configured_base_url"))
    entries: list[dict[str, str]] = []
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(str(item.get("slug") or ""))
        item["pool_configured"] = bool(pool.has_credentials())
        for entry in pool.entries():
            entries.append({
                "label": str(getattr(entry, "label", "") or "credential"),
                "status": str(getattr(entry, "last_status", "") or "ready"),
            })
    except Exception:
        pass
    item["pool_entries"] = entries
    if item.get("oauth_flow") or str(item.get("auth_type") or "").startswith(("oauth_", "external_process", "copilot")):
        try:
            from hermes_cli.web_routers.oauth import _resolve_provider_status
            item["oauth_logged_in"] = bool(_resolve_provider_status(str(item.get("slug") or ""), None).get("logged_in"))
        except Exception:
            item["oauth_logged_in"] = False
        item["oauth_status_loaded"] = True
    item["configured"] = _provider_is_configured(adapter, item)


def _build_provider_status_list(adapter: Any) -> list[dict[str, Any]]:
    current_slug = _provider_current_slug()
    items = _provider_catalog_sync()
    for item in items:
        _enrich_provider_status(adapter, item, current_slug)
    return items


def _provider_list_view(
    token: str,
    items: list[dict[str, Any]],
    page: int,
    *,
    heading: str = "Model providers",
    empty_message: str | None = None,
) -> tuple[str, Any]:
    pages = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    rows: list[list[tuple[str, str, str | None]]] = []
    for index, item in enumerate(items[start:start + _PAGE_SIZE], start=start):
        marker = "✓ " if item.get("configured") else ""
        label = f"{marker}{_clean_label(item.get('name') or item.get('slug'), 34)}"
        rows.append([(label, f"hm:providers:view:{token}:{index}", "success" if marker else None)])
    nav = []
    if page:
        nav.append(("‹ Previous", f"hm:providers:list:{token}:{page - 1}", None))
    if page + 1 < pages:
        nav.append(("Next ›", f"hm:providers:list:{token}:{page + 1}", None))
    if nav:
        rows.append(nav)
    rows.append([("Models", "hm:management:models", "primary"), ("Dashboard", "hm:management:miniapp", None)])
    rows.append([("Add custom endpoint", "hm:providers:addcustom", "success")])
    rows.append([("Close", "hm:management:close", None)])
    blocks = [
        f"<h1>{escape(str(heading))}</h1>",
        "<p>Browse providers Hermes supports. A <b>✓</b> marks credentials or endpoint settings as available.</p>",
    ]
    if not items:
        blocks.append(
            f"<p>{escape(str(empty_message or 'No providers are currently available.'))}</p>"
        )
    else:
        blocks.extend([
            "<hr />",
            f"<p><i>Page {page + 1} of {pages} · {len(items)} providers</i></p>",
        ])
    return "\n".join(blocks), inline_markup(rows)


def _provider_detail_view(item: dict[str, Any], *, can_manage: bool, in_dm: bool, token: str, index: int) -> tuple[str, Any]:
    name = _clean_label(item.get("name") or item.get("slug"), 64)
    slug = _clean_label(item.get("slug"), 48)
    auth_type = _clean_label(item.get("auth_type") or "API key", 32)
    env_vars = [str(v) for v in item.get("env_vars") or [] if str(v).strip()]
    configured = bool(item.get("configured"))
    lines = [f"# {name}", f"Provider: `{slug}`", f"Authentication: `{auth_type}`",
             f"Status: {'configured' if configured else 'not configured'}"]
    if item.get("oauth_flow"):
        lines.append(f"Account sign-in: {'connected' if item.get('oauth_logged_in') else 'not connected'}")
    if item.get("configured_base_url") or item.get("base_url_configured"):
        lines.append("Endpoint: configured")
    if env_vars:
        lines.extend(["", "Credential values are never shown here."])
    flow = str(item.get("oauth_flow") or "")
    if flow == "device_code":
        lines.extend(["", "This account supports a secure device-code sign-in."])
    elif flow == "external":
        command = str(item.get("oauth_cli_command") or f"hermes auth add {slug}")
        lines.extend(["", f"Sign-in is managed by the provider's supported client. Host command: `{command}`."])
    elif auth_type in {"api_key", "api-key"} and env_vars:
        lines.extend(["", "Use Credentials to add an API key or manage its environment setting."])
    elif auth_type in {"aws_sdk", "vertex"}:
        lines.extend(["", "This provider uses host SDK/profile credentials rather than a pasted API key."])
    if item.get("source") in {"configured", "custom endpoint"} or slug == "custom":
        lines.extend(["", "Custom provider definitions are stored in the active profile configuration; open Dashboard → Config to edit them."])
    lines.extend(["", "Browse models below or use `/models`. `/reasoning` changes this chat's effort only when the selected model supports it."])
    rows: list[list[tuple[str, str, str | None]]] = []
    if can_manage and in_dm:
        rows.append([("Credentials / sign-in", f"hm:providers:credentials:{token}:{index}", "success")])
        if flow == "device_code":
            rows.append([("Connect account", f"hm:providers:oauth:{token}:{index}", "primary")])
    rows.append([("Browse this provider's models", f"hm:providers:models:{token}:{index}", "primary")])
    if item.get("base_url_env_var") and can_manage and in_dm:
        rows.append([("Provider endpoint", f"hm:providers:baseurl:{token}:{index}", None)])
    if item.get("custom_endpoint") and can_manage and in_dm:
        rows.append([("Edit custom endpoint", f"hm:providers:editcustom:{token}:{index}", None)])
    if item.get("is_current"):
        rows.append([("Reasoning for this chat", "hm:management:reasoning", None)])
    elif item.get("supports_reasoning"):
        lines.append("\nThis provider declares model reasoning controls; select one of its models first to set this chat's effort.")
    if item.get("signup_url"):
        rows.append([("Provider console", f"hm:providers:signup:{token}:{index}", None)])
    rows.append([("‹ Providers", f"hm:providers:list:{token}:0", None)])
    rows.append([("Close", "hm:management:close", None)])
    return "\n".join(lines), inline_markup(rows)


def _provider_credentials_view(item: dict[str, Any], *, can_manage: bool, in_dm: bool,
                              token: str, index: int) -> tuple[str, Any]:
    name = _clean_label(item.get("name") or item.get("slug"), 64)
    env_vars = [str(value) for value in item.get("env_vars") or [] if str(value).strip()]
    setup_vars = [str(value) for value in item.get("setup_vars") or [] if str(value).strip()]
    lines = [f"# {name} · credentials", "", "Only status is shown; stored values are never displayed."]
    rows: list[list[tuple[str, str, str | None]]] = []
    if item.get("oauth_flow"):
        lines.append(f"Account sign-in: **{'connected' if item.get('oauth_logged_in') else 'not connected'}**")
    env_status = item.get("env_status") if isinstance(item.get("env_status"), dict) else {}
    if env_vars:
        lines.extend(["", "**Environment keys**"])
        for env_index, env_var in enumerate(env_vars):
            state = "set" if env_status.get(env_var) else "not set"
            lines.append(f"• `{env_var}` — {state}")
            if can_manage and in_dm:
                rows.append([(f"Set { _clean_label(env_var, 32) }", f"hm:providers:envkey:{token}:{index}:{env_index}", "success")])
    if setup_vars:
        lines.extend(["", "**Host credential settings**"])
        setup_status = item.get("setup_status") if isinstance(item.get("setup_status"), dict) else {}
        for env_index, env_var in enumerate(setup_vars):
            state = "set" if setup_status.get(env_var) else "not set"
            lines.append(f"• `{env_var}` — {state} (no secret value shown)")
            if can_manage and in_dm:
                rows.append([(f"Set { _clean_label(env_var, 32) }", f"hm:providers:setup:{token}:{index}:{env_index}", None)])
    pool_entries = item.get("pool_entries") if isinstance(item.get("pool_entries"), list) else []
    lines.extend(["", f"Pooled credentials: **{len(pool_entries)}**"])
    if pool_entries:
        for entry in pool_entries[:8]:
            lines.append(f"• {_clean_label(entry.get('label') or 'credential', 36)} — {_clean_label(entry.get('status') or 'ready', 20)}")
        if len(pool_entries) > 8:
            lines.append(f"• and {len(pool_entries) - 8} more")
    auth_type = str(item.get("auth_type") or "")
    accepts_api_key = auth_type in {"api_key", "api-key"} or bool(env_vars) or item.get("source") in {"configured", "custom", "custom endpoint"}
    if can_manage and in_dm and accepts_api_key:
        rows.append([("Add pooled API key", f"hm:providers:poolkey:{token}:{index}", "primary")])
    flow = str(item.get("oauth_flow") or "")
    if flow == "device_code" and can_manage and in_dm:
        rows.append([("Connect account", f"hm:providers:oauth:{token}:{index}", "primary")])
    elif flow == "external":
        command = str(item.get("oauth_cli_command") or f"hermes auth add {item.get('slug')}")
        lines.extend(["", f"Account sign-in is performed by the provider's supported client: `{command}`."])
        lines.append("Use Dashboard → Keys → Accounts for the full sign-in/status view.")
    if item.get("base_url_env_var"):
        base_status = bool(item.get("base_url_configured"))
        lines.extend(["", f"Endpoint override `{_clean_label(item.get('base_url_env_var'), 48)}`: {'set' if base_status else 'not set'}."])
        if can_manage and in_dm:
            rows.append([("Set endpoint override", f"hm:providers:baseurl:{token}:{index}", None)])
    if item.get("oauth_flow") or env_vars or item.get("source") in {"configured", "custom endpoint"} or item.get("slug") == "custom":
        rows.append([("Dashboard credential controls", f"hm:providers:dashboard:{token}:{index}", None)])
    if not (can_manage and in_dm):
        lines.extend(["", "To change credentials, open this menu in a private chat as an explicitly allowed account."])
    rows.append([("‹ Provider", f"hm:providers:view:{token}:{index}", None)])
    rows.append([("Close", "hm:management:close", None)])
    return "\n".join(lines), inline_markup(rows)


def _models_provider_view(token: str, providers: list[dict[str, Any]], page: int) -> tuple[str, Any]:
    pages = max(1, (len(providers) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _PAGE_SIZE
    rows: list[list[tuple[str, str, str | None]]] = []
    for index, provider in enumerate(providers[start:start + _PAGE_SIZE], start=start):
        count = len(provider.get("models") or [])
        marker = "✓ " if provider.get("is_current") else ""
        label = f"{marker}{_clean_label(provider.get('name') or provider.get('slug'), 28)} · {count}"
        rows.append([(label, f"hm:models:provider:{token}:{index}", "primary" if marker else None)])
    nav = []
    if page:
        nav.append(("‹ Previous", f"hm:models:providers:{token}:{page - 1}", None))
    if page + 1 < pages:
        nav.append(("Next ›", f"hm:models:providers:{token}:{page + 1}", None))
    if nav:
        rows.append(nav)
    rows.append([("Add / configure provider", "hm:management:providers", "success")])
    rows.append([("Close", "hm:management:close", None)])
    if not providers:
        text = "# Available models\nNo configured provider returned models. Open `/providers` to see setup options."
    else:
        text = (f"# Available models\nChoose a provider to browse its model catalog. Hermes shows the full list returned by each live API; "
                "where a provider does not expose live discovery, its supported cached/curated catalog is used.\n\n"
                f"__Page {page + 1} of {pages} · {len(providers)} providers__")
    return text, inline_markup(rows)


def _models_list_view(token: str, providers: list[dict[str, Any]], provider_index: int,
                      page: int) -> tuple[str, Any]:
    provider = providers[provider_index]
    models = [str(model) for model in provider.get("models") or [] if str(model).strip()]
    pages = max(1, (len(models) + _MODEL_PAGE_SIZE - 1) // _MODEL_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * _MODEL_PAGE_SIZE
    rows: list[list[tuple[str, str, str | None]]] = []
    for model_index, model in enumerate(models[start:start + _MODEL_PAGE_SIZE], start=start):
        label = _clean_label(model, 50)
        rows.append([(label, f"hm:models:pick:{token}:{provider_index}:{model_index}", None)])
    nav = []
    if page:
        nav.append(("‹ Previous", f"hm:models:list:{token}:{provider_index}:{page - 1}", None))
    if page + 1 < pages:
        nav.append(("Next ›", f"hm:models:list:{token}:{provider_index}:{page + 1}", None))
    if nav:
        rows.append(nav)
    rows.append([("‹ Providers", f"hm:models:providers:{token}:0", None)])
    rows.append([("Close", "hm:management:close", None)])
    current = " · current" if provider.get("is_current") else ""
    text = (f"# {_clean_label(provider.get('name') or provider.get('slug'), 48)} models{current}\n"
            f"{len(models)} model(s) available from this provider. Select one to switch this chat.\n\n"
            f"__Page {page + 1} of {pages} • total {len(models)}__")
    return text, inline_markup(rows)


def _dashboard_menu_markup(url: str) -> Any:
    try:
        from telethon import Button, types
        rows = [
            [Button.url("Chat", _dashboard_page_url(url, "/chat")),
             Button.url("Models", _dashboard_page_url(url, "/models"))],
            [Button.url("OAuth accounts", _dashboard_page_url(url, "/env", "section-oauth")),
             Button.url("API keys", _dashboard_page_url(url, "/env", "section-providers"))],
            [Button.url("Channels", _dashboard_page_url(url, "/channels")),
             Button.url("Sessions", _dashboard_page_url(url, "/sessions"))],
            [Button.url("Profiles", _dashboard_page_url(url, "/profiles")),
             Button.url("Config", _dashboard_page_url(url, "/config"))],
            [Button.inline("Close", b"hm:management:close")],
        ]
        return types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=row) for row in rows])
    except Exception:
        return _url_markup("Open Dashboard", url)


def _miniapp_view(url: str) -> tuple[str, Any]:
    if url:
        text = (
            "# Mini App & Dashboard\n\n"
            "Open the secure web Dashboard from Telegram. These buttons link directly to chat, model selection, "
            "OAuth accounts, API keys, channels, sessions, profiles, and configuration. The Dashboard opens in "
            "Telegram's browser and continues to enforce its configured sign-in and permissions.\n\n"
            "This is the Dashboard's web interface; a separately registered Telegram Web App is not configured here."
        )
        return text, _dashboard_menu_markup(url)
    text = (
        "# Mini App & Dashboard\n\n"
        "No public HTTPS Dashboard URL is configured for this profile. Set `dashboard.public_url` "
        "(or `HERMES_DASHBOARD_PUBLIC_URL`) to the HTTPS address, then reopen `/miniapp`.\n\n"
        "Telegram menus available now: `/settings`, `/models`, `/providers`, and `/help`."
    )
    return text, inline_markup([
        [("Settings", "hm:management:settings", None), ("Models", "hm:management:models", "primary")],
        [("Providers", "hm:management:providers", None), ("Help", "hm:management:help", None)],
        [("Close", "hm:management:close", None)],
    ])


async def _list_models(adapter: Any, source: Any) -> list[dict[str, Any]]:
    async with _profile_scope(adapter, source):
        def load():
            from hermes_cli.inventory import load_picker_context
            from hermes_cli.model_switch_providers import list_picker_providers
            ctx = load_picker_context()
            providers = list_picker_providers(
                current_provider=ctx.current_provider, current_base_url=ctx.current_base_url,
                user_providers=ctx.user_providers, custom_providers=ctx.custom_providers,
                current_model=ctx.current_model, include_moa=False,
                excluded_providers=ctx.excluded_providers or [],
            )
            for item in providers:
                item.setdefault("name", item.get("slug", "provider"))
                item["models"] = [str(model) for model in (item.get("models") or []) if str(model).strip()]
            return providers
        runner = getattr(adapter, "gateway_runner", None)
        executor = getattr(runner, "_run_in_executor_with_context", None)
        if callable(executor):
            return await executor(load)
        return await asyncio.to_thread(load)


async def handle_management_command(adapter: Any, source: Any, command: str, arguments: str = "",
                                   *, reply_to: int | None = None) -> bool:
    """Render one of /models, /providers or /miniapp after Telegram's access check."""
    command = str(command or "").lower()
    denial = _slash_denial(adapter, source, command)
    if denial:
        await _send(adapter, source, denial, reply_to=reply_to)
        return True
    if command in {"providers", "miniapp"} and (
        getattr(source, "chat_type", "") != "dm" or not _is_explicit_admin(adapter, source)
    ):
        await _send(
            adapter, source,
            f"/{command} shows profile or Dashboard settings. Open it in a private chat as the owner or an explicitly allowed account.",
            reply_to=reply_to,
        )
        return True
    if command == "miniapp":
        async with _profile_scope(adapter, source):
            url = await asyncio.to_thread(_dashboard_public_url)
        text, markup = _miniapp_view(url)
        await _send(adapter, source, text, markup, reply_to=reply_to)
        return True
    if command == "providers":
        async with _profile_scope(adapter, source):
            try:
                runner = getattr(adapter, "gateway_runner", None)
                executor = getattr(runner, "_run_in_executor_with_context", None)
                if callable(executor):
                    providers = await executor(lambda: _build_provider_status_list(adapter))
                else:
                    providers = await asyncio.to_thread(_build_provider_status_list, adapter)
            except Exception:
                logger.warning("Could not load the Telegram provider catalog", exc_info=True)
                providers = []
        search = str(arguments or "").strip().casefold()
        if search:
            providers = [item for item in providers if search in " ".join(
                str(item.get(key) or "") for key in ("name", "slug", "description", "auth_type")
            ).casefold()]
        token = _new_state(adapter, source, kind="providers", providers=providers)
        heading = (
            f"Providers matching {_clean_label(search, 48)}"
            if search
            else "Model providers"
        )
        empty_message = (
            "No provider matched that filter. Reopen /providers without a filter to see the full list."
            if search
            else None
        )
        text, markup = _provider_list_view(
            token,
            providers,
            0,
            heading=heading,
            empty_message=empty_message,
        )
        await _send(adapter, source, text, markup, reply_to=reply_to)
        return True
    if command == "models":
        try:
            providers = await _list_models(adapter, source)
        except Exception:
            logger.warning("Could not load Telegram model catalog", exc_info=True)
            providers = []
        had_catalog = bool(providers)
        search = str(arguments or "").strip().casefold()
        if search:
            filtered = []
            for provider in providers:
                if search in " ".join(str(provider.get(key) or "") for key in ("name", "slug")).casefold():
                    filtered.append(provider)
                    continue
                matched = [model for model in provider.get("models", []) if search in str(model).casefold()]
                if matched:
                    filtered.append({**provider, "models": matched})
            providers = filtered
            providers = [provider for provider in providers if provider.get("models")]
        token = _new_state(adapter, source, kind="models", providers=providers)
        text, markup = _models_provider_view(token, providers, 0)
        if search and providers:
            text = text.replace("# Available models", f"# Models matching {_clean_label(search, 48)}")
        elif not providers and had_catalog:
            text = f"# Models matching {_clean_label(search, 48)}\nNo model IDs matched that filter. Open `/models` without a filter to see the full list."
        elif not providers:
            text += "\n\nIf no API key is configured, open `/providers` for setup options."
        await _send(adapter, source, text, markup, reply_to=reply_to)
        return True
    return False


async def _begin_provider_value_entry(adapter: Any, event: Any, source: Any, token: str, index_text: str,
                                      *, mode: str, env_var: str = "") -> None:
    state = _get_state(adapter, source, token)
    if not state or state.get("kind") != "providers":
        await event.answer("This provider menu expired. Open /providers again.", alert=True)
        return
    providers = state.get("providers") or []
    try:
        index = int(index_text)
        item = providers[index]
    except (ValueError, IndexError, TypeError):
        await event.answer("This provider option is unavailable.", alert=True)
        return
    if source.chat_type != "dm" or not _is_explicit_admin(adapter, source):
        await event.answer("Credential and provider settings can only be changed by an explicitly allowed account in a private chat.", alert=True)
        return
    if mode in {"env_key", "base_url", "setting"} and not env_var:
        await event.answer("This provider setting is unavailable.", alert=True)
        return
    pending = getattr(adapter, "_pending_provider_key_entry", None)
    if not isinstance(pending, dict):
        pending = {}
        adapter._pending_provider_key_entry = pending
    user_id = str(getattr(source, "user_id", "") or "")
    custom_pending = getattr(adapter, "_pending_custom_endpoint", None)
    if isinstance(custom_pending, dict):
        custom_pending.pop(user_id, None)
    pending[user_id] = {
        "created": time.monotonic(), "chat_id": str(source.chat_id),
        "provider": str(item.get("slug") or ""), "env_var": env_var, "mode": mode,
    }
    await event.answer("Ready for a private value message.")
    name = _clean_label(item.get("name") or item.get("slug"), 48)
    if mode == "env_key":
        title = f"# Set {name} API key"
        instruction = (
            "Telegram bot chats are not end-to-end encrypted. Send the key as your next message here only if acceptable. "
            "I will not echo it; the message must be removable before it is saved."
        )
    elif mode == "pool":
        title = f"# Add {name} pooled API key"
        instruction = (
            "Telegram bot chats are not end-to-end encrypted. Send the key as your next message here only if acceptable. "
            "It will be added to this provider's credential pool; the message must be removable before it is saved."
        )
    elif mode == "base_url":
        title = f"# Set {name} endpoint"
        instruction = "Send an HTTPS endpoint URL (or a local HTTP endpoint). Query strings and embedded credentials are rejected. The message is deleted before the value is saved."
    else:
        title = f"# Set {name} host setting"
        instruction = f"Send the value for `{_clean_label(env_var, 48)}`. It will be saved to the active profile after this message is deleted."
    await _send(
        adapter, source,
        f"{title}\n\n{instruction}\n\nSend `/cancel` to cancel. This request expires in 10 minutes.",
        inline_markup([[('Cancel', 'hm:providers:cancelkey', 'danger')]]),
    )


async def _begin_api_key_entry(adapter: Any, event: Any, source: Any, token: str, index_text: str) -> None:
    """Compatibility wrapper for the original environment-key action."""
    state = _get_state(adapter, source, token)
    try:
        item = (state or {}).get("providers", [])[int(index_text)]
        env_var = str((item.get("env_vars") or [])[0])
    except (ValueError, IndexError, TypeError):
        await event.answer("This provider option is unavailable.", alert=True)
        return
    await _begin_provider_value_entry(adapter, event, source, token, index_text, mode="env_key", env_var=env_var)


async def _start_custom_endpoint_wizard(adapter: Any, event: Any, source: Any,
                                        item: dict[str, Any] | None = None) -> None:
    if source.chat_type != "dm" or not _is_explicit_admin(adapter, source):
        await event.answer("Custom endpoints can only be added or edited by an explicitly allowed account in a private chat.", alert=True)
        return
    user_id = str(getattr(source, "user_id", "") or "")
    pending = getattr(adapter, "_pending_custom_endpoint", None)
    if not isinstance(pending, dict):
        pending = {}
        adapter._pending_custom_endpoint = pending
    other_pending = getattr(adapter, "_pending_provider_key_entry", None)
    if isinstance(other_pending, dict):
        other_pending.pop(user_id, None)
    editing = isinstance(item, dict)
    pending[user_id] = {
        "created": time.monotonic(), "chat_id": str(source.chat_id),
        "step": "base_url" if editing else "name",
        "id": str(item.get("slug") or "") if editing else "",
        "name": str(item.get("name") or "") if editing else "",
    }
    await event.answer("Starting endpoint setup.")
    title = f"# Edit {_clean_label(item.get('name'), 48)} endpoint" if editing else "# Add custom endpoint"
    prompt = (
        "Send the new base URL (HTTPS; HTTP is accepted only for a local endpoint). Existing credentials are kept unless replaced below."
        if editing else "Send a short display name for this provider."
    )
    await _send(adapter, source,
                f"{title}\n\nThis private wizard consumes and deletes each reply before saving it. Telegram bot chats are not end-to-end encrypted.\n\n{prompt}\n\nSend `/cancel` to stop. This setup expires in 10 minutes.",
                inline_markup([[('Cancel', 'hm:providers:cancelkey', 'danger')]]))


async def _handle_pending_custom_endpoint(adapter: Any, source: Any, message: Any, text: str) -> bool:
    pending = getattr(adapter, "_pending_custom_endpoint", None)
    user_id = str(getattr(source, "user_id", "") or "")
    item = pending.get(user_id) if isinstance(pending, dict) else None
    if not isinstance(item, dict):
        return False

    value = str(text or "").strip()
    if time.monotonic() - float(item.get("created", 0)) > _KEY_TTL_SECONDS:
        pending.pop(user_id, None)
        if not value.startswith("/"):
            try:
                await adapter._client.delete_messages(adapter._peer_for(source.chat_id), [int(message.id)], revoke=True)
            except Exception:
                pass
            await _send(adapter, source, "The endpoint setup expired. No provider settings were saved; reopen `/providers` to start again.")
            return True
        return False
    if (str(item.get("chat_id")) != str(source.chat_id) or getattr(source, "chat_type", "") != "dm"
            or not _is_explicit_admin(adapter, source)):
        pending.pop(user_id, None)
        if not value.startswith("/"):
            try:
                await adapter._client.delete_messages(adapter._peer_for(source.chat_id), [int(message.id)], revoke=True)
            except Exception:
                await _send(adapter, source, "The endpoint setup was cancelled. If the message contained a key, remove it and rotate it.")
            else:
                await _send(adapter, source, "The message was removed. No endpoint settings were saved; setup is limited to the requesting private chat.")
            return True
        return False
    if value.casefold() in {"/cancel", "cancel"}:
        pending.pop(user_id, None)
        await _send(adapter, source, "Cancelled. No custom endpoint was saved.", reply_to=getattr(message, "id", None))
        return True
    if value.startswith("/") and value.casefold() != "/skip":
        pending.pop(user_id, None)
        return False

    message_id = getattr(message, "id", None)
    try:
        await adapter._client.delete_messages(adapter._peer_for(source.chat_id), [int(message_id)], revoke=True)
    except Exception:
        pending.pop(user_id, None)
        await _send(adapter, source, "The message could not be removed, so nothing was saved. Delete it from this chat before continuing.")
        return True

    step = str(item.get("step") or "")
    if step == "name":
        if not value or len(value) > 64 or any(ord(char) < 32 for char in value):
            pending.pop(user_id, None)
            await _send(adapter, source, "Name not saved: use 1–64 characters without control characters. Start again with `/providers`.")
            return True
        item["name"] = value
        item["step"] = "base_url"
        prompt = "Send the base URL (HTTPS; HTTP is accepted only for a local endpoint). Do not include embedded credentials, query strings, or fragments."
    elif step == "base_url":
        valid_url = _is_safe_endpoint_url(value)
        if not valid_url:
            pending.pop(user_id, None)
            await _send(adapter, source, "Endpoint not saved: use HTTPS, or HTTP only for a local endpoint, without credentials, query strings, or fragments. Start again with `/providers`.")
            return True
        item["base_url"] = value.rstrip("/")
        item["step"] = "model"
        prompt = "Send the default model ID advertised by this endpoint (for example `llama-3.3-70b`)."
    elif step == "model":
        if not value or len(value) > 256 or any(ord(char) < 32 for char in value):
            pending.pop(user_id, None)
            await _send(adapter, source, "Model ID not saved: use 1–256 characters without control characters. Start again with `/providers`.")
            return True
        item["model"] = value
        item["step"] = "api_key"
        prompt = (
            "Send the API key as the next message, or `/skip` to keep an existing key or configure an unauthenticated endpoint. "
            "The key message is deleted before Hermes writes it to the profile's secret store."
        )
    elif step == "api_key":
        api_key = None if value.casefold() == "/skip" else value
        if api_key is not None and (len(api_key) < 8 or len(api_key) > 4096 or "\n" in api_key or "\r" in api_key):
            pending.pop(user_id, None)
            await _send(adapter, source, "The key was not saved because it did not look valid. Start again with `/providers`.")
            return True
        pending.pop(user_id, None)
        try:
            from hermes_cli.web_models import CustomEndpointUpdate
            from hermes_cli.web_routers.config_env import upsert_custom_endpoint
            body = CustomEndpointUpdate(
                id=str(item.get("id") or ""), name=str(item.get("name") or ""),
                base_url=str(item.get("base_url") or ""), model=str(item.get("model") or ""),
                api_key=api_key, discover_models=True, make_default=False,
            )
            profile = getattr(source, "profile", None)
            await asyncio.to_thread(upsert_custom_endpoint, body, profile=profile)
        except Exception as exc:
            logger.warning("Could not save Telegram custom endpoint (%s)", type(exc).__name__)
            await _send(adapter, source, "The setup messages were removed, but Hermes could not save this endpoint. No key value was shown.")
            return True
        await _send(adapter, source,
                    f"Saved custom endpoint `{_clean_label(item.get('name'), 48)}`. It was not made the active model; open `/models` to browse discovered models and switch when ready.")
        return True
    else:
        pending.pop(user_id, None)
        return False

    await _send(adapter, source, f"# Custom endpoint setup · {_clean_label(item.get('name') or 'new provider', 48)}\n\n{prompt}\n\nSend `/cancel` to stop. Each reply is deleted before proceeding.",
                inline_markup([[('Cancel', 'hm:providers:cancelkey', 'danger')]]))
    return True


async def handle_pending_provider_key(adapter: Any, source: Any, message: Any, text: str) -> bool:
    """Consume one private API-key message without forwarding it to the model or transcript."""
    custom_pending = getattr(adapter, "_pending_custom_endpoint", None)
    user_id = str(getattr(source, "user_id", "") or "")
    if isinstance(custom_pending, dict) and isinstance(custom_pending.get(user_id), dict):
        return await _handle_pending_custom_endpoint(adapter, source, message, text)
    pending = getattr(adapter, "_pending_provider_key_entry", None)
    item = pending.get(user_id) if isinstance(pending, dict) else None
    if not isinstance(item, dict):
        return False
    expired = time.monotonic() - float(item.get("created", 0)) > _KEY_TTL_SECONDS
    if expired:
        pending.pop(user_id, None)
        if not str(text or "").lstrip().startswith("/"):
            removed = True
            try:
                await adapter._client.delete_messages(
                    adapter._peer_for(source.chat_id), [int(message.id)], revoke=True,
                )
            except Exception:
                removed = False
            note = ("The message was removed. " if removed else
                    "If that message contained a key, remove it from the chat and rotate it. ")
            await _send(adapter, source, note + "The provider-key request expired; no key was saved. Open `/providers` to start again.")
            return True
        return False
    if (str(item.get("chat_id")) != str(source.chat_id)
            or getattr(source, "chat_type", "") != "dm"
            or not _is_explicit_admin(adapter, source)):
        pending.pop(user_id, None)
        if not str(text or "").lstrip().startswith("/"):
            removed = True
            try:
                await adapter._client.delete_messages(
                    adapter._peer_for(source.chat_id), [int(message.id)], revoke=True,
                )
            except Exception:
                removed = False
            note = "The message was removed. " if removed else "If that message contained a key, remove it and rotate it. "
            await _send(adapter, source, note + "No provider key was saved. Credential entry is limited to its requesting private chat.")
            return True
        return False
    value = str(text or "").strip()
    if value.casefold() in {"/cancel", "cancel", "cancel key"}:
        pending.pop(user_id, None)
        await _send(adapter, source, "Cancelled. No provider key was saved.", reply_to=getattr(message, "id", None))
        return True
    if value.startswith("/"):
        pending.pop(user_id, None)
        return False
    pending.pop(user_id, None)
    message_id = getattr(message, "id", None)
    try:
        await adapter._client.delete_messages(adapter._peer_for(source.chat_id), [int(message_id)], revoke=True)
    except Exception:
        logger.warning("Could not remove a Telegram provider credential message; refusing to save it")
        await _send(adapter, source,
                    "The credential message could not be removed, so the key was not saved. Delete it from this chat and use `hermes auth add` on the host.")
        return True
    mode = str(item.get("mode") or "env_key")
    if mode in {"env_key", "pool"}:
        valid = bool(value) and 8 <= len(value) <= 4096 and "\n" not in value and "\r" not in value
        failure = "The key was not saved: the message did not look like a valid API key."
    elif mode == "base_url":
        valid = _is_safe_endpoint_url(value)
        failure = "The endpoint was not saved: use a valid HTTP(S) URL without credentials, query strings, or fragments."
    else:
        valid = bool(value) and len(value) <= 4096 and not any(ord(char) < 32 for char in value)
        failure = "The setting was not saved: the value is empty or contains unsupported control characters."
    if not valid:
        await _send(adapter, source, failure)
        return True
    try:
        async with _profile_scope(adapter, source):
            if mode == "pool":
                import uuid
                from agent.credential_pool import AUTH_TYPE_API_KEY, SOURCE_MANUAL, PooledCredential, load_pool
                pool = load_pool(str(item["provider"]))
                base_url = None
                try:
                    from hermes_cli.auth_commands import _provider_base_url
                    base_url = _provider_base_url(str(item["provider"])) or None
                except Exception:
                    pass
                entry = PooledCredential(
                    provider=str(item["provider"]), id=uuid.uuid4().hex[:6],
                    label=f"telegram-{len(pool.entries()) + 1}", auth_type=AUTH_TYPE_API_KEY,
                    priority=0, source=SOURCE_MANUAL, access_token=value, base_url=base_url,
                )
                await asyncio.to_thread(pool.add_entry, entry)
            else:
                from hermes_cli.credential_lifecycle import save_provider_env_credential
                await asyncio.to_thread(save_provider_env_credential, str(item["env_var"]), value)
    except Exception as exc:
        logger.warning("Could not save Telegram provider credential (%s)", type(exc).__name__)
        await _send(adapter, source, "The key message was removed, but Hermes could not save the credential. Use `hermes auth add` on the host.")
        return True
    name = _clean_label(item.get("provider"), 48)
    if mode == "pool":
        await _send(adapter, source, f"Added a pooled API key for `{name}`. The value was not shown.")
    elif mode == "base_url":
        await _send(adapter, source, f"Saved the endpoint override for `{name}`. The value was not shown.")
    elif mode == "setting":
        await _send(adapter, source, f"Saved the host setting for `{name}`. The value was not shown.")
    else:
        await _send(adapter, source, f"Saved the API key for `{name}`. The value was not shown.")
    return True


def _oauth_session_view(adapter: Any, source: Any, token: str, session: dict[str, Any], *, note: str = "") -> tuple[str, Any]:
    provider = _clean_label(session.get("provider_name") or session.get("provider"), 48)
    code = _clean_label(session.get("user_code"), 32)
    text = (f"# Connect {provider}\n\nOpen the provider's verification page and enter this one-time code:\n\n`{code}`"
            "\n\nThis code is private. Use it only in this private chat and do not share it.")
    if note:
        text += f"\n\n{note}"
    ttl = int(session.get("expires_in") or 0)
    if ttl:
        text += f"\n\nThis code expires in about {max(1, ttl // 60)} minute(s)."
    url = str(session.get("verification_url") or "")
    markup = _url_markup("Open sign-in page", url) if url else None
    try:
        from telethon import Button, types
        buttons = []
        if url and markup:
            buttons.append(Button.url("Open sign-in page", url))
        rows_raw = [buttons] if buttons else []
        rows_raw.append([
            Button.inline("Check status", f"hm:providers:oauthpoll:{token}:{session.get('provider_index', 0)}".encode()),
            Button.inline("Cancel", f"hm:providers:oauthcancel:{token}:{session.get('provider_index', 0)}".encode()),
        ])
        rows_raw.append([Button.inline("‹ Provider", f"hm:providers:view:{session.get('provider_token', '')}:{session.get('provider_index', 0)}".encode())])
        markup = types.ReplyInlineMarkup(rows=[types.KeyboardButtonRow(buttons=row) for row in rows_raw if row])
    except Exception:
        markup = inline_markup([
            [("Check status", f"hm:providers:oauthpoll:{token}:{session.get('provider_index', 0)}", "primary"),
             ("Cancel", f"hm:providers:oauthcancel:{token}:{session.get('provider_index', 0)}", "danger")],
        ])
    return text, markup


async def _start_provider_oauth(adapter: Any, event: Any, source: Any, provider_state_token: str,
                                index_text: str) -> None:
    state = _get_state(adapter, source, provider_state_token)
    try:
        index = int(index_text)
        item = (state or {}).get("providers", [])[index]
    except (ValueError, IndexError, TypeError):
        await event.answer("This provider option is unavailable.", alert=True)
        return
    if source.chat_type != "dm" or not _is_explicit_admin(adapter, source):
        await event.answer("Provider sign-in can only be started by an explicitly allowed account in a private chat.", alert=True)
        return
    if str(item.get("oauth_flow") or "") != "device_code":
        await event.answer("This provider uses its supported host or Dashboard sign-in flow.", alert=True)
        return
    await event.answer("Starting secure sign-in…")
    try:
        from hermes_cli.web_routers.oauth import _start_device_code_flow
        profile = getattr(source, "profile", None)
        result = await _start_device_code_flow(str(item.get("slug") or ""), profile=profile)
    except Exception as exc:
        logger.warning("Could not start Telegram provider sign-in (%s)", type(exc).__name__)
        await _edit(adapter, event, source,
                    "# Sign-in unavailable\n\nHermes could not start this provider's sign-in. Try the Dashboard or the provider's host command.",
                    inline_markup([[('‹ Provider', f"hm:providers:view:{provider_state_token}:{index}", None),
                                    ('Close', 'hm:management:close', None)]]))
        return
    session = {
        "kind": "oauth", "provider": str(item.get("slug") or ""),
        "provider_name": str(item.get("name") or item.get("slug") or "provider"),
        "session_id": str(result.get("session_id") or ""),
        "user_code": str(result.get("user_code") or ""),
        "verification_url": str(result.get("verification_url") or ""),
        "expires_in": int(result.get("expires_in") or 0),
        "profile": getattr(source, "profile", None),
        "provider_token": provider_state_token, "provider_index": index,
    }
    token = _new_state(adapter, source, **session)
    text, markup = _oauth_session_view(adapter, source, token, session)
    await _edit(adapter, event, source, text, markup)


async def _poll_provider_oauth(adapter: Any, event: Any, source: Any, token: str, index_text: str) -> bool:
    state = _get_state(adapter, source, token)
    if not state or state.get("kind") != "oauth":
        await event.answer("This sign-in session expired. Open /providers and try again.", alert=True)
        return True
    if source.chat_type != "dm" or not _is_explicit_admin(adapter, source):
        await event.answer("Sign-in status is available only in the requesting private chat.", alert=True)
        return True
    try:
        from hermes_cli.web_routers.oauth import poll_oauth_session
        status = await poll_oauth_session(
            str(state.get("provider") or ""), str(state.get("session_id") or ""),
            profile=state.get("profile"),
        )
        value = str(status.get("status") or "pending").lower()
    except Exception as exc:
        logger.warning("Could not check Telegram provider sign-in (%s)", type(exc).__name__)
        value = "expired"
    if value == "approved":
        _state_store(adapter).pop(token, None)
        text = f"# { _clean_label(state.get('provider_name'), 48) } connected\n\nHermes saved the sign-in to the active profile. Open `/models` to choose one of its models."
        markup = inline_markup([
            [("Browse models", "hm:management:models", "primary"),
             ("‹ Provider", f"hm:providers:view:{state.get('provider_token')}:{state.get('provider_index')}", None)],
            [("Close", "hm:management:close", None)],
        ])
    elif value == "pending":
        session = dict(state)
        text, markup = _oauth_session_view(adapter, source, token, session, note="Waiting for provider approval.")
    else:
        _state_store(adapter).pop(token, None)
        text = f"# { _clean_label(state.get('provider_name'), 48) } sign-in ended\n\nThe session is no longer active. Start a new sign-in from `/providers` if needed."
        markup = inline_markup([
            [("‹ Provider", f"hm:providers:view:{state.get('provider_token')}:{state.get('provider_index')}", None),
             ("Close", "hm:management:close", None)],
        ])
    await event.answer("Status checked." if value == "pending" else "Sign-in updated.")
    await _edit(adapter, event, source, text, markup)
    return True


async def _cancel_provider_oauth(adapter: Any, event: Any, source: Any, token: str) -> bool:
    state = _get_state(adapter, source, token)
    if not state or state.get("kind") != "oauth":
        await event.answer("This sign-in session expired.", alert=True)
        return True
    try:
        from hermes_cli.web_routers import oauth as oauth_module
        with oauth_module._oauth_sessions_lock:
            session = oauth_module._oauth_sessions.get(str(state.get("session_id") or ""))
            if session is not None and session.get("profile") == oauth_module._oauth_profile_name(state.get("profile")):
                session["cancelled"] = True
                oauth_module._oauth_sessions.pop(str(state.get("session_id") or ""), None)
    except Exception:
        logger.warning("Could not cancel Telegram provider sign-in", exc_info=True)
    _state_store(adapter).pop(token, None)
    await event.answer("Sign-in cancelled.")
    await _edit(adapter, event, source, "# Sign-in cancelled\n\nNo new provider credentials were saved.",
                inline_markup([[('‹ Provider', f"hm:providers:view:{state.get('provider_token')}:{state.get('provider_index')}", None),
                                ('Close', 'hm:management:close', None)]]))
    return True


async def handle_management_callback(adapter: Any, event: Any, source: Any, data: str) -> bool:
    """Handle Telegram menu callbacks; every stateful action is pinned to its opener."""
    parts = str(data or "").split(":")
    if len(parts) < 2 or parts[0] != "hm":
        return False
    if parts[1] in {"providers", "models"} and not (
        parts[1] == "providers" and len(parts) == 3 and parts[2] == "cancelkey"
    ):
        access_command = "model" if parts[1] == "models" and len(parts) > 2 and parts[2] == "pick" else parts[1]
        denial = _slash_denial(adapter, source, access_command)
        if denial:
            await event.answer("This action is not available for this account.", alert=True)
            await _send(adapter, source, denial)
            return True
    if parts[1] == "management" and len(parts) == 3 and parts[2] == "close":
        await event.answer("Closed.")
        delete_helper = getattr(adapter, "_delete_rich_ui_message", None)
        if callable(delete_helper):
            await delete_helper(event, source)
        else:
            try:
                await adapter._client.delete_messages(adapter._peer_for(source.chat_id), [int(getattr(event, "message_id", 0))])
            except Exception:
                logger.debug("Could not delete closed Telegram menu", exc_info=True)
        return True
    if parts[1] == "management" and len(parts) == 3:
        action = parts[2]
        if action in {"settings", "reasoning"}:
            denial = _slash_denial(adapter, source, action)
            if denial:
                await event.answer("This action is not available for this account.", alert=True)
                await _send(adapter, source, denial)
                return True
        await event.answer()
        if action in {"models", "providers", "miniapp"}:
            return await handle_management_command(adapter, source, action)
        if action == "settings":
            from .telegram_controls import show_settings
            await show_settings(adapter, source)
            return True
        if action == "help":
            await adapter._show_help(source)
            return True
        if action == "reasoning":
            await adapter.handle_message(__import__("gateway.platforms.event", fromlist=["MessageEvent"]).MessageEvent(
                text="/reasoning", message_type=__import__("gateway.platforms.event", fromlist=["MessageType"]).MessageType.COMMAND,
                user_id=source.user_id, user_name=source.user_name, source=source,
            ))
            return True
        return False
    if parts[1] == "providers" and len(parts) == 3 and parts[2] == "addcustom":
        await _start_custom_endpoint_wizard(adapter, event, source)
        return True
    if parts[1] == "providers" and len(parts) == 3 and parts[2] == "cancelkey":
        user_id = str(getattr(source, "user_id", "") or "")
        pending = getattr(adapter, "_pending_provider_key_entry", {})
        if isinstance(pending, dict):
            pending.pop(user_id, None)
        custom_pending = getattr(adapter, "_pending_custom_endpoint", {})
        if isinstance(custom_pending, dict):
            custom_pending.pop(user_id, None)
        await event.answer("Cancelled. No provider settings or credentials were saved.")
        return True
    if parts[1] == "providers" and len(parts) == 5 and parts[2] in {"oauthpoll", "oauthcancel"}:
        action, token, index_text = parts[2], parts[3], parts[4]
        if action == "oauthpoll":
            return await _poll_provider_oauth(adapter, event, source, token, index_text)
        return await _cancel_provider_oauth(adapter, event, source, token)
    if parts[1] == "providers" and len(parts) in {5, 6}:
        action, token, index_text = parts[2], parts[3], parts[4]
        state = _get_state(adapter, source, token)
        if not state or state.get("kind") != "providers":
            await event.answer("This provider menu expired. Open /providers again.", alert=True)
            return True
        providers = state.get("providers") or []
        if action == "list":
            try:
                page = int(index_text)
            except ValueError:
                page = 0
            text, markup = _provider_list_view(token, providers, page)
        elif action == "view":
            try:
                index = int(index_text)
                item = providers[index]
            except (ValueError, IndexError, TypeError):
                await event.answer("This provider option is unavailable.", alert=True)
                return True
            text, markup = _provider_detail_view(
                item, can_manage=_is_explicit_admin(adapter, source), in_dm=source.chat_type == "dm",
                token=token, index=index,
            )
        elif action == "credentials":
            try:
                index = int(index_text)
                item = providers[index]
            except (ValueError, IndexError, TypeError):
                await event.answer("This provider option is unavailable.", alert=True)
                return True
            text, markup = _provider_credentials_view(
                item, can_manage=_is_explicit_admin(adapter, source), in_dm=source.chat_type == "dm",
                token=token, index=index,
            )
        elif action == "key":
            await _begin_api_key_entry(adapter, event, source, token, index_text)
            return True
        elif action == "editcustom":
            try:
                item = providers[int(index_text)]
            except (ValueError, IndexError, TypeError):
                await event.answer("This custom endpoint is unavailable.", alert=True)
                return True
            if not item.get("custom_endpoint"):
                await event.answer("This provider is not a custom endpoint.", alert=True)
                return True
            await _start_custom_endpoint_wizard(adapter, event, source, item)
            return True
        elif action == "envkey" and len(parts) == 6:
            try:
                item = providers[int(index_text)]
                env_var = str(item.get("env_vars", [])[int(parts[5])])
            except (ValueError, IndexError, TypeError):
                await event.answer("This credential field is unavailable.", alert=True)
                return True
            await _begin_provider_value_entry(adapter, event, source, token, index_text, mode="env_key", env_var=env_var)
            return True
        elif action == "setup" and len(parts) == 6:
            try:
                item = providers[int(index_text)]
                env_var = str(item.get("setup_vars", [])[int(parts[5])])
            except (ValueError, IndexError, TypeError):
                await event.answer("This host setting is unavailable.", alert=True)
                return True
            await _begin_provider_value_entry(adapter, event, source, token, index_text, mode="setting", env_var=env_var)
            return True
        elif action == "poolkey":
            await _begin_provider_value_entry(adapter, event, source, token, index_text, mode="pool")
            return True
        elif action == "baseurl":
            try:
                item = providers[int(index_text)]
                env_var = str(item.get("base_url_env_var") or "")
            except (ValueError, IndexError, TypeError):
                env_var = ""
            await _begin_provider_value_entry(adapter, event, source, token, index_text, mode="base_url", env_var=env_var)
            return True
        elif action == "oauth":
            await _start_provider_oauth(adapter, event, source, token, index_text)
            return True
        elif action == "models":
            try:
                index = int(index_text)
                item = providers[index]
                all_model_providers = await _list_models(adapter, source)
                models_provider = next((p for p in all_model_providers
                                        if str(p.get("slug") or "").lower() == str(item.get("slug") or "").lower()), None)
            except (ValueError, IndexError, TypeError):
                models_provider = None
                index = -1
                item = {}
            if not models_provider or not models_provider.get("models"):
                text = f"# {_clean_label(item.get('name') or item.get('slug'), 48)} models\n\nNo model list is currently available for this provider. Some providers require credentials or do not expose live model discovery."
                markup = inline_markup([
                    [("Credentials", f"hm:providers:credentials:{token}:{index}", "primary")],
                    [("‹ Provider", f"hm:providers:view:{token}:{index}", None), ("Close", "hm:management:close", None)],
                ])
            else:
                model_token = _new_state(adapter, source, kind="models", providers=[models_provider])
                text, markup = _models_list_view(model_token, [models_provider], 0, 0)
        elif action == "signup":
            try:
                item = providers[int(index_text)]
                url = str(item.get("signup_url") or item.get("oauth_docs_url") or "")
                name = _clean_label(item.get("name") or item.get("slug"), 48)
            except (ValueError, IndexError, TypeError):
                url, name = "", "Provider"
            markup = _url_markup("Open provider console", url,
                                 back_callback=f"hm:providers:view:{token}:{index_text}")
            if not markup:
                await event.answer("No HTTPS provider link is available.", alert=True)
                return True
            text = f"# {name}\n\nOpen the provider's verified HTTPS console. Hermes does not send credentials to this link."
        elif action == "dashboard":
            try:
                item = providers[int(index_text)]
            except (ValueError, IndexError, TypeError):
                await event.answer("This provider option is unavailable.", alert=True)
                return True
            async with _profile_scope(adapter, source):
                dashboard = await asyncio.to_thread(_dashboard_public_url)
            flow = str(item.get("oauth_flow") or "")
            slug = str(item.get("slug") or "")
            if slug == "custom" or item.get("source") in {"configured", "custom endpoint"}:
                target = _dashboard_page_url(dashboard, "/config")
                label = "Open provider configuration"
                detail = "Use the Dashboard configuration page to add or edit custom provider definitions."
            elif flow or item.get("source") == "accounts":
                target = _dashboard_page_url(dashboard, "/env", "section-oauth")
                label = "Open account settings"
                detail = "Use the Dashboard Accounts section for provider sign-in status and supported disconnect actions."
            else:
                target = _dashboard_page_url(dashboard, "/env", "section-providers")
                label = "Open API key settings"
                detail = "Use the Dashboard Keys section to review, clear, or configure environment credentials."
            markup = _url_markup(label, target,
                                 back_callback=f"hm:providers:view:{token}:{index_text}") if target else None
            if not markup:
                text = f"# Dashboard unavailable\n\nNo public HTTPS Dashboard URL is configured. {detail} Configure `dashboard.public_url` or `HERMES_DASHBOARD_PUBLIC_URL`, then reopen `/miniapp`."
                markup = inline_markup([[('‹ Provider', f"hm:providers:view:{token}:{index_text}", None),
                                         ('Close', 'hm:management:close', None)]])
            else:
                text = f"# Dashboard · {_clean_label(item.get('name') or slug, 48)}\n\n{detail}"
        else:
            return False
        await event.answer()
        await _edit(adapter, event, source, text, markup)
        return True
    if parts[1] == "models" and len(parts) >= 4:
        action = parts[2]
        token = parts[3]
        state = _get_state(adapter, source, token)
        if not state or state.get("kind") != "models":
            await event.answer("This model list expired. Open /models again.", alert=True)
            return True
        providers = state.get("providers") or []
        if action == "providers" and len(parts) == 5:
            try:
                page = int(parts[4])
            except ValueError:
                page = 0
            text, markup = _models_provider_view(token, providers, page)
        elif action == "provider" and len(parts) == 5:
            try:
                provider_index = int(parts[4])
                provider = providers[provider_index]
            except (ValueError, IndexError, TypeError):
                await event.answer("This provider option is unavailable.", alert=True)
                return True
            models = [str(model) for model in provider.get("models") or []]
            if not models:
                await event.answer("No models were returned by this provider.", alert=True)
                return True
            text, markup = _models_list_view(token, providers, provider_index, 0)
        elif action == "list" and len(parts) == 6:
            try:
                provider_index, page = int(parts[4]), int(parts[5])
                providers[provider_index]
            except (ValueError, IndexError, TypeError):
                await event.answer("This model page is unavailable.", alert=True)
                return True
            text, markup = _models_list_view(token, providers, provider_index, page)
        elif action == "pick" and len(parts) == 6:
            try:
                provider_index, model_index = int(parts[4]), int(parts[5])
                provider = providers[provider_index]
                model = str(provider.get("models")[model_index])
            except (ValueError, IndexError, TypeError, KeyError):
                await event.answer("This model selection is unavailable.", alert=True)
                return True
            denial = _slash_denial(adapter, source, "model")
            if denial:
                await event.answer("Model switching is not available for this account.", alert=True)
                await _send(adapter, source, denial)
                return True
            from gateway.platforms.event import MessageEvent, MessageType
            import shlex
            command = f"/model {shlex.quote(model)} --provider {shlex.quote(str(provider.get('slug') or ''))}"
            await event.answer("Switching this chat's model…")
            await _edit(adapter, event, source,
                        f"# Model selected\n`{_clean_label(provider.get('name') or provider.get('slug'))}` · `{_clean_label(model, 100)}`\n\nApplying the normal `/model` command…")
            await adapter.handle_message(MessageEvent(
                text=command, message_type=MessageType.COMMAND,
                user_id=source.user_id, user_name=source.user_name, source=source,
                metadata={"thread_id": source.thread_id} if getattr(source, "thread_id", None) else {},
            ))
            _state_store(adapter).pop(token, None)
            return True
        else:
            return False
        await event.answer()
        await _edit(adapter, event, source, text, markup)
        return True
    return False
