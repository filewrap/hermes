"""Guest history control: is this turn a guest turn, and may it be saved?

The gateway cannot import the Telegram plugin (plugins never touch core,
core never imports a specific bundled plugin), so the gateway owns its copy
of the ``guest-`` thread prefix — see
``plugins/platforms/telegram/guestbot.py`` (``_GUEST_THREAD_PREFIX``), the
same wire fact from the adapter side. Keep the two in sync: a guest turn's
source carries ``thread_id == "guest-<user_id>-<query_id>"`` — one thread
per mention, so every mention is its own turn.
"""
from __future__ import annotations

from typing import Any

from utils import is_truthy_value

GUEST_THREAD_PREFIX = "guest-"

# Config key (``platforms.telegram`` in the profile's config.yaml) for the
# "Save guest messages" toggle. Default True: today's behaviour (saves).
GUEST_SAVE_HISTORY_KEY = "guest_save_history"


def is_guest_thread_id(thread_id: Any) -> bool:
    """True when *thread_id* is a guest thread (``guest-<user_id>-<query_id>``)."""
    return str(thread_id or "").startswith(GUEST_THREAD_PREFIX)


def guest_history_saving_enabled(user_config: Any) -> bool:
    """Whether guest turns save their history for this profile's config.

    Reads ``platforms.telegram.guest_save_history`` from the raw config dict;
    missing, malformed or unset means True (today's behaviour — save).
    """
    if not isinstance(user_config, dict):
        return True
    platforms = user_config.get("platforms")
    if not isinstance(platforms, dict):
        return True
    telegram = platforms.get("telegram")
    if not isinstance(telegram, dict):
        return True
    value = telegram.get(GUEST_SAVE_HISTORY_KEY, True)
    if value is None:
        return True
    return is_truthy_value(value, default=True)


def is_ephemeral_guest_turn(source: Any, user_config: Any) -> bool:
    """True when this turn is a guest turn with history saving OFF.

    Such a turn is ephemeral: nothing is persisted (the agent's
    ``_persist_disabled`` flag), and its cached agent is dropped after the
    turn so the next guest turn cannot inherit its in-memory history.
    """
    return is_guest_thread_id(getattr(source, "thread_id", None)) and not guest_history_saving_enabled(user_config)
