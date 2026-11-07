"""Canonical Mongo and Telegram owner bootstrap environment names.

The legacy aliases remain read-only inputs for profiles and deployments that
have not yet had an authorized configuration window to rename their variables.
"""
from __future__ import annotations

ENVIRONMENT_ALIASES = {
    "MONGODB_URI": "DEEP_MONGODB_URI",
    "CREDENTIAL_KEY": "DEEP_CREDENTIAL_KEY",
    "OWNER_TELEGRAM_ID": "DEEP_OWNER_TELEGRAM_ID",
    "MULTIUSER_ENABLED": "DEEP_MULTIUSER_ENABLED",
}

CANONICAL_BOOTSTRAP_KEYS = frozenset({
    *ENVIRONMENT_ALIASES,
    "MONGODB_DATABASE",
})
LEGACY_BOOTSTRAP_KEYS = frozenset(ENVIRONMENT_ALIASES.values())
BOOTSTRAP_KEYS = CANONICAL_BOOTSTRAP_KEYS | LEGACY_BOOTSTRAP_KEYS


def canonical_values(values: dict[str, str]) -> dict[str, str]:
    """Return recognized bootstrap settings with canonical keys and precedence.

    A present canonical value wins even when empty, allowing an operator to
    intentionally clear a legacy value during a staged configuration update.
    """
    result = {
        key: values[key]
        for key in CANONICAL_BOOTSTRAP_KEYS
        if key in values
    }
    for canonical, legacy in ENVIRONMENT_ALIASES.items():
        if canonical not in result and legacy in values:
            result[canonical] = values[legacy]
    return result


def legacy_config_secret_name(name: str) -> str | None:
    """Map a canonical CONFIG_* secret reference to its stored legacy name."""
    if name.startswith("CONFIG_"):
        return "DEEP_" + name
    if name.startswith("DEEP_CONFIG_"):
        return name
    return None
