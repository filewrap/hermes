"""Runtime enforcement for HF's opt-in OpenRouter free-only mode.

This is deliberately inert unless ``HERMES_STRICT_FREE_ONLY=1``.  It is used at
both the main-agent and auxiliary outbound request boundaries; configuration is
never rewritten by this module.
"""
from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlsplit

POLICY_VERSION = 1
STRICT_FREE_ONLY_ENV = "HERMES_STRICT_FREE_ONLY"
OPENROUTER_MODEL = "openrouter/free"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_PROVIDER_POLICY = {
    "allow_fallbacks": False,
    "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
}
_FORBIDDEN_BODY_KEYS = frozenset({
    "models", "model", "fallback", "fallbacks", "fallback_providers",
    "plugins", "web_search", "web_search_options", "route", "preset",
})


class StrictFreeOnlyError(RuntimeError):
    """Raised when a runtime route/request violates the strict free-only policy."""


def enabled() -> bool:
    """Return whether the process opted into strict free-only enforcement."""
    return os.environ.get(STRICT_FREE_ONLY_ENV, "").strip() == "1"


def _normalized_base_url(value: Any) -> str:
    if value is None:
        return ""
    # OpenAI clients expose an httpx.URL; str() is its canonical textual form.
    raw = str(value).strip()
    try:
        parts = urlsplit(raw)
        if (parts.scheme.lower() != "https" or parts.hostname is None
                or parts.hostname.lower() != "openrouter.ai"
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.query or parts.fragment or parts.path.rstrip("/") != "/api/v1"):
            return ""
        return OPENROUTER_BASE_URL
    except (TypeError, ValueError):
        return ""


def validate_route(
    provider: Any, model: Any, base_url: Any, api_mode: Any = "chat_completions",
) -> None:
    """Reject any non-canonical provider, model, endpoint, or transport in strict mode."""
    if not enabled():
        return
    if str(provider or "").strip().lower() != "openrouter":
        raise StrictFreeOnlyError("Strict free-only mode permits only the OpenRouter provider")
    if str(model or "").strip() != OPENROUTER_MODEL:
        raise StrictFreeOnlyError(f"Strict free-only mode permits only model {OPENROUTER_MODEL}")
    if _normalized_base_url(base_url) != OPENROUTER_BASE_URL:
        raise StrictFreeOnlyError(f"Strict free-only mode requires endpoint {OPENROUTER_BASE_URL}")
    if str(api_mode or "").strip().lower() != "chat_completions":
        raise StrictFreeOnlyError("Strict free-only mode permits only OpenAI-compatible chat completions")


def enforce_request(
    kwargs: Mapping[str, Any], *, provider: Any, model: Any, base_url: Any,
    api_mode: Any = "chat_completions",
) -> dict[str, Any]:
    """Validate and return a sanitized request with explicit zero-cost routing limits.

    Non-strict callers get an unchanged shallow copy. In strict mode, conflicting
    model/route/plugin/search controls are rejected, never silently rerouted.
    """
    request = dict(kwargs)
    if not enabled():
        return request
    validate_route(provider, model, base_url, api_mode)
    if request.get("model", OPENROUTER_MODEL) != OPENROUTER_MODEL:
        raise StrictFreeOnlyError("Request model override rejected by strict free-only mode")
    request["model"] = OPENROUTER_MODEL
    for key in ("models", "fallback", "fallbacks", "fallback_providers", "route", "plugins", "web_search", "web_search_options", "preset"):
        if key in request:
            raise StrictFreeOnlyError(f"Request field {key!r} is forbidden in strict free-only mode")
    if "provider" in request:
        raise StrictFreeOnlyError("Top-level provider override rejected by strict free-only mode")
    if request.get("extra_query"):
        raise StrictFreeOnlyError("Query routing overrides are forbidden in strict free-only mode")
    # OpenRouter server tools (advisor/subagent/search/image generation) can bill
    # independently of the free primary model. Only local function tools are admitted.
    tools = request.get("tools")
    if tools is not None and (not isinstance(tools, list) or any(
        not isinstance(tool, Mapping) or tool.get("type") != "function" for tool in tools
    )):
        raise StrictFreeOnlyError("Server-side tools are forbidden in strict free-only mode")
    if "modalities" in request and request["modalities"] != ["text"]:
        raise StrictFreeOnlyError("Non-text output is forbidden in strict free-only mode")
    if "audio" in request:
        raise StrictFreeOnlyError("Audio generation is forbidden in strict free-only mode")
    for message in request.get("messages") or []:
        if isinstance(message, Mapping) and isinstance(message.get("content"), list):
            for part in message["content"]:
                if not isinstance(part, Mapping) or part.get("type") not in {"text", "image_url"}:
                    raise StrictFreeOnlyError("Paid file/audio processing is forbidden in strict free-only mode")
    extra = request.get("extra_body")
    if extra is not None and not isinstance(extra, Mapping):
        raise StrictFreeOnlyError("Invalid extra_body in strict free-only mode")
    extra_body = dict(extra or {})
    conflicts = _FORBIDDEN_BODY_KEYS.intersection(extra_body)
    if conflicts:
        raise StrictFreeOnlyError(
            "Forbidden OpenRouter request override(s): " + ", ".join(sorted(conflicts))
        )
    # Fail closed on new/unknown controls rather than trusting an extensible
    # provider body that can carry presets, server tools or separately billed plugins.
    # OpenRouter's own profile adds session affinity, while the title
    # generator requests structured text. Neither selects a model/provider or
    # enables billed server features; both must survive the final outbound guard.
    allowed_extra = {"provider", "reasoning", "include_reasoning", "top_k", "min_p",
                     "repetition_penalty", "usage", "session_id", "response_format"}
    unknown_extra = set(extra_body) - allowed_extra
    if unknown_extra:
        raise StrictFreeOnlyError(
            "Unknown extra_body controls rejected by strict free-only mode: "
            + ", ".join(sorted(str(key) for key in unknown_extra))
        )
    provider_options = extra_body.get("provider")
    if provider_options is not None:
        if not isinstance(provider_options, Mapping):
            raise StrictFreeOnlyError("Invalid OpenRouter provider routing override")
        unknown = set(provider_options) - {"allow_fallbacks", "max_price"}
        if unknown:
            raise StrictFreeOnlyError(
                "Unsupported OpenRouter provider routing override(s): " + ", ".join(sorted(unknown))
            )
        if "allow_fallbacks" in provider_options and provider_options["allow_fallbacks"] is not False:
            raise StrictFreeOnlyError("Provider fallbacks are forbidden in strict free-only mode")
        prices = provider_options.get("max_price")
        if prices is not None:
            if not isinstance(prices, Mapping) or any(
                key not in _PROVIDER_POLICY["max_price"] or value != 0
                for key, value in prices.items()
            ):
                raise StrictFreeOnlyError("Paid OpenRouter max_price override rejected")
    extra_body["provider"] = {
        "allow_fallbacks": False,
        "max_price": dict(_PROVIDER_POLICY["max_price"]),
    }
    request["extra_body"] = extra_body
    return request


def _check_fallbacks(container: Mapping[str, Any], path: str, errors: list[str]) -> None:
    for key in ("fallback_providers", "fallback_chain", "fallback_model", "fallbacks"):
        value = container.get(key)
        if value not in (None, [], ""):
            errors.append(f"{path}.{key} must be empty in strict free-only mode")


def validate_config(config: Any) -> tuple[str, ...]:
    """Read-only validation of saved routing config; returns unsafe-setting diagnostics.

    This is intentionally callable by the HF supervisor before launching Hermes.
    Absent optional settings are left absent; runtime guards remain authoritative.
    """
    if not enabled():
        return ()
    if not isinstance(config, Mapping):
        return ("config must be a mapping",)
    errors: list[str] = []
    model_cfg = config.get("model")
    if not isinstance(model_cfg, Mapping):
        errors.append("model routing config must be a mapping")
        model_cfg = {}
    try:
        validate_route(
            model_cfg.get("provider"), model_cfg.get("default", model_cfg.get("model")),
            model_cfg.get("base_url"), model_cfg.get("api_mode", "chat_completions"),
        )
    except StrictFreeOnlyError as exc:
        errors.append(str(exc))
    _check_fallbacks(config, "config", errors)
    _check_fallbacks(model_cfg, "model", errors)

    routing = config.get("provider_routing")
    if routing is not None:
        if not isinstance(routing, Mapping):
            errors.append("provider_routing must be a mapping")
        else:
            # OpenRouter's controls can select/rank different upstreams. In this
            # mode, only the free alias plus the request-level zero-price cap is used.
            unsafe = {"sort", "only", "ignore", "order", "models"}.intersection(routing)
            if unsafe:
                errors.append("provider_routing selection overrides are forbidden: " + ", ".join(sorted(unsafe)))
            if routing.get("allow_fallbacks", False) is not False:
                errors.append("provider_routing.allow_fallbacks must be false")
            if "max_price" in routing:
                prices = routing["max_price"]
                if not isinstance(prices, Mapping) or any(
                    key not in _PROVIDER_POLICY["max_price"] or value != 0
                    for key, value in prices.items()
                ):
                    errors.append("provider_routing.max_price must contain only zero limits")

    auxiliary = config.get("auxiliary")
    if auxiliary is not None:
        if not isinstance(auxiliary, Mapping):
            errors.append("auxiliary must be a mapping")
        else:
            if auxiliary.get("free_only") is not True:
                errors.append("auxiliary.free_only must be true")
            alias = auxiliary.get("openrouter_model")
            if alias is not None and alias != OPENROUTER_MODEL:
                errors.append("auxiliary.openrouter_model must be openrouter/free")
            for task, task_cfg in auxiliary.items():
                if task in {"free_only", "openrouter_model"}:
                    continue
                if not isinstance(task_cfg, Mapping):
                    continue
                try:
                    validate_route(
                        task_cfg.get("provider"), task_cfg.get("model"),
                        task_cfg.get("base_url", OPENROUTER_BASE_URL),
                        task_cfg.get("api_mode", "chat_completions"),
                    )
                except StrictFreeOnlyError as exc:
                    errors.append(f"auxiliary.{task}: {exc}")
                _check_fallbacks(task_cfg, f"auxiliary.{task}", errors)

    delegation = config.get("delegation")
    if isinstance(delegation, Mapping):
        _check_fallbacks(delegation, "delegation", errors)
    return tuple(errors)


def assert_config_safe(config: Any) -> None:
    """Raise if ``config`` contains an unsafe strict-mode route or fallback."""
    errors = validate_config(config)
    if errors:
        raise StrictFreeOnlyError("Unsafe strict free-only config: " + "; ".join(errors))


def enforce_agent_request(agent: Any, kwargs: Mapping[str, Any], *, client: Any = None) -> dict[str, Any]:
    """Final main-agent guard, including request-local SDK endpoint validation."""
    if not enabled():
        return dict(kwargs)
    request = enforce_request(
        kwargs, provider=getattr(agent, "provider", None), model=getattr(agent, "model", None),
        base_url=getattr(agent, "base_url", None), api_mode=getattr(agent, "api_mode", None),
    )
    if client is not None:
        validate_route(agent.provider, request["model"], getattr(client, "base_url", None), agent.api_mode)
    return request
