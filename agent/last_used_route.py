"""Durable last-used (provider, model) route for the dangling-route rescue.

When the selected provider was removed but is still the route, the rescue in
``agent.chat_completion_helpers`` finishes the turn on a working provider.
The first catalog model is a safe default, but the route that just served a
turn is a better guess at what the user actually wants — so a successful turn
records its route here, and the rescue consults this cache before falling
back to the first working provider.

The file is profile-scoped through ``get_hermes_home()`` (never a hardcoded
``~/.hermes``) and written atomically; a missing or corrupt file reads as
"no cache", never as an error. The toggle lives here too because the rescue
is its only consumer: ``fallback.auto_fallback`` (default on) gates the
automatic rescue only — the explicit ``fallback_providers`` chain is
unaffected.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LAST_USED_ROUTE_FILENAME = "last_used_route.json"


def last_used_route_path(home: Path | str | None = None) -> Path:
    """Profile-home path of the last-used-route cache."""
    from hermes_constants import get_hermes_home

    return (Path(home) if home is not None else get_hermes_home()) / LAST_USED_ROUTE_FILENAME


def read_last_used_route(home: Path | str | None = None) -> tuple[str, str] | None:
    """Last recorded ``(provider, model)`` pair, or None when there is none.

    A missing file, corrupt JSON, a non-mapping payload, or a pair without a
    concrete provider/model (including the ``"auto"`` pseudo-provider) all
    read as "no cache".
    """
    try:
        data = json.loads(last_used_route_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    provider = str(data.get("provider") or "").strip().lower()
    model = str(data.get("model") or "").strip()
    if not provider or provider == "auto" or not model:
        return None
    return provider, model


def record_last_used_route(agent: Any, *, home: Path | str | None = None) -> bool:
    """Persist the route on ``agent`` as the last one that served a turn.

    Callers record only completed turns: recording at resolution time would
    let the failing primary overwrite the last good pair. Returns False when
    there is nothing worth recording (empty or ``"auto"`` route) or the write
    failed; a failed write never raises into the turn.
    """
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    model = str(getattr(agent, "model", "") or "").strip()
    if not provider or provider == "auto" or not model:
        return False
    try:
        from utils import atomic_json_write

        atomic_json_write(last_used_route_path(home), {"provider": provider, "model": model})
    except Exception:
        logger.debug("Could not record last-used route", exc_info=True)
        return False
    return True


def auto_fallback_enabled() -> bool:
    """Read ``fallback.auto_fallback`` from the merged config (default on).

    Unreadable config keeps the rescue on: the default preserves current
    behaviour, and a config read failure must not silently change routing.
    """
    try:
        from hermes_cli.config import load_config

        fallback = (load_config() or {}).get("fallback", {})
    except Exception:
        logger.debug("auto_fallback config read failed; keeping automatic rescue on", exc_info=True)
        return True
    if not isinstance(fallback, dict):
        return True
    return bool(fallback.get("auto_fallback", True))
