"""Music Generation Provider Registry.

Populated by plugins via ``PluginContext.register_music_gen_provider()``;
consumed by the ``music_generate`` tool (``tools/music_generation_tool.py``).
The active provider is ``music_gen.provider`` from ``config.yaml``; a configured-but-unregistered name
fails closed. If unset, the single *available* registered provider is used
(mirrors ``agent/video_gen_registry.py``) so a box with credentials for only
one backend auto-selects it; otherwise None and the tool points the user at
``hermes tools``.

Five providers ship in-tree under ``plugins/music_gen/<name>/`` (fal, minimax,
google, openrouter, comfy) — see
``website/docs/developer-guide/music-gen-provider-plugin.md``.
"""

from __future__ import annotations

import logging
from typing import Optional

from agent.music_gen_provider import MusicGenProvider
from agent.provider_registry import ProviderRegistry, configured_provider_name, is_available_safe

logger = logging.getLogger(__name__)


_registry: ProviderRegistry[MusicGenProvider] = ProviderRegistry(
    label="Music gen", provider_cls=MusicGenProvider, logger=logger,
)
_registry.export(globals())


def get_active_provider() -> Optional[MusicGenProvider]:
    """Resolve the currently-active provider (see module docstring)."""
    configured = configured_provider_name("music_gen", logger)
    snapshot = _registry.merged()

    if configured:
        provider = snapshot.get(configured)
        if provider is None:
            logger.debug(
                "music_gen.provider='%s' configured but not registered; failing closed", configured
            )
        return provider

    available = [
        p for p in snapshot.values()
        if is_available_safe(p, logger, "music_gen provider %s.is_available() raised %s")
    ]
    return available[0] if len(available) == 1 else None
