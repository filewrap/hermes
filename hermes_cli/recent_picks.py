"""Per-profile recent providers/models.

Recents are machine-maintained bookkeeping in the profile's ``config.yaml``,
top-level underscore-prefixed keys (like ``_left_core_scoped``): never user
settings, never in ``DEFAULT_CONFIG``, and the docs-drift checker skips
``_``-prefixed keys. Newest first, at most five entries each.

``record_recent_route`` is called by ``persist_model_selection`` on every
durable model switch, so every surface (CLI, gateway /model, Telegram
provider UI, providers tool) records the same way. Session-only overrides
never reach ``persist_model_selection`` and are not recorded: recents are
the profile's routes, not one user's ephemeral pick. A provider switch is
recorded with the model switch that carries it — there is no
provider-only switch operation; the route's provider is the recent
provider.
"""

from __future__ import annotations

from typing import Any

MAX_RECENT_PICKS = 5
RECENT_PROVIDERS_KEY = "_recent_providers"
RECENT_MODELS_KEY = "_recent_models"


def clean_recent_list(value: Any, *, fold: bool = False) -> list[str]:
    """String items of a config value, newest first, capped; [] when malformed.

    ``fold`` casefolds items (provider slugs); model ids keep their case.
    """
    if not isinstance(value, list):
        return []
    cleaned = [str(item or "").strip() for item in value]
    if fold:
        cleaned = [item.casefold() for item in cleaned]
    return list(dict.fromkeys(item for item in cleaned if item))[:MAX_RECENT_PICKS]


def load_recent_picks(config: Any) -> tuple[list[str], dict[str, list[str]]]:
    """(recent providers, recent models by provider) from a raw config dict."""
    if not isinstance(config, dict):
        return [], {}
    providers = clean_recent_list(config.get(RECENT_PROVIDERS_KEY), fold=True)
    models: dict[str, list[str]] = {}
    raw_models = config.get(RECENT_MODELS_KEY)
    if isinstance(raw_models, dict):
        for slug, names in raw_models.items():
            cleaned = clean_recent_list(names)
            if slug and cleaned:
                models[str(slug).strip().casefold()] = cleaned
    return providers, models


def record_recent_route(config: dict, provider: Any, model: Any) -> None:
    """Prepend (provider, model) to the recents in a raw config dict, in place.

    No-op when either is empty. Providers are stored casefolded; model ids
    keep their case. Each list is capped at ``MAX_RECENT_PICKS``.
    """
    slug = str(provider or "").strip().casefold()
    name = str(model or "").strip()
    if not slug or not name:
        return
    providers, models = load_recent_picks(config)
    providers = [slug, *[p for p in providers if p != slug]][:MAX_RECENT_PICKS]
    mine = [name, *[m for m in models.get(slug, []) if m != name]][:MAX_RECENT_PICKS]
    models[slug] = mine
    config[RECENT_PROVIDERS_KEY] = providers
    config[RECENT_MODELS_KEY] = models
