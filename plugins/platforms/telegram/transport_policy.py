"""The Telegram runtime is pinned to one audited Python MTProto client."""
from __future__ import annotations

PINNED_TELETHON_VERSION = "1.45.0"


def require_pinned_telethon() -> None:
    import telethon

    if telethon.__version__ != PINNED_TELETHON_VERSION:
        raise RuntimeError(
            f"Telegram requires Telethon=={PINNED_TELETHON_VERSION} (Python MTProto); "
            f"installed version is {telethon.__version__}. Reinstall the project's pinned "
            "messaging dependencies. No alternative Telegram transport is available."
        )
