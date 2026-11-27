"""Model-picker filters for the Telegram provider-management UI.

Beta feature (``platforms.telegram.model_picker_filters``, off by default):
a Filter button on one provider's model list narrows the visible models to
free models or to a context-window bucket. The filter is scoped to the
provider currently open — it can only ever restrict that provider's list,
never show models from another provider.

The free filter reuses the same data as the green free markers
(``_is_free_model``); the context buckets resolve through the local
models.dev cache (``agent.models_dev.lookup_models_dev_context``, no
network), which is the published plugin seam for "context window for a
model".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from plugins.platforms.telegram.provider_management_ui_text import _is_free_model

logger = logging.getLogger(__name__)

_K = 1024

# (key, button label, catalog label), in the order the buttons render.
FILTERS: tuple[tuple[str, str, str], ...] = (
    ("free", "Free", "Free"),
    ("ctx-256k", "256k", "256k context"),
    ("ctx-512k", "512k", "512k context"),
    ("ctx-512k-plus", "512k+", "512k+ context"),
    ("ctx-1m", "1M", "1M context"),
    ("ctx-2m", "2M", "2M context"),
)

FILTER_KEYS = frozenset(key for key, _button_label, _catalog_label in FILTERS)


def bucket_for_context(context: Optional[int]) -> Optional[str]:
    """Context-window bucket for a model, or None when unknown.

    The buckets partition (0, infinity): 256k holds up to 256k tokens,
    512k up to 512k, 512k+ up to (not including) 1M, 1M up to (not
    including) 2M, 2M everything at or above 2M.
    """
    if context is None or context <= 0:
        return None
    if context <= 256 * _K:
        return "ctx-256k"
    if context <= 512 * _K:
        return "ctx-512k"
    if context < 1024 * _K:
        return "ctx-512k-plus"
    if context < 2048 * _K:
        return "ctx-1m"
    return "ctx-2m"


def filter_button_label(filter_key: str) -> str:
    """Short label for a filter key (filter buttons, Filter button)."""
    for key, button_label, _catalog_label in FILTERS:
        if key == filter_key:
            return button_label
    return ""


def filter_catalog_label(filter_key: str) -> str:
    """Catalog-text label for a filter key ("- Filter: 256k context")."""
    for key, _button_label, catalog_label in FILTERS:
        if key == filter_key:
            return catalog_label
    return ""


def _default_context(provider_slug: str, model_id: str) -> Optional[int]:
    """Context window from the local models.dev cache; None when unknown.

    No network: the lookup reads the disk cache only
    (``allow_network=False``), so a render loop stays cheap once the
    registry cache is warm. A lookup failure reads as "unknown", never
    as an error the picker has to surface.
    """
    try:
        from agent.models_dev import lookup_models_dev_context

        return lookup_models_dev_context(provider_slug, model_id, allow_network=False)
    except Exception:
        logger.debug("Could not read models.dev context for %s", model_id, exc_info=True)
        return None


def model_matches_filter(
    model_id: str, *, provider_slug: str, filter_key: Optional[str],
    context_fn: Optional[Callable[[str, str], Optional[int]]] = None,
) -> bool:
    """True when a model passes the active filter; no filter matches all.

    ``context_fn`` resolves (provider_slug, model_id) -> context tokens;
    the models.dev lookup is the default. Unknown filter keys match
    nothing — the callback only ever stores a validated key.
    """
    if filter_key is None:
        return True
    if filter_key == "free":
        return _is_free_model(model_id)
    if filter_key in FILTER_KEYS:
        resolve = context_fn or _default_context
        return bucket_for_context(resolve(provider_slug, model_id)) == filter_key
    return False


def active_model_filter(state: dict, provider_slug: str) -> Optional[str]:
    """The active model filter key, or None.

    A filter binds to the provider it was set on (``model_filter_provider``);
    opening another provider drops it by construction, so a filter can never
    leak across providers.
    """
    filter_key = state.get("model_filter")
    if filter_key not in FILTER_KEYS:
        return None
    if state.get("model_filter_provider") != provider_slug:
        return None
    return filter_key


def apply_model_filter(
    state: dict, provider: dict, models: list, filtered_indices: list, *,
    enabled: bool,
    context_fn: Optional[Callable[[str, str], Optional[int]]] = None,
) -> tuple[Optional[str], list]:
    """Restrict the search-matched indices to the active filter.

    Returns (active filter key or None, restricted indices). Beta off or
    no active filter returns the indices unchanged.
    """
    if not enabled:
        return None, filtered_indices
    active = active_model_filter(state, provider["slug"])
    if active is None:
        return None, filtered_indices
    return active, [
        index for index in filtered_indices
        if model_matches_filter(
            models[index], provider_slug=provider["slug"], filter_key=active,
            context_fn=context_fn,
        )
    ]


def model_filter_rows(
    state_id: int, *, panel_open: bool, active_key: Optional[str],
) -> list[list[tuple[str, str, Optional[str]]]]:
    """Filter button rows for the bottom of the models card.

    The Filter button toggles the panel; when open, the filter buttons
    render in rows of three with the active one marked primary, followed
    by a Clear filter row while a filter is active.
    """
    label = "Filter" if active_key is None else f"Filter: {filter_button_label(active_key)}"
    rows: list[list[tuple[str, str, Optional[str]]]] = [[
        (label, f"pv:{state_id}:model-filter", "primary" if panel_open else None),
    ]]
    if panel_open:
        button_row: list[tuple[str, str, Optional[str]]] = []
        for key, button_label, _catalog_label in FILTERS:
            button_row.append((
                button_label,
                f"pv:{state_id}:model-filter-value:{key}",
                "primary" if key == active_key else None,
            ))
            if len(button_row) == 3:
                rows.append(button_row)
                button_row = []
        if button_row:
            rows.append(button_row)
        if active_key is not None:
            rows.append([("Clear filter", f"pv:{state_id}:model-filter-value:clear", None)])
    return rows


def handle_model_filter_callback(
    state: dict, provider: Optional[dict], action: str, value: str,
) -> None:
    """Apply a model-filter callback to the card state.

    ``model-filter`` toggles the filter panel; ``model-filter-value:<key>``
    sets the filter (``clear`` or a repeat tap clears it). One filter at a
    time; a second tap replaces the first. Raises ValueError on misuse
    (wrong view, beta off, unknown key) — the caller renders the safe
    error card.
    """
    if state.get("view") != "models" or provider is None:
        raise ValueError("Model filters are only available on a provider model list")
    if action == "model-filter":
        state["show_model_filters"] = not state.get("show_model_filters", False)
        return
    if action != "model-filter-value":
        raise ValueError("unsupported model filter action")
    if not model_picker_filters_enabled(str(state.get("profile_home") or "")):
        raise ValueError("Model picker filters are off for this profile")
    selected = value.casefold()
    active = active_model_filter(state, provider["slug"])
    if selected == "clear" or selected == active:
        state["model_filter"] = None
        state.pop("model_filter_provider", None)
    elif selected in FILTER_KEYS:
        state["model_filter"] = selected
        state["model_filter_provider"] = provider["slug"]
        state["page"] = 0
    else:
        raise ValueError("unsupported model filter")


def model_picker_filters_enabled(home: str) -> bool:
    """Profile-wide ``platforms.telegram.model_picker_filters``; False when unset or unreadable.

    Same read as the other Telegram beta toggles (raw config.yaml, no
    merged-config dependency). Model picker filters is a Beta, off by
    default.
    """
    try:
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(home) / "config.yaml")
        platforms = config.get("platforms") if isinstance(config, dict) else None
        telegram = platforms.get("telegram") if isinstance(platforms, dict) else None
        if not isinstance(telegram, dict):
            return False
        return bool(telegram.get("model_picker_filters", False))
    except Exception:
        logger.debug("Could not read model-picker-filters setting", exc_info=True)
        return False
