"""HF-only configuration defaults and fail-closed validation; never store credentials."""
from __future__ import annotations

from copy import deepcopy

MODEL = "openrouter/free"
BASE_URL = "https://openrouter.ai/api/v1"
ROUTING = {
    "allow_fallbacks": False,
    "max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0},
}


def fresh_config() -> dict:
    # Read the pinned source's task list, so new auxiliary tasks are not silently omitted.
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    auxiliary = {"free_only": True, "openrouter_model": MODEL}
    for name, defaults in DEFAULT_CONFIG["auxiliary"].items():
        if isinstance(defaults, dict) and "provider" in defaults and "model" in defaults:
            auxiliary[name] = {"provider": "openrouter", "model": MODEL,
                               "base_url": BASE_URL}
    return {
        "model": {"default": MODEL, "provider": "openrouter", "base_url": BASE_URL,
                  "api_mode": "chat_completions"},
        "fallback_providers": [],
        "provider_routing": deepcopy(ROUTING),
        "auxiliary": auxiliary,
        "delegation": {"provider": "openrouter", "model": MODEL,
                       "base_url": BASE_URL, "fallback_providers": []},
        "terminal": {"backend": "local"},
    }


def validate_config(config: object) -> None:
    """Reject unsafe routing without rewriting an existing/restored user's file.

    Request-time guards remain authoritative, including defaults, environment,
    session overrides and config edits made after this startup check.
    """
    if not isinstance(config, dict):
        raise ValueError("HF free-only policy requires a mapping config")
    model = config.get("model")
    if not isinstance(model, dict) or model.get("default") != MODEL or model.get("provider") != "openrouter":
        raise ValueError("HF free-only policy requires model.default=openrouter/free and model.provider=openrouter; existing config preserved")

    def walk(node: object, path: str = "config") -> None:
        if isinstance(node, list):
            for value in node:
                walk(value, path)
        elif isinstance(node, dict):
            for key, value in node.items():
                where = f"{path}.{key}"
                if key in {"fallback_providers", "fallback_models", "models", "plugins"} and value:
                    raise ValueError(f"HF free-only policy blocks {where}; existing config preserved")
                if key in {"provider", "default_provider"} and isinstance(value, str) and value not in {"", "auto", "openrouter"}:
                    raise ValueError(f"HF free-only policy blocks alternate provider at {where}")
                if (key in {"model", "openrouter_model"} or (key == "default" and path == "config.model")) and isinstance(value, str) and value not in {"", MODEL}:
                    raise ValueError(f"HF free-only policy blocks alternate model at {where}")
                if key in {"base_url", "api_base"} and value and value != BASE_URL:
                    raise ValueError(f"HF free-only policy blocks alternate endpoint at {where}")
                if key == "api_mode" and value not in {"", "chat_completions", None}:
                    raise ValueError(f"HF free-only policy blocks alternate transport at {where}")
                if key == "free_only" and value is not True:
                    raise ValueError(f"HF free-only policy cannot be disabled at {where}")
                if key == "allow_fallbacks" and value is not False:
                    raise ValueError(f"HF free-only policy blocks provider fallbacks at {where}")
                if key == "max_price" and (not isinstance(value, dict) or any(type(v) not in {int, float} or v != 0 for v in value.values())):
                    raise ValueError(f"HF free-only policy blocks nonzero price cap at {where}")
                walk(value, where)

    walk(config)
