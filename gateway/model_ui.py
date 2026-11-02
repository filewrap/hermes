"""Data-only presentation helpers for the gateway model dashboard and settings menus.

The Telegram/platform adapter owns native rendering and callback transport. This module keeps the
menu schema deterministic and testable without coupling it to a particular adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    page: str
    kind: str
    minimum: float
    maximum: float
    step: float | None = None


MODEL_SETTINGS: tuple[SettingField, ...] = (
    SettingField("temperature", "Temperature", "sampling", "float", 0.0, 2.0, 0.1),
    SettingField("top_p", "Top-p", "sampling", "float", 0.0, 1.0, 0.05),
    SettingField("frequency_penalty", "Frequency penalty", "sampling", "float", -2.0, 2.0, 0.1),
    SettingField("presence_penalty", "Presence penalty", "sampling", "float", -2.0, 2.0, 0.1),
    SettingField("max_tokens", "Max output tokens", "output", "int", 1, 1_000_000),
)
SETTINGS_PAGES: tuple[tuple[str, str], ...] = (
    ("sampling", "Sampling"), ("output", "Output"), ("capabilities", "Capabilities"),
)
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def parse_setting_value(key: str, raw: Any) -> float | int:
    """Validate a direct Hermes request setting, rejecting bools, NaN and out-of-range values."""
    field = next((item for item in MODEL_SETTINGS if item.key == key), None)
    if field is None:
        raise ValueError(f"Unsupported setting: {key}")
    if isinstance(raw, bool):
        raise ValueError(f"{field.label} must be a number")
    try:
        value = int(raw) if field.kind == "int" else float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        expected = "an integer" if field.kind == "int" else "a number"
        raise ValueError(f"{field.label} must be {expected}") from exc
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise ValueError(f"{field.label} must be finite")
    if not field.minimum <= value <= field.maximum:
        raise ValueError(f"{field.label} must be between {field.minimum:g} and {field.maximum:g}")
    return value


def dashboard_payload(*, current_model: str, current_provider: str,
                      providers: list[dict[str, Any]], settings: dict[str, Any],
                      reasoning_effort: str = "") -> dict[str, Any]:
    """Build the current-route dashboard payload; provider/model inventory must be live supplied data."""
    row = next((p for p in providers if str(p.get("slug", "")).casefold() == current_provider.casefold()), {})
    return {
        "view": "dashboard",
        "current_model": current_model or "unknown",
        "current_provider": current_provider or "unknown",
        "provider_name": str(row.get("name") or current_provider or "Unknown provider"),
        "provider_configured": bool(row.get("configured", False)),
        "model_catalog_count": int(row.get("total_models", 0) or 0),
        "reasoning_supported": row.get("reasoning_supported"),
        "reasoning_efforts": list(row.get("reasoning_efforts") or ()),
        "session_settings": dict(settings),
        "reasoning_effort": reasoning_effort or "provider default",
        "actions": [
            {"id": "providers", "label": "Providers", "command": "/model providers"},
            {"id": "models", "label": "List/change", "command": "/model list"},
            {"id": "settings", "label": "Model settings", "command": "/msettings page 1"},
            {"id": "close", "label": "Close", "style": "danger"},
        ],
    }


def settings_payload(*, settings: dict[str, Any], reasoning_effort: str = "",
                     page: str = "sampling", supported_reasoning_efforts: list[str] | None = None,
                     reasoning_supported: bool | None = None) -> dict[str, Any]:
    """Build a deterministic paged settings menu with an explicit Back action."""
    pages = {key: label for key, label in SETTINGS_PAGES}
    if page not in pages:
        page = "sampling"
    fields = [
        {"key": field.key, "label": field.label, "value": settings.get(field.key),
         "minimum": field.minimum, "maximum": field.maximum, "kind": field.kind,
         "set_command": f"/msettings set {field.key} <value>",
         "reset_command": f"/msettings reset {field.key}"}
        for field in MODEL_SETTINGS if field.page == page
    ]
    payload: dict[str, Any] = {
        "view": "settings", "page": page, "page_label": pages[page],
        "page_index": tuple(pages).index(page) + 1, "page_count": len(pages),
        "pages": [{"id": key, "label": label} for key, label in SETTINGS_PAGES],
        "page_actions": [{"id": key, "label": f"{index}. {label}", "command": f"/msettings page {index}"}
                         for index, (key, label) in enumerate(SETTINGS_PAGES, 1)],
        "fields": fields,
        "reasoning_effort": reasoning_effort or "provider default",
        "actions": [{"id": "back", "label": "Back", "command": "/model"},
                    {"id": "close", "label": "Close", "style": "danger"}],
    }
    if page == "capabilities":
        options = [str(value) for value in (supported_reasoning_efforts or REASONING_EFFORTS)]
        payload["reasoning_supported"] = reasoning_supported
        payload["reasoning_options"] = options if reasoning_supported is not False else []
        payload["reasoning_commands"] = [f"/msettings reasoning {effort}" for effort in payload["reasoning_options"]]
    return payload


def render_dashboard(payload: dict[str, Any]) -> str:
    """Plain-text fallback for transports without a native model dashboard."""
    settings = payload.get("session_settings") or {}
    settings_text = ", ".join(f"{key}={value}" for key, value in settings.items()) or "provider defaults"
    count = payload.get("model_catalog_count") or "not yet catalogued"
    reasoning = "supported" if payload.get("reasoning_supported") else "not confirmed"
    return (
        "Current model\n"
        f"Provider: {payload.get('provider_name')} (`{payload.get('current_provider')}`)\n"
        f"Model: `{payload.get('current_model')}`\n"
        f"Provider model catalog: {count} live entries · reasoning: {reasoning}\n"
        f"Session request settings: {settings_text}\n\n"
        "Providers: `/model providers`. Live model list/change: `/model list`.\n"
        "Model settings: use `/msettings`; this changes this conversation only.\n"
        "Close this dashboard by dismissing this message."
    )


def render_settings(payload: dict[str, Any]) -> str:
    lines = [f"Model settings — {payload['page_label']} ({payload['page_index']}/{payload['page_count']})"]
    for field in payload["fields"]:
        value = field["value"]
        lines.append(f"{field['label']}: {value if value is not None else 'provider default'}")
    if payload["page"] == "capabilities":
        lines.append(f"Reasoning effort: {payload['reasoning_effort']}")
        lines.append("Supported values: " + ", ".join(payload["reasoning_options"]))
    lines.append("Set with `/msettings set <setting> <value>`; use `/msettings reset <setting>` to restore provider defaults.")
    lines.append("`/msettings page <number>` changes page; Back returns to the model dashboard.")
    return "\n".join(lines)
