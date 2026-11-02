"""Gateway slash commands that switch or tune the model route:
/model, /codex-runtime, /reasoning, /fast, /personality.

Split out of ``gateway/slash_commands.py``; bound onto ``GatewayRunner`` through
``GatewaySlashCommandsMixin``. Origin internals are imported lazily inside the bodies to avoid
the import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from typing import Any, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from hermes_cli.config import atomic_config_write
from utils import base_url_host_matches

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

# /fast argument -> (service tier, persisted value, i18n label key; None = value.upper()).
_FAST_SELECTIONS = {
    "fast": ("priority", "fast", "gateway.fast.label_fast"),
    "on": ("priority", "fast", "gateway.fast.label_fast"),
    "normal": (None, "normal", "gateway.fast.label_normal"),
    "off": (None, "normal", "gateway.fast.label_normal"),
    "auto": ("auto", "auto", None),
    "cold": ("cold", "cold", None),
    "ultrafast": ("ultrafast", "ultrafast", None),
}


def _fast_route_supports(model: str, runtime: dict, tier: Optional[str] = None) -> bool:
    """The turn's own gate (``run_turn.py::_resolve_turn_agent_config``): a tier /fast accepts must be
    one the session's next request actually carries, so proxies and other providers are refused."""
    from hermes_cli.models import resolve_fast_mode_overrides

    return resolve_fast_mode_overrides(
        model, provider=runtime.get("provider"), base_url=runtime.get("base_url"), tier=tier) is not None


# /reasoning display-toggle arguments -> show_reasoning value.
_REASONING_DISPLAY_TOGGLES = {"show": True, "on": True, "hide": False, "off": False}


def _telegram_model_invocation(event: MessageEvent) -> dict[str, Any]:
    """Keep menu deletion bound to the command, never a task/topic reply anchor."""
    platform = getattr(event.source.platform, "value", event.source.platform)
    if str(platform).lower() == "telegram":
        return {"invocation_message_id": event.message_id}
    return {}


def _model_switch_skew_guard() -> Optional[str]:
    """Refuse a model switch when the gateway is running stale code: a first-time lazy import on
    a new code path can crash on a stale cached dependency. Scoped to the highest-risk trigger."""
    from gateway.code_skew import detect_code_skew

    skew = detect_code_skew()
    if not skew:
        return None
    boot_rev, disk_rev = skew
    return t(
        "gateway.model.error_prefix",
        error=t("gateway.model.err_skew", boot_rev=boot_rev, disk_rev=disk_rev),
    )


async def _persist_model_switch_to_config(result, config_path) -> None:
    """Write-through a resolved /model switch to the profile config at ``config_path``, off the
    event loop (the route comparison can do cold-start disk I/O)."""
    from hermes_cli.model_switch import persist_model_selection
    await asyncio.to_thread(persist_model_selection, result, config_path)


@dataclasses.dataclass
class _ModelSwitchContext:
    """Everything a /model switch needs beyond the target: current route + persistence policy."""

    session_key: str
    source: Any
    config_path: Any
    persist_global: bool
    one_turn: bool = False
    reasoning_effort: str = ""  # `--reasoning <level>` riding with the pick (typed path only)
    restore_snapshot: Optional[dict] = None
    current_model: str = ""
    current_provider: str = "openrouter"
    # The provider actually configured/overridden (None = unset): ``current_provider`` defaults to
    # openrouter for switch_model, which must not label the configured model in metrics.
    route_provider: Optional[str] = None
    current_base_url: str = ""
    current_api_key: str = ""
    user_provs: Any = None
    custom_provs: Any = None
    excluded_provs: list = dataclasses.field(default_factory=list)

    def read_config(self) -> None:
        """Fill the current route from ``config_path``; fail-open to the defaults."""
        from gateway.run import _load_gateway_config
        try:
            cfg = _load_gateway_config(config_path=self.config_path)
            if not cfg:
                return
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                self.current_model = model_cfg.get("default", "")
                self.current_provider = model_cfg.get("provider", self.current_provider)
                self.route_provider = model_cfg.get("provider")
                self.current_base_url = model_cfg.get("base_url", "")
            self.user_provs = cfg.get("providers")
            try:
                from hermes_cli.config import get_compatible_custom_providers
                self.custom_provs = get_compatible_custom_providers(cfg)
            except Exception:
                self.custom_provs = cfg.get("custom_providers")
            excl = cfg.get("model_catalog", {}).get("excluded_providers")
            if isinstance(excl, list):
                self.excluded_provs = excl
        except Exception:
            pass

    def apply_override(self, override: dict) -> None:
        """A session /model override supersedes the configured route."""
        if override:
            self.current_model = override.get("model", self.current_model)
            self.current_provider = override.get("provider", self.current_provider)
            self.route_provider = override.get("provider", self.route_provider)
            self.current_base_url = override.get("base_url", self.current_base_url)
            self.current_api_key = override.get("api_key", self.current_api_key)



_TEXT_LISTING_MODELS = 5


def _model_provider_listing_lines(providers) -> list[str]:
    """Text-list body for ``/model`` with no args on platforms without a picker."""
    lines: list[str] = []
    for p in providers:
        tag = t("gateway.model.current_tag") if p["is_current"] else ""
        lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
        if p["models"]:
            shown = p["models"][:_TEXT_LISTING_MODELS]  # uncapped rows arrive full; this is a preview
            model_strs = ", ".join(f"`{m}`" for m in shown)
            hidden = p["total_models"] - len(shown)
            extra = t("gateway.model.more_models_suffix", count=hidden) if hidden > 0 else ""
            lines.append(f"  {model_strs}{extra}")
        elif p.get("api_url"):
            lines.append(f"  `{p['api_url']}`")
        lines.append("")
    return lines


class GatewayModelCommandsMixin:
    """Model-route slash commands (/model, /codex-runtime, /reasoning, /fast, /personality)."""

    async def _provider_inventory_snapshot(self, source):
        """Build a cache-first provider snapshot; stale catalogs warm off the render path."""
        from gateway.run import _hermes_home, _load_gateway_config, _profile_runtime_scope
        from hermes_cli.auth import PROVIDER_REGISTRY
        from hermes_cli.config import get_compatible_custom_providers, stringify_provider_map
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.provider_catalog import provider_catalog

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(source)
        profile_home = profile_home or _hermes_home
        config_path = profile_home / "config.yaml"
        scope = _profile_runtime_scope(profile_home)
        with scope:
            cfg = await asyncio.to_thread(_load_gateway_config, config_path=config_path)
            cfg = cfg if isinstance(cfg, dict) else {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                current_model = str(model_cfg.get("default", model_cfg.get("name", "")) or "")
                current_provider = str(model_cfg.get("provider", "openrouter") or "openrouter")
                current_base_url = str(model_cfg.get("base_url", "") or "")
            else:
                current_model, current_provider = str(model_cfg or ""), "openrouter"
                current_base_url = ""
            configured_default_model = current_model
            configured_default_provider = current_provider.strip().lower()
            normalized_source = await asyncio.to_thread(self._normalize_source_for_session_key, source)
            session_key = self._session_key_for_source(normalized_source)
            override = (getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}
            if override:
                current_model = str(override.get("model") or current_model)
                current_provider = str(override.get("provider") or current_provider)
                current_base_url = str(override.get("base_url") or current_base_url)
            current_provider = current_provider.strip().lower()
            current_provider_config = PROVIDER_REGISTRY.get(current_provider)
            if current_provider_config is not None:
                current_provider = str(current_provider_config.id or current_provider).strip().lower()
            configured_provider_config = PROVIDER_REGISTRY.get(configured_default_provider)
            if configured_provider_config is not None:
                configured_default_provider = str(configured_provider_config.id or configured_default_provider).strip().lower()

            user_providers = stringify_provider_map(cfg.get("providers"))
            custom_providers = get_compatible_custom_providers(cfg)
            excluded = cfg.get("model_catalog", {}).get("excluded_providers")
            if not isinstance(excluded, list):
                excluded = []
            # Return disk/curated snapshots immediately and let the picker
            # API deduplicate stale catalog warming in the background. Never
            # wait for live provider/custom-endpoint probes just to render UI.
            authenticated_rows = await asyncio.to_thread(
                list_authenticated_providers,
                current_provider=current_provider,
                current_base_url=current_base_url,
                current_model=current_model,
                user_providers=user_providers,
                custom_providers=custom_providers,
                excluded_providers=excluded,
                max_models=250,
                refresh=False,
                probe_custom_providers=False,
                probe_current_custom_provider=False,
                non_blocking_catalogs=True,
            )

            # Providers with credentials right now (the credential truth for the
            # "configured" badge); a config.yaml model.provider pointer alone is
            # a route selection, not a credential, and must never mark a
            # credential-less provider as configured.
            authed_slugs = {
                str(provider.get("slug") or "").strip().lower()
                for provider in authenticated_rows
            }

            by_slug: dict[str, dict[str, Any]] = {}
            # Start from the CLI's unified catalog, not the authenticated-only
            # list, so users can inspect setup instructions before configuring a
            # provider. This metadata is display-safe and contains no credentials.
            catalog = provider_catalog()
            for descriptor in catalog:
                slug = str(descriptor.slug or "").strip().lower()
                if not slug:
                    continue
                by_slug[slug] = {
                    "slug": slug,
                    "name": str(descriptor.label or slug),
                    "configured": False,
                    "enabled": True,
                    "is_catalog_provider": True,
                    "is_user_defined": slug in {"custom", "moa"},
                    "models": [],
                    "total_models": 0,
                    "description": str(descriptor.description or ""),
                    "auth_type": str(descriptor.auth_type or "api_key"),
                    "api_key_env_vars": list(descriptor.api_key_env_vars or ()),
                    "base_url_env_var": str(descriptor.base_url_env_var or ""),
                    "signup_url": str(descriptor.signup_url or ""),
                    "catalog_order": int(descriptor.order),
                    "default_model": "",
                    "default_reasoning": "",
                    "catalog_refreshed": False,
                }
            for provider in PROVIDER_REGISTRY.values():
                slug = str(getattr(provider, "id", "") or "").strip().lower()
                if slug:
                    by_slug.setdefault(slug, {
                        "slug": slug,
                        "name": str(getattr(provider, "name", "") or slug),
                        "configured": False,
                        "enabled": True,
                        "is_catalog_provider": False,
                        "is_user_defined": False,
                        "models": [],
                        "total_models": 0,
                        "catalog_order": len(catalog) + len(by_slug),
                    })

            for slug, entry in user_providers.items():
                if not isinstance(entry, dict):
                    continue
                normalized = str(slug).strip().lower()
                if not normalized:
                    continue
                row = by_slug.setdefault(normalized, {
                    "slug": normalized, "name": str(entry.get("name") or normalized),
                    "configured": True, "enabled": True, "is_user_defined": True,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row["name"] = str(entry.get("name") or row["name"])
                row["enabled"] = bool(entry.get("enabled", True))
                row["is_user_defined"] = True
                row["configured"] = True
                configured_model = str(entry.get("default_model") or entry.get("model") or "").strip()
                if configured_model:
                    row["default_model"] = configured_model

            for custom in custom_providers:
                if not isinstance(custom, dict):
                    continue
                slug = str(custom.get("provider_key") or custom.get("name") or "custom").strip().lower()
                if not slug:
                    continue
                row = by_slug.setdefault(slug, {
                    "slug": slug, "name": str(custom.get("name") or slug),
                    "configured": True, "enabled": True, "is_user_defined": True,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row["configured"] = True
                row["is_user_defined"] = True
                custom_model = str(custom.get("model") or custom.get("default_model") or "").strip()
                if custom_model:
                    row["default_model"] = custom_model

            for provider in authenticated_rows:
                slug = str(provider.get("slug") or "").strip().lower()
                if not slug:
                    continue
                row = by_slug.setdefault(slug, {
                    "slug": slug, "name": str(provider.get("name") or slug),
                    "configured": True, "enabled": True,
                    "is_user_defined": bool(provider.get("is_user_defined")),
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                })
                row.update({
                    "name": str(provider.get("name") or row["name"]),
                    "configured": True,
                    "is_user_defined": bool(provider.get("is_user_defined", row["is_user_defined"])),
                    "models": [str(model) for model in (provider.get("models") or []) if str(model).strip()],
                    "total_models": int(provider.get("total_models") or len(provider.get("models") or [])),
                    "catalog_refreshed": False,
                    "catalog_status": "cached_or_curated" if provider.get("models") else "unavailable",
                })

            current_provider = current_provider.strip().lower()
            current_row = by_slug.get(current_provider)
            if current_row is None:
                current_row = {
                    "slug": current_provider or "unknown", "name": current_provider or "Unknown provider",
                    "configured": False, "enabled": True, "is_user_defined": False,
                    "models": [], "total_models": 0,
                    "catalog_order": len(catalog) + len(by_slug),
                }
                by_slug[current_row["slug"]] = current_row
            current_row["configured"] = bool(current_row.get("configured") or current_provider in authed_slugs)
            if current_provider == configured_default_provider:
                current_row["default_model"] = configured_default_model or current_row.get("default_model") or current_model
            agent_cfg = cfg.get("agent", {})
            agent_cfg = agent_cfg if isinstance(agent_cfg, dict) else {}
            reasoning_overrides = agent_cfg.get("reasoning_overrides", {})
            reasoning_overrides = reasoning_overrides if isinstance(reasoning_overrides, dict) else {}
            global_reasoning = agent_cfg.get("reasoning_effort", "")
            if global_reasoning is False:
                global_reasoning = "none"
            for provider_slug, row in by_slug.items():
                if not row.get("default_model"):
                    try:
                        from hermes_cli.models import get_default_model_for_provider
                        row["default_model"] = get_default_model_for_provider(provider_slug) or ""
                    except Exception:
                        row["default_model"] = ""
                effort = reasoning_overrides.get(row.get("default_model"), global_reasoning)
                row["default_reasoning"] = "none" if effort is False else str(effort or "")
            for slug, row in by_slug.items():
                row.setdefault("catalog_status", "not_configured")
                if row.get("configured") and row["catalog_status"] == "not_configured":
                    row["catalog_status"] = "cached_or_curated" if row.get("models") else "unavailable"
                row["is_current"] = slug == current_provider
                row["is_profile_default"] = slug == configured_default_provider
                row["profile_default_model"] = (
                    configured_default_model if slug == configured_default_provider else ""
                )
                # Status helpers inspect local config/auth stores and env only; skip unknown IDs
                # so a catalog lookup is never triggered just to render a menu.
                # A config.yaml model.provider pointer (or auth.json active_provider) is a
                # route selection, not a credential: it must never resurrect "configured"
                # for a provider whose credentials were removed. Only real auth
                # material counts here (pool entry, env credential, provider-state
                # material such as a Nous agent_key/guest identity, or keyless
                # Vertex/Bedrock routing config).
                if slug in PROVIDER_REGISTRY:
                    try:
                        from hermes_cli.auth import (
                            _explicit_env_credentials_present,
                            _explicit_pool_entry_present,
                            _keyless_provider_has_explicit_config,
                            provider_has_local_auth_material,
                        )
                        row["configured"] = bool(
                            row["configured"]
                            or _explicit_env_credentials_present(slug)
                            or _explicit_pool_entry_present(slug)
                            or provider_has_local_auth_material(slug)
                            or _keyless_provider_has_explicit_config(slug)
                        )
                    except Exception:
                        pass
                row["reasoning_supported"] = True
                row["reasoning_efforts"] = []
                if slug == current_provider and current_model and slug in {"openrouter", "nous"}:
                    try:
                        from hermes_cli.models_reasoning_caps import (
                            nous_model_reasoning_capabilities,
                            openrouter_model_reasoning_capabilities,
                        )
                        caps_fn = (
                            openrouter_model_reasoning_capabilities if slug == "openrouter"
                            else nous_model_reasoning_capabilities
                        )
                        caps = caps_fn(current_model, allow_fetch=False)
                        if caps is not None:
                            row["reasoning_supported"] = caps.get("supports_reasoning") is True
                            efforts = caps.get("supported_efforts")
                            if isinstance(efforts, list):
                                row["reasoning_efforts"] = efforts
                    except Exception:
                        pass
            # Per-user provider sharing (multiuser): a credential added by one
            # user is theirs until they share it. Hide providers whose
            # credentials are all invisible to the acting user; the turn-time
            # check in _resolve_session_agent_runtime still guards stale
            # session overrides / profile defaults pointing at them.
            acting_user = str(getattr(source, "user_id", "") or "").strip() if source is not None else ""
            if acting_user:
                from gateway.provider_sharing import provider_visible_to_user

                by_slug = {
                    slug: row for slug, row in by_slug.items()
                    if provider_visible_to_user(slug, acting_user)
                }
            providers = sorted(
                by_slug.values(),
                key=lambda row: (
                    not row["is_current"], not row["configured"],
                    int(row.get("catalog_order", len(catalog))), str(row["name"]).casefold(),
                ),
            )
            return providers, current_model, current_provider, session_key, profile_home

    @staticmethod
    def _provider_inventory_text(providers: list[dict], current_model: str, current_provider: str,
                                 *, models_only: bool = False) -> str:
        if models_only:
            lines = ["# Available models", "Read-only local cached/curated snapshot. Stale catalogs warm in the background; freshness is not verified on this view. Use `/model` to switch the current model.", ""]
            visible = [row for row in providers if row.get("models")]
            if not visible:
                lines.append("No available model catalogs were found for the configured providers.")
            for row in visible:
                lines.append(f"## {row['name']} (`{row['slug']}`) — {row['total_models']} models")
                lines.extend(f"- `{model}`" for model in row["models"])
                lines.append("")
            return "\n".join(lines).rstrip()
        lines = ["# Providers", f"Current route: `{current_provider}` · `{current_model or 'unknown'}`",
                 "Catalog counts are local cached/curated snapshots; stale catalogs refresh in the background.", ""]
        for row in providers:
            status = "configured" if row.get("configured") else "not configured"
            if not row.get("enabled", True):
                status = "disabled"
            lines.append(f"- **{row['name']}** (`{row['slug']}`): {status}; {row.get('total_models', 0)} catalog models")
        lines.extend(["", "Credential values are never shown. Use `/models` for the read-only catalog."])
        return "\n".join(lines)

    async def _handle_models_command(self, event: MessageEvent) -> Optional[str]:
        """List available model IDs; this is distinct from /model's switch/picker flow."""
        providers, current_model, current_provider, session_key, profile_home = \
            await self._provider_inventory_snapshot(event.source)
        requested = (event.get_command_args() or "").strip().casefold()
        selected = next((row for row in providers if requested in {
            str(row.get("slug", "")).casefold(), str(row.get("name", "")).casefold()
        }), None) if requested else None
        if requested and selected is None:
            matches = ", ".join(f"`{row['slug']}`" for row in providers if requested in str(row.get("slug", "")).casefold())
            return (f"No provider matched `{requested}`." + (f" Similar IDs: {matches}." if matches else ""))
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_provider_management", None)
        if callable(send_ui):
            if await send_ui(
                source=event.source, providers=providers, current_model=current_model,
                current_provider=current_provider, session_key=session_key, profile_home=profile_home,
                view="models", selected_provider=selected.get("slug") if selected else None,
                reply_to=self._reply_anchor_for_event(event),
                **_telegram_model_invocation(event),
            ):
                return None
            if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
                return None  # Telegram provider cards never degrade to a plain-text fallback.
        return self._provider_inventory_text(providers, current_model, current_provider, models_only=True)

    async def _handle_providers_command(self, event: MessageEvent) -> Optional[str]:
        """Open provider status, model-catalog, credential-status, and reasoning controls."""
        providers, current_model, current_provider, session_key, profile_home = \
            await self._provider_inventory_snapshot(event.source)
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_provider_management", None)
        if callable(send_ui) and await send_ui(
            source=event.source, providers=providers, current_model=current_model,
            current_provider=current_provider, session_key=session_key, profile_home=profile_home,
            view="providers", reply_to=self._reply_anchor_for_event(event),
            **_telegram_model_invocation(event),
        ):
            return None
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # Telegram provider cards never degrade to a plain-text fallback.
        return self._provider_inventory_text(providers, current_model, current_provider)

    async def _handle_mcp_command(self, event: MessageEvent) -> Optional[str]:
        """Open the profile-aware MCP catalogue and personal-access controls."""
        from gateway.run import _hermes_home

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        profile_home = profile_home or _hermes_home
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_mcp_management", None)
        if callable(send_ui) and await send_ui(
            source=event.source, profile_home=profile_home,
            reply_to=self._reply_anchor_for_event(event),
        ):
            return None
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # MCP management uses native MTProto RichMessages only.
        return "Interactive MCP management is not available on this chat platform."

    async def _handle_plugins_command(self, event: MessageEvent) -> Optional[str]:
        """Open the private, profile-aware general-plugin manager."""
        from gateway.run import _hermes_home

        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        profile_home = profile_home or _hermes_home
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_plugin_management", None)
        if callable(send_ui):
            try:
                if await send_ui(
                    source=event.source, profile_home=profile_home,
                    reply_to=self._reply_anchor_for_event(event),
                ):
                    return None
            except Exception:
                logger.warning("Could not open the Telegram plugin manager", exc_info=True)
        if str(getattr(event.source.platform, "value", event.source.platform)).lower() == "telegram":
            return None  # Plugin management uses native MTProto RichMessages only.
        return "Interactive plugin management is available only in the Telegram owner's private chat."

    async def _send_model_dashboard(self, event: MessageEvent, providers, current_model,
                                    current_provider, session_key, profile_home) -> Optional[str]:
        from gateway.model_ui import dashboard_payload, render_dashboard

        state = self._session_state(session_key).conversation
        reasoning = (getattr(self, "_session_reasoning_overrides", {}) or {}).get(session_key)
        if isinstance(reasoning, dict):
            reasoning = reasoning.get("effort") or reasoning.get("reasoning_effort") or ""
        payload = dashboard_payload(
            current_model=current_model, current_provider=current_provider, providers=providers,
            settings=dict(state.model_settings), reasoning_effort=str(reasoning or ""),
        )
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_model_dashboard", None)
        if callable(send_ui):
            if await send_ui(
                source=event.source, dashboard=payload, providers=providers,
                current_model=current_model, current_provider=current_provider,
                session_key=session_key, profile_home=profile_home,
                reply_to=self._reply_anchor_for_event(event),
                **_telegram_model_invocation(event),
            ):
                return None
        return render_dashboard(payload)

    def _model_settings_owner_error(self, source, profile_home) -> Optional[str]:
        """Require the configured profile owner in an actual Telegram private conversation."""
        if (str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower()
                != "telegram" or getattr(source, "chat_type", None) not in {"dm", "private"}
                or not getattr(source, "chat_id", None) or getattr(source, "is_bot", False)):
            return "Model settings are available only in the owner's Telegram DM."
        try:
            from gateway.mongo_multiuser import owner_user_id
            owner = owner_user_id(profile_home)
        except Exception:
            return "Model settings are unavailable: this profile has no readable configured owner."
        if not (owner.isascii() and owner.isdecimal() and not owner.startswith("0")):
            return "Model settings are unavailable: this profile has no valid configured owner."
        if str(getattr(source, "user_id", "") or "") != owner:
            return "Only the configured profile owner can change model settings."
        return None

    def _model_settings_reasoning(self, session_key: str) -> str:
        value = (getattr(self, "_session_reasoning_overrides", {}) or {}).get(session_key)
        if isinstance(value, dict):
            return str(value.get("effort") or value.get("reasoning_effort") or "")
        return str(value or "")

    def _telegram_model_ui_context(self, event) -> Optional[dict[str, Any]]:
        """Return the live native-settings callback context, never untrusted inbound metadata."""
        try:
            adapter = self._delivery_adapter_for(event.source)
            getter = getattr(adapter, "_model_settings_ui", None)
            ui = getter() if callable(getter) else None
            callback_context = getattr(ui, "callback_context", None)
            context = callback_context(event) if callable(callback_context) else None
            return context if isinstance(context, dict) else None
        except Exception:
            logger.debug("Could not read the active Telegram model UI callback context", exc_info=True)
            return None

    @staticmethod
    def _model_ui_provider_row(context: Optional[dict[str, Any]], current_provider: str) -> dict[str, Any]:
        if not context:
            return {}
        rows = context.get("providers") or []
        if isinstance(rows, list):
            return next((row for row in rows if isinstance(row, dict)
                         and str(row.get("slug") or "").casefold() == current_provider.casefold()), {})
        return {}

    async def _send_model_settings(self, event, session_key: str, page: str = "sampling") -> Optional[str]:
        from gateway.model_ui import render_settings, settings_payload

        state = self._session_state(session_key).conversation
        ui_context = self._telegram_model_ui_context(event)
        cached_settings = (ui_context or {}).get("settings") or {}
        cached_dashboard = (ui_context or {}).get("dashboard") or {}
        cached_provider = str(cached_settings.get("current_provider") or
                              (ui_context or {}).get("current_provider") or "")
        if ui_context and (cached_settings or cached_dashboard):
            current_model = str(cached_settings.get("current_model") or
                                cached_dashboard.get("current_model") or
                                (ui_context or {}).get("current_model") or "")
            current_provider = str(cached_provider or cached_dashboard.get("current_provider") or "")
            current_row = cached_settings or self._model_ui_provider_row(ui_context, current_provider)
        else:
            providers, current_model, current_provider, _key, _home = \
                await self._provider_inventory_snapshot(event.source)
            current_row = next((row for row in providers if row.get("slug") == current_provider), {})
        payload = settings_payload(
            settings=dict(state.model_settings), reasoning_effort=self._model_settings_reasoning(session_key),
            page=page, supported_reasoning_efforts=(current_row.get("reasoning_options") or
                                                    current_row.get("reasoning_efforts")),
            reasoning_supported=current_row.get("reasoning_supported"),
        )
        payload["current_model"] = current_model
        payload["current_provider"] = current_provider
        payload["reasoning_supported"] = current_row.get("reasoning_supported")
        payload["reasoning_options"] = list(current_row.get("reasoning_options") or
                                                current_row.get("reasoning_efforts") or [])
        payload["settings"] = dict(state.model_settings)
        adapter = self._delivery_adapter_for(event.source)
        send_ui = getattr(adapter, "send_model_settings", None)
        if callable(send_ui) and await send_ui(
            source=event.source, settings=payload, session_key=session_key,
            reply_to=self._reply_anchor_for_event(event),
            **_telegram_model_invocation(event),
        ):
            return None
        return render_settings(payload)

    async def _handle_msettings_command(self, event: MessageEvent) -> Optional[str]:
        """Open or update owner-only, conversation-scoped direct model request settings.

        Native buttons dispatch the same deterministic argument forms accepted here, so retries and
        typed use share one validation/update path.
        """
        from gateway.model_ui import MODEL_SETTINGS, SETTINGS_PAGES, parse_setting_value
        from gateway.run import _hermes_home

        resolver = getattr(self, "_resolve_profile_home_for_source", None)
        profile_home = resolver(event.source) if callable(resolver) else None
        profile_home = profile_home or _hermes_home
        blocked = self._model_settings_owner_error(event.source, profile_home)
        if blocked:
            return blocked
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        parts = (event.get_command_args() or "").strip().split()
        pages = {key: index + 1 for index, (key, _label) in enumerate(SETTINGS_PAGES)}
        if not parts:
            return await self._send_model_settings(event, session_key)
        action = parts[0].casefold()
        if action in {"back", "dashboard"}:
            providers, model, provider, key, home = await self._provider_inventory_snapshot(event.source)
            return await self._send_model_dashboard(event, providers, model, provider, key, home)
        if action == "page":
            if len(parts) != 2:
                return "Usage: `/msettings page <1-3|sampling|output|capabilities>`."
            selected = parts[1].casefold()
            if selected.isdecimal():
                number = int(selected)
                selected = next((name for name, index in pages.items() if index == number), "")
            if selected not in pages:
                return "Unknown settings page. Choose 1 (Sampling), 2 (Output), or 3 (Capabilities)."
            return await self._send_model_settings(event, session_key, selected)
        if action == "reset":
            if len(parts) != 2:
                return "Usage: `/msettings reset <temperature|top_p|frequency_penalty|presence_penalty|max_tokens|reasoning|all>`."
            setting = parts[1].casefold()
            state = self._session_state(session_key).conversation
            if setting == "all":
                state.model_settings.clear()
                self._set_session_reasoning_override(session_key, None)
            elif setting == "reasoning":
                self._set_session_reasoning_override(session_key, None)
            elif any(item.key == setting for item in MODEL_SETTINGS):
                state.model_settings.pop(setting, None)
            else:
                return f"Unsupported setting: `{setting}`."
            self._evict_cached_agent(session_key)
            page = next((field.page for field in MODEL_SETTINGS if field.key == setting), "sampling")
            return await self._send_model_settings(event, session_key, page)
        if action == "reasoning":
            if len(parts) != 2:
                return "Usage: `/msettings reasoning <none|low|medium|high|xhigh|reset>`."
            if parts[1].casefold() != "reset":
                ui_context = self._telegram_model_ui_context(event)
                cached_settings = (ui_context or {}).get("settings") or {}
                cached_dashboard = (ui_context or {}).get("dashboard") or {}
                current_provider = str(cached_settings.get("current_provider") or
                                       (ui_context or {}).get("current_provider") or
                                       cached_dashboard.get("current_provider") or "")
                current_row = cached_settings or self._model_ui_provider_row(ui_context, current_provider)
                if not ui_context:
                    providers, _model, current_provider, _key, _home = \
                        await self._provider_inventory_snapshot(event.source)
                    current_row = next((row for row in providers if row.get("slug") == current_provider), {})
                if current_row.get("reasoning_supported") is False:
                    return "The current model/provider does not support configurable reasoning effort."
                supported = current_row.get("reasoning_options") or current_row.get("reasoning_efforts") or []
                if supported and parts[1].casefold() not in supported and parts[1].casefold() != "none":
                    return "That reasoning effort is not supported by the current model. Choose: " + ", ".join(supported)
            result = self._apply_reasoning_selection(session_key, "telegram", parts[1], persist_global=False)
            return result
        if action == "set" and len(parts) == 3:
            setting, raw = parts[1].casefold(), parts[2]
        elif len(parts) == 2:
            setting, raw = action, parts[1]
        else:
            return "Usage: `/msettings set <setting> <value>` (for example `/msettings set temperature 0.7`)."
        try:
            value = parse_setting_value(setting, raw)
        except ValueError as exc:
            return str(exc)
        state = self._session_state(session_key).conversation
        state.model_settings[setting] = value
        self._evict_cached_agent(session_key)
        page = next(field.page for field in MODEL_SETTINGS if field.key == setting)
        return await self._send_model_settings(event, session_key, page)

    # ----------------------------------------------------------------- /model

    async def _perform_model_switch(
        self, ctx: _ModelSwitchContext, raw_input: str, explicit_provider, source
    ):
        """Resolve a /model switch off-loop. Returns ``(result, None)`` or ``(None, error_text)``."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import switch_model

        skew_error = _model_switch_skew_guard()
        if skew_error:
            return None, skew_error
        # Off-loop: switch_model() can hit a synchronous models.dev fetch (15s) on a cold cache.
        result = await asyncio.to_thread(
            switch_model, raw_input=raw_input, current_provider=ctx.current_provider,
            current_model=ctx.current_model, current_base_url=ctx.current_base_url,
            current_api_key=ctx.current_api_key, is_global=ctx.persist_global,
            explicit_provider=explicit_provider, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs,
        )
        if not result.success:
            return None, t("gateway.model.error_prefix", error=result.error_message)
        try:
            from hermes_cli.context_switch_guard import enrich_model_switch_warnings_for_gateway
            # Off-loop: merge_preflight_compression_warning() runs the sync provider probe ladder.
            await asyncio.to_thread(
                enrich_model_switch_warnings_for_gateway, result, self, session_key=ctx.session_key,
                source=source, custom_providers=ctx.custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)
        return result, None

    def _switch_cached_agent_model(self, result, ctx: _ModelSwitchContext, picker: bool) -> Optional[str]:
        """In-place swap on the cached agent; returns the error reply when it failed.

        The agent rolls back to the OLD model/client and re-raises; the commit (DB, override,
        eviction, config) is aborted so the next message doesn't rebuild a broken agent.
        """
        cached_agent = self._cached_agent_for(ctx.session_key)
        if cached_agent is None:
            return None
        try:
            cached_agent.switch_model(
                new_model=result.new_model, new_provider=result.target_provider,
                api_key=result.api_key, base_url=result.base_url, api_mode=result.api_mode,
                capabilities=getattr(result, "runtime_capabilities", None),
            )
        except Exception as exc:
            logger.warning(
                "%s model switch failed for cached agent: %s", "Picker" if picker else "In-place", exc
            )
            return t(
                "gateway.model.error_prefix",
                error=t("gateway.model.err_switch_failed", model=result.new_model, error=exc, current=ctx.current_model),
            )
        return None

    async def _record_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, one_turn: bool, picker: bool
    ) -> Optional[str]:
        """Persist a committed switch: session DB, next-turn note, config write-through, override map.

        Returns the warning for a ``--global`` switch whose ``config.yaml`` write or stale-override
        cleanup failed (the switch then stays a session override), else ``None``.
        """
        from hermes_cli.model_switch import format_model_for_display

        # Persist the new model to the session DB so the dashboard shows the updated model (#34850).
        _sess_db = getattr(self, "_session_db", None)
        if _sess_db is not None:  # so the dashboard shows the updated model
            try:
                _sess_entry = await self.async_session_store.get_or_create_session(source)
                # Typed path: consume the auto-reset flag so the next message's cleanup does not
                # wipe the override stored below.
                if not picker and getattr(_sess_entry, "was_auto_reset", False):
                    # See #48031.
                    _sess_entry.was_auto_reset = False
                await _sess_db.update_session_model(
                    _sess_entry.session_id, result.new_model, provider=result.target_provider,
                    base_url=result.base_url, api_mode=result.api_mode,
                )
            except Exception as exc:
                logger.debug("Failed to persist model switch to DB: %s", exc)
        # Prepended to the next user message (no system messages mid-history). Display form strips
        # opaque Palantir RID prefixes; the override map keeps the full ID for the wire.
        if not hasattr(self, "_pending_model_notes"):
            self._pending_model_notes = {}
        self._pending_model_notes[ctx.session_key] = (
            f"[Note: model was just switched from {format_model_for_display(ctx.current_model)} to "
            f"{format_model_for_display(result.new_model)} "
            f"via {result.provider_label or result.target_provider}. "
            f"{'This override applies to the next turn only. ' if one_turn else ''}"
            f"Adjust your self-identification accordingly.]"
        )
        self._session_model_overrides[ctx.session_key] = {
            "model": result.new_model, "provider": result.target_provider, "api_key": result.api_key,
            "base_url": result.base_url, "api_mode": result.api_mode,
            "request_overrides": dict(result.request_overrides or {}),
            "capabilities": dict(result.runtime_capabilities or {}),
        }
        if one_turn:
            # A repeated --once before the turn runs must keep the EARLIEST snapshot: the later
            # command's snapshot is the first temporary model, not the user's standing override.
            self._claim_one_turn_restore(ctx.session_key, ctx.restore_snapshot)
        elif not picker and hasattr(self, "_pending_one_turn_model_restores"):
            self._pending_one_turn_model_restores.pop(ctx.session_key, None)
        # A --global switch has ONE durable authority: config.yaml. Write it first; on success drop
        # the session override (memory + store) — a redundant copy would shadow every later global
        # change after a restart (#100314: a stale override resumed `gpt-5.6-sol-900k` as the base
        # 272K model). On failure keep the override so the switch truthfully survives as session-only.
        global_error: Optional[str] = None
        if ctx.persist_global:
            try:
                await _persist_model_switch_to_config(result, ctx.config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        # Precedence is session > channel_overrides > config.yaml: in a chat with a channel_overrides
        # model/provider the session override must stay, or the next turn runs the channel model.
        if ctx.persist_global and global_error is None and self._channel_override_for(source) is None:
            try:
                await self.async_session_store.set_model_override(ctx.session_key, None)
            except Exception as e:
                # Store still holds the stale copy: keep memory in agreement and report it (#100314).
                logger.warning("Failed to clear persisted session model override: %s", e)
                global_error = t("gateway.model.err_stale_override", error=e)
            else:
                self._session_model_overrides.pop(ctx.session_key, None)
        # Non-secret write-through so the override survives a restart (api_key/api_mode are
        # re-resolved on rehydration); a --once override must NOT outlive a restart.
        # Write-through the non-secret parts (model/provider/base_url) to the session store so the override
        # survives a gateway restart. api_key/api_mode are never persisted — they are re-resolved via
        # runtime provider resolution on rehydration. /model --once is intentionally EXCLUDED from the
        # write-through: a one-turn override must never survive a restart. The persisted value stays at the
        # pre-once state (the prior session override, or nothing), which is exactly what the finally-restore
        # reverts the in-memory dict to. (#29923 review defect: the original implementation wrote through,
        # so a crash before the restore rehydrated the once-model permanently.)
        elif not one_turn:
            try:
                await self.async_session_store.set_model_override(
                    ctx.session_key, self._session_model_overrides[ctx.session_key]
                )
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        self._evict_cached_agent(ctx.session_key)  # next turn builds fresh from the override
        return global_error

    async def _model_switch_confirmation(
        self, result, ctx: _ModelSwitchContext, *, one_turn: bool, picker: bool,
        global_error: Optional[str] = None,
    ) -> str:
        """Confirmation text with full metadata (display form shortens opaque Palantir IDs)."""
        from gateway.run import _load_gateway_config
        from hermes_cli.model_switch import format_model_for_display, resolve_display_context_length_async

        lines = [
            t("gateway.model.switched", model=format_model_for_display(result.new_model)),
            t("gateway.model.provider_label", provider=result.provider_label or result.target_provider),
        ]
        # Provider-aware chain: Codex OAuth, Nous and profile live-catalog caps win over the raw models.dev entry.
        mi = result.model_info
        model_cfg: dict = {}
        config_ctx = None
        with contextlib.suppress(Exception):  # fail-open on config read errors
            model_cfg = _load_gateway_config().get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("context_length") is not None:
                config_ctx = int(model_cfg["context_length"])
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        ctx_len = await resolve_display_context_length_async(
            result.new_model, result.target_provider,
            base_url=result.base_url or ctx.current_base_url or "",
            api_key=result.api_key or ctx.current_api_key or "", model_info=mi,
            custom_providers=ctx.custom_provs, config_context_length=config_ctx,
            configured_model=model_cfg.get("default") or model_cfg.get("model"),
            configured_provider=model_cfg.get("provider"),
            configured_base_url=model_cfg.get("base_url"),
        )
        if ctx_len:
            lines.append(t("gateway.model.context_label", tokens=f"{ctx_len:,}"))
        if mi and mi.max_output:
            lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
        if mi:
            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
        openrouter_claude = (
            base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower()
        )
        if not picker and (openrouter_claude or result.api_mode == "anthropic_messages"):
            lines.append(t("gateway.model.prompt_caching_enabled"))
        if result.warning_message:
            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
        if getattr(result, "usage_notice", ""):
            lines.append(result.usage_notice)
        if ctx.persist_global and global_error is not None:
            # Never claim a clean global commit the disk did not take (#100314).
            lines.append(t("gateway.model.warning_prefix", warning=global_error))
            lines.append(t("gateway.model.session_only_hint"))
        elif ctx.persist_global:
            lines.append(t("gateway.model.saved_global"))
        elif one_turn:
            lines.append(t("gateway.model.next_turn_only"))
        else:
            lines.append(t("gateway.model.session_only_hint"))
        return "\n".join(lines)

    async def _commit_model_switch(
        self, result, ctx: _ModelSwitchContext, *, source, picker: bool = False
    ) -> str:
        """Apply a resolved switch (cached agent, session, config) and build the confirmation; shared
        by the typed path and the picker callback (``picker=True`` never carries --once).

        Entry for the picker / cost-confirm callbacks, which fire outside ``_handle_model_command``
        and take the switch lock themselves; the typed path already holds it."""
        async with self._model_switch_lock():
            return await self._commit_model_switch_locked(result, ctx, source=source, picker=picker)

    def _channel_override_for(self, source):
        """This chat's ``channel_overrides`` entry (model/provider), or None."""
        from gateway.run import _get_channel_override
        cfg = getattr(self, "config", None)
        if not cfg or source is None:
            return None
        return _get_channel_override(
            cfg, source.platform, str(source.chat_id) if source.chat_id else "",
            thread_id=str(source.thread_id) if getattr(source, "thread_id", None) else None,
            parent_id=str(source.parent_chat_id) if getattr(source, "parent_chat_id", None) else None,
        )

    def _model_switch_lock(self) -> asyncio.Lock:
        """Runner-wide lock over a /model command's read-resolve-commit. Slash commands bypass the busy
        guard while no agent runs, so a second /model on the same (or another) session otherwise
        interleaves with the first's store/config awaits: two ``--global`` picks left config.yaml on
        whichever thread wrote last and a ``--global`` cleanup wiped a session pick issued after it
        (#100314). Lazy: the runner is built without __init__ in tests."""
        lock = self.__dict__.get("_model_switch_lock_obj")
        if lock is None:
            lock = self.__dict__["_model_switch_lock_obj"] = asyncio.Lock()
        return lock

    def _record_switch_metrics(self, result, ctx: _ModelSwitchContext, source) -> None:
        """Slash dispatch does not install the routed profile's scope, so a multiplexed runner binds
        the owning home for the switch row and its switch_away friction."""
        from hermes_cli.observability.shared_metrics_events import record_model_switch
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with contextlib.suppress(Exception):
                home = self._resolve_profile_home_for_source(source)
        session_id = None
        with contextlib.suppress(Exception):
            session_id = getattr(self._cached_agent_for(ctx.session_key), "session_id", None)
        token = set_hermes_home_override(str(home)) if home else None
        try:
            record_model_switch(
                from_provider=ctx.route_provider, to_provider=result.target_provider, surface="gateway",
                from_model=ctx.current_model, session_id=session_id)
        finally:
            if token is not None:
                reset_hermes_home_override(token)

    async def _commit_model_switch_locked(self, result, ctx: _ModelSwitchContext, *, source, picker: bool) -> str:
        one_turn = False if picker else ctx.one_turn
        from gateway.session_setting_commands import defer_local_model_pick, local_authority_live
        authority, live = local_authority_live(self, source)
        level = ""
        if live is not None:
            # A local session runs its frozen policy: the owner's canonical model mutation is the
            # only commit, landing at the next turn boundary (this command is the running turn).
            if one_turn:
                return t("gateway.model.error_prefix", error=t("gateway.model.err_once_local"))
            from hermes_constants import parse_reasoning_effort
            effort = (ctx.reasoning_effort or "").strip().lower()
            level = effort if parse_reasoning_effort(effort) is not None else ""
            await asyncio.to_thread(defer_local_model_pick, authority, live, result, level)
            self._record_switch_metrics(result, ctx, source)
            global_error = None
            if ctx.persist_global:
                try:
                    await _persist_model_switch_to_config(result, ctx.config_path)
                except Exception as e:
                    logger.warning("Failed to persist model switch", exc_info=True)
                    global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        else:
            error = self._switch_cached_agent_model(result, ctx, picker)
            if error is not None:
                return error
            self._record_switch_metrics(result, ctx, source)
            global_error = await self._record_model_switch(
                result, ctx, source=source, one_turn=one_turn, picker=picker)
        reply = await self._model_switch_confirmation(
            result, ctx, one_turn=one_turn, picker=picker, global_error=global_error,
        )
        if level:
            # Committed with the local pick: only the config write-through and the reply remain.
            saved = (ctx.persist_global and global_error is None
                     and self._save_gateway_config_key("agent.reasoning_effort", level))
            reply += "\n" + t("gateway.reasoning.set_global" if saved else "gateway.reasoning.set_session",
                              effort=level)
        elif ctx.reasoning_effort and not one_turn:
            # `/model X --reasoning <level>`: same applier as /reasoning, same scope as the pick.
            # The record step already evicted the cached agent, so the pin lands on the rebuild.
            from gateway.run import _platform_config_key
            reply += "\n" + self._apply_reasoning_selection(
                ctx.session_key, _platform_config_key(source.platform), ctx.reasoning_effort,
                persist_global=ctx.persist_global and global_error is None)
        return reply + ("\n" + t("gateway.model.local_next_turn") if live is not None else "")

    async def _send_model_picker(self, event: MessageEvent, source, adapter, session_key: str, listing_kwargs: dict, on_model_selected) -> bool:
        """Send the interactive /model picker; False when nothing was sent (text fallback). *source*
        is session-key-normalized so the picker's thread metadata lands where the next turn reads."""
        from hermes_cli.model_switch_providers import list_picker_providers
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(
                list_picker_providers, max_models=50, include_moa=True, **listing_kwargs
            )
        except Exception:
            providers = []
        if not providers:
            return False
        picker_metadata = dict(self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)) or {})
        picker_metadata.update({"user_id": source.user_id, "chat_type": source.chat_type})
        result = await adapter.send_model_picker(
            chat_id=source.chat_id, providers=providers,
            current_model=listing_kwargs["current_model"],
            current_provider=listing_kwargs["current_provider"], session_key=session_key,
            on_model_selected=on_model_selected,
            metadata=picker_metadata,
        )
        return bool(result.success)

    async def _model_listing_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, profile_home
    ) -> Optional[str]:
        """``/model`` with no args: interactive picker where supported, else the text list."""
        from hermes_cli.model_switch import list_authenticated_providers
        from hermes_cli.providers import get_label

        listing_kwargs = dict(
            current_provider=ctx.current_provider, current_base_url=ctx.current_base_url,
            current_model=ctx.current_model, user_providers=ctx.user_provs,
            custom_providers=ctx.custom_provs, excluded_providers=ctx.excluded_provs,
            # Chat `/model` is a read path: catalogs come from the disk cache and stale ones warm
            # in the background, and only the selected custom endpoint is probed live, so one
            # degraded provider can't stall the reply (#74003). Mirrors the GUI read path.
            non_blocking_catalogs=True, probe_custom_providers=False, probe_current_custom_provider=True,
        )
        adapter = self._delivery_adapter_for(ctx.source)
        if adapter is not None and getattr(type(adapter), "send_model_picker", None) is not None:
            async def _picker_switch(model_id: str, provider_slug: str) -> str:
                # The picker callback binds the raw event source (pre-normalization).
                result, error = await self._perform_model_switch(ctx, model_id, provider_slug, event.source)
                if error is not None:
                    return error
                return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

            async def _on_model_selected(_chat_id: str, model_id: str, provider_slug: str) -> str:
                if profile_home is None:
                    return await _picker_switch(model_id, provider_slug)
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(profile_home):
                    return await _picker_switch(model_id, provider_slug)

            if await self._send_model_picker(event, ctx.source, adapter, ctx.session_key, listing_kwargs, _on_model_selected):
                return None  # Picker sent — adapter handles the response

        lines = [t("gateway.model.current_label", model=ctx.current_model or t("gateway.shared.unknown_value"),
                   provider=get_label(ctx.current_provider)), ""]
        try:  # off-loop: listing still reads config/disk cache synchronously (#41289)
            providers = await asyncio.to_thread(list_authenticated_providers, max_models=_TEXT_LISTING_MODELS, **listing_kwargs)
            lines.extend(_model_provider_listing_lines(providers))
        except Exception:
            pass
        lines.append(t("gateway.model.usage_switch_model"))
        lines.append(t("gateway.model.usage_switch_provider"))
        lines.append(t("gateway.model.usage_persist"))
        return "\n".join(lines)

    async def _model_selection_guard_reply(
        self, event: MessageEvent, ctx: _ModelSwitchContext, result
    ) -> tuple[bool, Optional[str]]:
        """Selection-guard confirmation for the typed path (pickers confirm via their own UI).

        The unified registry (cost + data-policy guards) runs off-loop — pricing lookups may hit
        models.dev on a cache miss. Returns ``(fired, reply)``; the reply is None when the platform
        rendered confirm buttons itself.
        """
        try:
            from hermes_cli.model_selection_guards import (
                combined_selection_warning, selection_context_for_agent)
            warning = await asyncio.to_thread(
                combined_selection_warning, result.new_model, provider=result.target_provider,
                base_url=result.base_url or ctx.current_base_url or "",
                api_key=result.api_key or ctx.current_api_key or "", model_info=result.model_info,
                selection_context=selection_context_for_agent(self._cached_agent_for(ctx.session_key)),
            )
        except Exception:
            warning = None
        if warning is None:
            return False, None

        async def _on_cost_confirm(choice: str) -> str:
            if choice == "cancel":
                return t("gateway.model.switch_cancelled", model=ctx.current_model or t("gateway.shared.unknown_value"))
            # "once" and "always" both proceed — selection guards have no persistent opt-out.
            return await self._commit_model_switch(result, ctx, source=ctx.source)

        _p = self._typed_command_prefix_for(event.source.platform)
        message = t("gateway.model.guard_confirm", title=warning.title, message=warning.message, prefix=_p)
        return True, await self._request_slash_confirm(
            event=event, command="model", title=warning.title, message=message, handler=_on_cost_confirm,
        )

    async def _handle_model_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model. Taken under the switch lock BEFORE the first await so
        concurrent commands commit in issue order (see ``_model_switch_lock``)."""
        async with self._model_switch_lock():
            return await self._handle_model_command_locked(event)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        from gateway.run import _hermes_home
        from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

        raw_args = (event.get_command_args() or "").strip()
        if not raw_args:
            ui_context = self._telegram_model_ui_context(event)
            cached_dashboard = (ui_context or {}).get("dashboard") or {}
            cached_providers = (ui_context or {}).get("providers")
            cached_session = (ui_context or {}).get("_callback_session_key")
            if ui_context and isinstance(cached_providers, list) and cached_dashboard and cached_session:
                return await self._send_model_dashboard(
                    event, cached_providers,
                    str(cached_dashboard.get("current_model") or
                        (ui_context or {}).get("current_model") or ""),
                    str(cached_dashboard.get("current_provider") or
                        (ui_context or {}).get("current_provider") or ""),
                    str(cached_session),
                    (ui_context or {}).get("profile_home") or
                    (ui_context or {}).get("_callback_profile_home") or _hermes_home,
                )
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        request = parse_model_switch_args(raw_args)  # single-owner parser
        if request.errors:
            return request.error_messages()[0]  # gateway decoration over canonical copy
        if request.force_refresh:  # bust the disk cache so the picker shows live data
            with contextlib.suppress(Exception):
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
        # Normalize like a message turn (Telegram DM topic recovery) before deriving the override
        # key, so the override lands under the key the next turn reads.
        # Check for session override. See #30479.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        ctx = _ModelSwitchContext(
            # Gateway routing columns — forward ALL of them at CREATE time, same fix as the
            # compression-rotation bug in agent/conversation_compression.py. Without these, the branched
            # child row has NULL routing columns until switch_session() below calls
            # _record_gateway_session_peer() — a crash/kill anywhere between here and there (most plausibly
            # mid-history-copy, since each append_message call a few lines down is independently
            # best-effort) leaves the branch permanently unroutable: unreachable by chat/thread lookup, and
            # unreachable via /resume's IDOR guard too (which requires the row's chat_id/thread_id to match
            # the caller's). user_id is critical for the fallback lookup path (hermes_state.py:1994-2009)
            # that searches by the complete peer tuple when session_key doesn't match. origin_json and
            # display_name complete the identity (same shape as the reset path's db_create_kwargs in
            # gateway/session.py, #82633) so consumers that read routing/presentation data from state.db
            # (the mcp-serve plugin, mirror, channel directory) see the branch row fully formed with zero backfill gap.
            session_key=session_key,
            source=source,
            config_path=(profile_home or _hermes_home) / "config.yaml",
            persist_global=resolve_persist_behavior(
                request.is_global, request.is_session, is_once=request.is_once,
                explicit_provider=request.explicit_provider,
            ),
            one_turn=request.is_once,
            reasoning_effort=request.reasoning_effort,
            restore_snapshot=self._snapshot_session_model_override(session_key) if request.is_once else None,
        )
        ctx.read_config()
        ctx.apply_override(self._session_model_overrides.get(session_key, {}))
        if raw_args.casefold() == "providers":
            return await self._handle_providers_command(dataclasses.replace(event, text="/providers"))
        if raw_args.casefold() in {"list", "change"}:
            return await self._handle_models_command(dataclasses.replace(event, text="/models"))
        if not request.target and not request.explicit_provider:
            # Upstream routes a bare /model to the interactive picker
            # (_model_listing_reply); the fork's dashboard is route/settings-first
            # and wins only where the adapter actually renders it. When the
            # adapter has no send_model_dashboard, _send_model_dashboard would
            # only fall back to render_dashboard text, so fall through to the
            # picker (or its text listing) instead.
            adapter = self._delivery_adapter_for(event.source)
            if callable(getattr(adapter, "send_model_dashboard", None)):
                providers, current_model, current_provider, dashboard_key, dashboard_home = \
                    await self._provider_inventory_snapshot(source)
                return await self._send_model_dashboard(
                    event, providers, current_model, current_provider, dashboard_key, dashboard_home,
                )
            return await self._model_listing_reply(event, ctx, profile_home)
        result, error = await self._perform_model_switch(ctx, request.target, request.explicit_provider, source)
        if error is not None:
            return error
        guard_fired, guard_reply = await self._model_selection_guard_reply(event, ctx, result)
        if guard_fired:
            return guard_reply
        return await self._commit_model_switch_locked(result, ctx, source=source, picker=False)

    # -------------------------------------------------- usage-window limit: one-tap switch

    def _schedule_usage_limit_offer(self, event: MessageEvent, limit: dict, *, session_key: str = "",
                                    generation: Optional[int] = None) -> None:
        """After a usage-window refusal's reply: offer the plan's recommended model as a button where the
        platform already has quick-choice buttons, else one line of text.

        Runs as the adapter's post-delivery callback, so the offer always lands under the reply (the reply
        is sent after this turn returns). The tap switches this session only (never the default) and
        re-sends the refused message, like /retry. No credits button in a chat: anyone in the room could
        spend the owner's credits, so the Portal is named instead. Hermes never switches on its own."""
        try:
            adapter = self._delivery_adapter_for(event.source) if event.source is not None else None
            register = getattr(adapter, "register_post_delivery_callback", None)
            if session_key and callable(register):
                register(session_key, lambda: self._offer_usage_limit_switch(event, limit, generation),
                         generation=generation)
                return
            self._track_background_task(self._offer_usage_limit_switch(event, limit, generation))
        except Exception:
            logger.debug("usage-limit offer scheduling failed", exc_info=True)

    async def _offer_usage_limit_switch(self, event: MessageEvent, limit: dict,
                                        generation: Optional[int] = None) -> None:
        from agent.plan_limits import limit_next_steps_from_payload
        try:
            suggestions = [s for s in (limit.get("suggestions") or []) if isinstance(s, dict) and s.get("model")]
            source = event.source
            if source is None:
                return
            prefix = self._typed_command_prefix_for(source.platform)
            adapter = self._delivery_adapter_for(source)
            session_key = self._session_key_for_source(source)
            if suggestions and adapter is not None and getattr(type(adapter), "send_choice_picker", None) is not None:
                async def _on_choice(_chat_id: str, value: str) -> str:
                    if generation is not None and self._usage_limit_offer_is_stale(session_key, generation):
                        # A newer turn ran since the refusal: /retry would re-run that one, not the refused one.
                        return t("platform.discord.picker.choice_expired")
                    return await self._accept_usage_limit_switch(
                        event, value, offer=None if generation is None else (session_key, generation))

                choices = [{"value": str(s["model"]),
                            "label": t("gateway.usage_limit.switch_choice", model=str(s.get("label") or s["model"]),
                                       note=str(s.get("note") or "")),
                            "is_current": False} for s in suggestions]
                if await self._try_send_choice_picker(
                        event, session_key, title=t("gateway.usage_limit.picker_title"), choices=choices,
                        on_choice_selected=_on_choice):
                    return
            text = limit_next_steps_from_payload(limit, model_command=f"{prefix}model", topup_command=None, portal=True)
            if adapter is not None and text:
                await adapter.send(source.chat_id, text, metadata=self._reply_metadata(event))
        except Exception:
            logger.debug("usage-limit offer failed", exc_info=True)

    def _usage_limit_offer_is_stale(self, session_key: str, generation: int) -> bool:
        """True once the session has started a newer run than the refused one. Fails open (not stale)."""
        try:
            return int(self._session_state(session_key).persistent.run_generation) != int(generation)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    async def _accept_usage_limit_switch(self, event: MessageEvent, model_id: str, *,
                                         offer: Optional[tuple[str, int]] = None) -> str:
        """The tap: switch this session to *model_id* on Nous, then re-send the refused message.
        ``offer`` is the refused turn's ``(session_key, run_generation)``."""
        from gateway.run import _hermes_home
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_home = self._resolve_profile_home_for_source(event.source)
        ctx = _ModelSwitchContext(session_key=session_key, source=source,
                                  config_path=(profile_home or _hermes_home) / "config.yaml", persist_global=False)

        async def _switch() -> str:
            ctx.read_config()
            ctx.apply_override(self._session_model_overrides.get(session_key, {}))
            result, error = await self._perform_model_switch(ctx, model_id, "nous", event.source)
            if error is not None:
                return error
            return await self._commit_model_switch(result, ctx, source=event.source, picker=True)

        before = self._session_model_overrides.get(session_key)
        if profile_home is None:
            reply = await _switch()
        else:
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(profile_home):
                reply = await _switch()
        after = self._session_model_overrides.get(session_key)
        switched = after is not None and after is not before and after.get("provider") == "nous"
        if switched:
            # Only after the switch landed (an error reply leaves the override unchanged). Exactly a typed /retry from the same sender (rewinds the refused turn, runs the message
            # again, and the adapter delivers the reply). Called directly, the re-run's reply would be
            # returned to nobody.
            self._track_background_task(self._dispatch_usage_limit_retry(event, offer))
        return reply

    async def _dispatch_usage_limit_retry(self, event: MessageEvent, offer: Optional[tuple[str, int]] = None) -> None:
        from gateway.platforms.event import MessageType
        adapter = self._delivery_adapter_for(event.source)
        # Re-checked here, with no await before the /retry: the switch awaited model resolution, and a
        # turn that ran meanwhile is what /retry would rewind. The switch itself stands.
        if adapter is None or (offer is not None and self._usage_limit_offer_is_stale(*offer)):
            return
        try:
            await adapter.handle_message(MessageEvent(
                text="/retry", message_type=MessageType.TEXT, source=event.source,
                raw_message=event.raw_message, channel_prompt=event.channel_prompt))
        except Exception:
            logger.debug("usage-limit retry dispatch failed", exc_info=True)

    # -------------------------------------------------- /codex-runtime, /personality

    async def _handle_codex_runtime_command(self, event: MessageEvent) -> str | None:
        """Handle /codex-runtime; a real change evicts the cached agent so the new api_mode applies
        on the next message (avoids prompt-cache invalidation mid-session)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Codex runtime", text=text, nav=("model", "status")):
                return None
            return text
        from hermes_cli import codex_runtime_switch as crs

        new_value, errors = crs.parse_args(event.get_command_args().strip() if event else "")
        if errors:
            return await _reply("\n".join(errors))
        try:
            from hermes_cli.config import load_config, save_config
        except Exception as exc:
            return await _reply(t("gateway.codex_runtime.config_load_failed", error=exc))
        result = crs.apply(
            load_config(), new_value, persist_callback=(save_config if new_value is not None else None),
        )
        if result.success and new_value is not None and result.requires_new_session:
            try:
                self._evict_cached_agent(self._session_key_for_source(event.source))
            except Exception:
                logger.debug("could not evict cached agent after codex-runtime change", exc_info=True)
        return await _reply(f"{'✓' if result.success else '✗'} {result.message}")

    async def _handle_personality_command(self, event: MessageEvent) -> str | None:
        """Handle /personality — list or set a personality (hermes_cli.personality owns the state)."""

        async def _reply(text: str) -> str | None:
            if await self._send_telegram_command_card(
                event, title="Personality", text=text, nav=("model", "status")):
                return None
            return text
        from gateway.run import _load_gateway_config
        from hermes_cli.personality import (
            active_personality_name,
            available_personalities,
            describe_personality,
            persist_personality,
            resolve_personality,
        )

        args = event.get_command_args().strip()
        try:
            config = _load_gateway_config()
        except Exception:
            config = {}
        personalities = available_personalities(config)
        if not args:
            current = active_personality_name(config)
            lines = [t("gateway.personality.header"), t("gateway.personality.none_option")]
            for name, prompt in personalities.items():
                marker = " ✓" if name == current else ""
                lines.append(
                    t("gateway.personality.item", name=f"{name}{marker}", preview=describe_personality(prompt))
                )
            lines.append(t("gateway.personality.usage"))
            return await _reply("\n".join(lines))
        try:
            name, _new_prompt = resolve_personality(args, config)
        except ValueError:
            available = "`none`, " + ", ".join(f"`{n}`" for n in personalities)
            return await _reply(t("gateway.personality.unknown", name=args.lower(), available=available))
        # Persists the selection only (never agent.system_prompt, a user-owned overlay) into the
        # routed profile's config.yaml; the next turn re-resolves the prompt — no process-global state.
        if not persist_personality(name):
            return await _reply(t("gateway.personality.save_failed", error=t("gateway.personality.err_config_write")))
        if not name:
            return await _reply(t("gateway.personality.cleared"))
        return await _reply(t("gateway.personality.set_to", name=name))

    # ----------------------------------------------------------- /reasoning, /fast

    def _save_gateway_config_key(self, key_path: str, value) -> bool:
        """Save a dot-separated key to config.yaml (shared by /reasoning, /fast and their pickers)."""
        from gateway.slash_commands import _nested_dict
        from gateway.run import _gateway_config_home
        from hermes_cli.config import read_user_config_raw
        config_path = _gateway_config_home() / "config.yaml"
        try:
            user_config = read_user_config_raw(config_path)  # raw: never persist merged defaults
            *parents, leaf = key_path.split(".")
            _nested_dict(user_config, *parents)[leaf] = value
            atomic_config_write(config_path, user_config)
            return True
        except Exception as e:
            logger.error("Failed to save config key %s: %s", key_path, e)
            return False

    def _set_reasoning_override(self, session_key: str, value) -> None:
        """Store (or clear with None) the session reasoning override and drop the cached agent."""
        self._set_session_reasoning_override(session_key, value)
        self._evict_cached_agent(session_key)

    def _apply_reasoning_selection(
        self, session_key: str, platform_key: str, value: str, persist_global: bool = False,
    ) -> str:
        """Apply a /reasoning argument (typed or picked) and return the reply."""
        from hermes_constants import parse_reasoning_effort

        value = (value or "").strip().lower()
        from gateway.session_setting_commands import local_live_for_route
        live = local_live_for_route(self, session_key)
        show = _REASONING_DISPLAY_TOGGLES.get(value)
        if show is not None:  # per-platform display toggle
            self._show_reasoning = show
            if live is not None:
                live.show_reasoning = show  # what this local session's viewers stream (config.get show_reasoning)
            self._save_gateway_config_key(f"display.platforms.{platform_key}.show_reasoning", show)
            key = "gateway.reasoning.display_set_on" if show else "gateway.reasoning.display_set_off"
            return t(key, platform=platform_key)
        if value == "reset":
            if persist_global:
                return t("gateway.reasoning.reset_global_unsupported")
            self._set_session_reasoning_override(session_key, None)
            self._reasoning_config = self._load_reasoning_config()
            self._evict_cached_agent(session_key)
            return t("gateway.reasoning.reset_done")

        parsed = parse_reasoning_effort(value)
        if parsed is None:
            return t("gateway.reasoning.unknown_arg", arg=value)
        self._reasoning_config = parsed
        if persist_global:
            if self._save_gateway_config_key("agent.reasoning_effort", value):
                # A local session runs its frozen level, not config.yaml's: it takes the level too.
                self._set_reasoning_override(session_key, parsed if live is not None else None)
                return t("gateway.reasoning.set_global", effort=value)
            self._set_reasoning_override(session_key, parsed)
            return t("gateway.reasoning.set_global_save_failed", effort=value)
        self._set_reasoning_override(session_key, parsed)
        return t("gateway.reasoning.set_session", effort=value)

    async def _try_send_choice_picker(
        self, event: MessageEvent, session_key: str, title: str, choices: list, on_choice_selected,
    ) -> bool:
        """Send an interactive choice picker when the adapter *type* supports it (the /model gate);
        a failed send returns False (text fallback) instead of erroring."""
        adapter = self._delivery_adapter_for(event.source)
        if adapter is None or getattr(type(adapter), "send_choice_picker", None) is None:
            return False
        try:
            result = await adapter.send_choice_picker(
                chat_id=event.source.chat_id, title=title, choices=choices, session_key=session_key,
                on_choice_selected=on_choice_selected, metadata=self._reply_metadata(event),
            )
            return bool(getattr(result, "success", False))
        except Exception as e:
            logger.warning("send_choice_picker failed, falling back to text: %s", e)
            return False

    async def _handle_reasoning_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reasoning command — manage reasoning effort and display toggle."""
        from gateway.run import _platform_config_key
        from hermes_constants import VALID_REASONING_EFFORTS

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        # Normalize (Telegram DM topic recovery) so the override key matches the next turn's.
        # See #30479.
        _reasoning_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        from gateway.session_setting_commands import local_live
        if (_live := local_live(self, event.source)) is not None:
            self._show_reasoning = getattr(_live, "show_reasoning", None) is True
        # Effective model (session /model override wins) so per-model reasoning_overrides display.
        _session_model = str(
            ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}).get("model") or ""
        )
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source, session_key=session_key, model=_session_model,
        )
        platform_key = _platform_config_key(event.source.platform)
        if raw_args:  # typed path — same applier the picker uses
            return self._apply_reasoning_selection(session_key, platform_key, args, persist_global=persist_global)
        rc = self._reasoning_config
        # Labels tell the truth about the route: a Hermes-internal step (``ultra``) that the wire
        # clamps is shown as "ultra (sends max on this route)" instead of a distinct level (#61634).
        from agent.reasoning_effort import effort_display_label
        from gateway.run import _load_gateway_config
        from hermes_cli.codex_runtime_switch import get_current_runtime
        _session_route = ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {})
        _model_cfg = {}
        with contextlib.suppress(Exception):  # fail-open on config read errors, like /model does
            _model_cfg = _load_gateway_config(config_path=self.config_path).get("model", {}) or {}
        _route = (
            _session_route.get("provider") or _model_cfg.get("provider"),
            _session_model or _model_cfg.get("default") or _model_cfg.get("model"),
            get_current_runtime({"model": _model_cfg}),
        )
        if rc is None:
            level, current_effort = t("gateway.reasoning.level_default"), "medium"
        elif rc.get("enabled") is False:
            level, current_effort = t("gateway.reasoning.level_disabled"), "none"
        else:
            current_effort = rc.get("effort", "medium")
            level = effort_display_label(current_effort, *_route)
        display_state = t("gateway.reasoning.display_on") if self._show_reasoning else t("gateway.reasoning.display_off")
        has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
        scope = t("gateway.reasoning.scope_session") if has_session_override else t("gateway.reasoning.scope_global")

        async def _on_reasoning_choice(_chat_id: str, value: str) -> str:
            return self._apply_reasoning_selection(session_key, platform_key, value)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.reasoning.picker_title", level=level, scope=scope, display=display_state),
            choices=[
                {"value": "none", "label": t("gateway.reasoning.choice_none"), "is_current": current_effort == "none"},
                *({"value": lv, "label": effort_display_label(lv, *_route), "is_current": lv == current_effort}
                  for lv in VALID_REASONING_EFFORTS),
                *({"value": v, "label": t(f"gateway.reasoning.choice_{v}"), "is_current": False}
                  for v in ("reset", "show", "hide")),
            ],
            on_choice_selected=_on_reasoning_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.reasoning.status", level=level, scope=scope, display=display_state)

    def _apply_fast_selection(self, session_key: str, value: str, persist: bool = False) -> str:
        """Apply a /fast argument (typed or picked) and return the reply."""
        selection = _FAST_SELECTIONS.get(value)
        if selection is None:
            return t("gateway.fast.unknown_arg", arg=value)
        tier, saved_value, label_key = selection
        label = t(label_key) if label_key else value.upper()
        self._service_tier = tier
        from gateway.session_setting_commands import local_live_for_route
        if persist and self._save_gateway_config_key("agent.service_tier", saved_value):
            if local_live_for_route(self, session_key) is not None:
                # A local session runs its frozen tier, not config.yaml's: it takes the tier too.
                self._set_session_service_tier_override(session_key, tier)
            else:
                self._set_session_service_tier_override(session_key, None, clear=True)  # global wins
            self._evict_cached_agent(session_key)
            return t("gateway.fast.saved", label=label)
        # Session override — also the fallback after a failed config write (as /reasoning --global).
        self._set_session_service_tier_override(session_key, tier)
        self._evict_cached_agent(session_key)
        return t("gateway.fast.session_only", label=label)

    async def _handle_fast_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /fast — the CLI Priority Processing toggle; session-scoped unless ``--global``
        (persists agent.service_tier, parity with /model)."""
        from agent.fast_mode import FAST_ARG_ALIASES, service_tier_word
        from gateway.run import _load_gateway_config, _resolve_gateway_model

        # The /reasoning parser strips --global (any position) and normalizes unicode dashes.
        args, persist_global = self._parse_reasoning_command_args(event.get_command_args().strip().lower())
        # Normalized like /model and /reasoning (#30479): the tier override must land under the key
        # the next turn reads, and the eligibility check must see that turn's route.
        source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(source)
        self._service_tier = self._resolve_session_service_tier(session_key=session_key)
        user_config = _load_gateway_config()
        try:
            # Off the loop: credential resolution can refresh an OAuth token, as the turn does.
            model, runtime = await asyncio.to_thread(
                self._resolve_session_agent_runtime, source=source, session_key=session_key, user_config=user_config)
        except Exception:
            # No usable route at all (the turn fails too): judge the configured model alone.
            model, runtime = _resolve_gateway_model(user_config), {}
        if not _fast_route_supports(model, runtime):
            return t("gateway.fast.not_supported")
        args = FAST_ARG_ALIASES.get(args, args)
        from agent.codex_service_tiers import ultrafast_offered
        ultrafast = ultrafast_offered(model, provider=runtime.get("provider"), base_url=runtime.get("base_url"),
                                      access_token=runtime.get("api_key"))
        if args == "ultrafast" and not ultrafast:
            return t("gateway.fast.ultrafast_not_supported", model=model)
        if args and args != "status":
            return self._apply_fast_selection(session_key, args, persist=persist_global)
        mode = service_tier_word(self._service_tier)
        status = {"fast": t("gateway.fast.status_fast"), "normal": t("gateway.fast.status_normal")}.get(mode, mode)

        async def _on_fast_choice(_chat_id: str, value: str) -> str:
            return self._apply_fast_selection(session_key, value, persist=persist_global)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.fast.picker_title", mode=status),
            choices=[
                {"value": v, "label": t(f"gateway.fast.choice_{v}"), "is_current": mode == v}
                for v in ("fast", "normal", "auto", "cold", *(("ultrafast",) if ultrafast else ()))
            ],
            on_choice_selected=_on_fast_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.fast.status", mode=status)
