"""Profile-scoped Telegram Business Mode settings (``config.yaml``).

The ONE reader/writer the business UI and the ``telegram_business`` tool
share. Two namespaces feed it, in this order (later wins):

1. the declared defaults (``hermes_cli.config_defaults`` top-level
   ``telegram.business`` — the config registry's copy of the design's
   section-10 block);
2. the merged config's top-level ``telegram.business`` block (where the
   declared defaults land, plus anything the owner wrote there by hand);
3. the merged config's ``platforms.telegram.business`` block (the platform
   namespace the design names, and where the settings UI persists changes
   — the same split ``autonomous_mode`` lives in).

Reads go through the merged config (``load_config_readonly()``, the
adapter's read path); when that loader is unavailable the raw file at the
given home is read instead. Writes go through the ONE writer seam,
``hermes_cli.config.atomic_config_write``, into
``platforms.telegram.business``; the full current file is written back, so
nothing is ever deleted by omission.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .business_manager import DEFAULT_PERSONA

logger = logging.getLogger(__name__)

BUSINESS_SETTING_KEYS = (
    "enabled",
    "trigger_mode",
    "trigger_word",
    "debounce_seconds",
    "max_customer_text_chars",
    "owner_persona",
)

BUSINESS_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trigger_mode": "direct",
    "trigger_word": "",
    "debounce_seconds": 8,
    "max_customer_text_chars": 4000,
    "owner_persona": DEFAULT_PERSONA,
}

_TRIGGER_MODES = ("direct", "trigger")


def _coerce_business_value(key: str, value: Any) -> Any:
    """Validate one business setting; raise ValueError on a bad value."""
    if key == "enabled":
        if type(value) is not bool:
            raise ValueError("business enabled must be true or false")
        return value
    if key == "trigger_mode":
        mode = str(value).strip().lower() if isinstance(value, str) else ""
        if mode not in _TRIGGER_MODES:
            raise ValueError("business trigger_mode must be direct or trigger")
        return mode
    if key == "trigger_word":
        if not isinstance(value, str):
            raise ValueError("business trigger_word must be a single word")
        word = value.strip()
        if word and any(ch.isspace() for ch in word):
            raise ValueError("business trigger_word must be a single word")
        return word
    if key == "debounce_seconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("business debounce_seconds must be a number")
        if value < 0:
            raise ValueError("business debounce_seconds must not be negative")
        return value
    if key == "max_customer_text_chars":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("business max_customer_text_chars must be a whole number")
        if value <= 0:
            raise ValueError("business max_customer_text_chars must be positive")
        return value
    if key == "owner_persona":
        if not isinstance(value, str):
            raise ValueError("business owner_persona must be text")
        return value
    raise ValueError(f"Unknown business setting: {key}")


def _overlay(settings: dict[str, Any], node: Any) -> None:
    """Fold one config mapping over ``settings``; bad values keep the default."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key not in BUSINESS_DEFAULTS:
            continue
        try:
            settings[key] = _coerce_business_value(key, value)
        except ValueError:
            logger.debug("Ignoring invalid business setting %s=%r", key, value)


def _node_at(config: Any, *path: str) -> Any:
    node = config
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def load_business_settings(home: str | Path) -> dict[str, Any]:
    """Business settings for ``home``: merged config over the section-10 defaults."""
    settings = dict(BUSINESS_DEFAULTS)
    merged: Any = None
    try:
        from hermes_cli.config import load_config_readonly

        merged = load_config_readonly()
    except Exception:
        logger.debug("Business settings: merged config unreadable, reading raw file", exc_info=True)
    if isinstance(merged, dict):
        _overlay(settings, _node_at(merged, "telegram", "business"))
        _overlay(settings, _node_at(merged, "platforms", "telegram", "business"))
        return settings
    try:
        from hermes_cli.config import read_user_config_raw

        raw = read_user_config_raw(Path(home) / "config.yaml")
    except Exception:
        logger.debug("Business settings: raw config unreadable", exc_info=True)
        return settings
    _overlay(settings, _node_at(raw, "telegram", "business"))
    _overlay(settings, _node_at(raw, "platforms", "telegram", "business"))
    return settings


def save_business_settings(home: str | Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Persist ``changes`` under ``platforms.telegram.business``; return the merged settings.

    Unknown keys and invalid values raise ValueError and write nothing.
    The full current config is written back through the atomic writer, so
    unrelated settings are never deleted by omission.
    """
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Business settings need at least one change")
    unknown = sorted(set(changes) - set(BUSINESS_DEFAULTS))
    if unknown:
        raise ValueError(f"Unknown business setting: {', '.join(unknown)}")
    validated = {key: _coerce_business_value(key, value) for key, value in changes.items()}
    from hermes_cli.config import atomic_config_write, require_readable_config_before_write

    path = Path(home) / "config.yaml"
    config = require_readable_config_before_write(path)
    if not isinstance(config, dict):
        raise ValueError("The active config.yaml is not a mapping")
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        raise ValueError("The platforms section of config.yaml is not a mapping")
    telegram = platforms.setdefault("telegram", {})
    if not isinstance(telegram, dict):
        raise ValueError("The Telegram section of config.yaml is not a mapping")
    business = telegram.setdefault("business", {})
    if not isinstance(business, dict):
        raise ValueError("The Telegram business section is not a mapping")
    business.update(validated)
    atomic_config_write(path, config)
    return load_business_settings(home)
