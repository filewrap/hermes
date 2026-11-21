"""Short-TTL cache for the provider-inventory snapshot.

The /model, /provider and /models cards all funnel through
``GatewayRunner._provider_inventory_snapshot``, which re-reads the config,
the credential pool and the provider catalogs on every open — a few hundred
milliseconds warm, several seconds cold. The snapshot's inputs change rarely
(config edits, credential adds/removes, catalog refreshes), so a 60-second
cache with stale-serve background refresh covers repeated opens without
changing what the cards show.

Invalidation is by file signature, not just TTL: any change to a config,
auth store, .env or catalog cache under either the profile home or the
launch home makes the key miss, so an explicit catalog refresh or a newly
added credential shows on the next open, not after the TTL. Rows are
deep-copied on serve: the provider UI mutates rows in place (readiness
flags), so serving the stored object would corrupt the cache and leak
mutations between commands.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

PROVIDER_INVENTORY_CACHE_TTL_SECONDS = 60.0

# Files whose content feeds the snapshot, in each home that feeds it. A
# change to any of them makes a cached snapshot stale even inside the TTL.
_INVENTORY_INPUT_FILES = (
    "config.yaml",
    "auth.json",
    ".env",
    "models_dev_cache.json",
    "provider_models_cache.json",
)


def _file_signature(path: Path) -> Optional[tuple[int, int]]:
    """(mtime_ns, size) for *path*, or None when it is missing/unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


class ProviderInventoryCache:
    """Per-runner cache of provider-inventory snapshots.

    One instance lives on the gateway runner (never module-level: one
    process serves many profiles, and tests isolate homes per file).
    """

    def __init__(self, ttl_seconds: float = PROVIDER_INVENTORY_CACHE_TTL_SECONDS):
        self._ttl = float(ttl_seconds)
        self._entries: dict[tuple[Any, ...], tuple[float, tuple[Any, ...]]] = {}
        self._refreshing: set[tuple[Any, ...]] = set()

    def key_for(
        self,
        *,
        profile_home: Any,
        launch_home: Any,
        session_key: str,
        override: Optional[dict[str, Any]],
        user_id: Any,
    ) -> tuple[Any, ...]:
        """Cache key: where the snapshot runs, for whom, and the signatures
        of every file that feeds it."""
        signatures: list[Optional[tuple[int, int]]] = []
        for home in (profile_home, launch_home):
            if home is None:
                continue
            home_path = Path(home)
            for name in _INVENTORY_INPUT_FILES:
                signatures.append(_file_signature(home_path / name))
        override_fingerprint = json.dumps(override or {}, sort_keys=True, default=str)
        return (
            str(profile_home or ""),
            str(user_id or ""),
            str(session_key or ""),
            override_fingerprint,
            tuple(signatures),
        )

    def get(self, key: tuple[Any, ...]) -> tuple[Optional[str], Optional[tuple[Any, ...]]]:
        """Return ("fresh"|"stale", value), or (None, None) on a miss."""
        entry = self._entries.get(key)
        if entry is None:
            return None, None
        stored_at, value = entry
        if time.monotonic() - stored_at < self._ttl:
            return "fresh", value
        return "stale", value

    def put(self, key: tuple[Any, ...], value: tuple[Any, ...]) -> None:
        self._entries[key] = (time.monotonic(), value)

    @staticmethod
    def copy(value: tuple[Any, ...]) -> tuple[Any, ...]:
        """Deep copy for serve/store: callers mutate snapshot rows in place."""
        return copy.deepcopy(value)

    def refresh_in_background(
        self,
        key: tuple[Any, ...],
        source: Any,
        rebuild: Callable[..., Awaitable[tuple[Any, ...]]],
    ) -> bool:
        """Schedule a rebuild of a stale key; False when one is already running
        or no event loop is available (the next open then rebuilds inline)."""
        if key in self._refreshing:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._refreshing.add(key)
        task = loop.create_task(self._refresh(key, source, rebuild))
        task.add_done_callback(lambda _task: self._refreshing.discard(key))
        return True

    async def _refresh(
        self,
        key: tuple[Any, ...],
        source: Any,
        rebuild: Callable[..., Awaitable[tuple[Any, ...]]],
    ) -> None:
        try:
            await rebuild(source, force_rebuild=True)
        except Exception as exc:  # a background refresh must never surface into a turn
            logger.debug("Provider inventory background refresh failed: %s", exc)
