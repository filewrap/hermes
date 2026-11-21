"""Shutdown/restart notice targets for GatewayRunner.

Split out of ``gateway.run_shutdown`` (file-size ratchet); the names stay importable from the
facade so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Optional


def _notice_target_key(platform_value: str, chat_id, thread_id) -> tuple:
    """Dedup key for one notice destination: thread/topic platforms share a chat but route apart."""
    return (platform_value, str(chat_id), str(thread_id) if thread_id else None)


def _telegram_restart_notice_path(profile_home: Path) -> Path:
    """Profile-local, private state file containing only Telegram chat/thread/message IDs."""
    state_dir = Path(profile_home) / "state" / "telegram_restart_notices"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        state_dir.chmod(0o700)
    return state_dir / "targets.json"


def _telegram_restart_notice_key(chat_id: str, thread_id: Optional[str]) -> str:
    """Opaque stable key for a chat/topic pair; avoid raw chat IDs in JSON keys."""
    identity = json.dumps([str(chat_id), str(thread_id) if thread_id else None], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _telegram_restart_notice_markdown(*, restarting: bool, updated_at: str) -> str:
    """Build a compact operational Rich Message; the UTC timestamp stays natively copyable."""
    if restarting:
        title = "# Gateway restarting"
        state = "**State:** Restart in progress"
        lifecycle = "After Telegram reconnects, send your message again."
    else:
        title = "# Gateway shutting down"
        state = "**State:** Shutting down"
        lifecycle = (
            "Whether it returns automatically depends on how this gateway is managed. "
            "If it returns, wait for Telegram to reconnect before sending again."
        )
    return (
        f"{title}\n\n"
        f"{state}\n"
        f"Updated: `{updated_at}` UTC\n\n"
        "> An active task may be interrupted.\n\n"
        f"{lifecycle} An interrupted tool action may not resume; restate anything important."
    )


def _delivery_target_key(platform_value: str, chat_id, thread_id, *, profile: Optional[str] = None) -> tuple:
    """Dedupe key for one DELIVERED chat: profile-independent, except Telegram private chats.

    Two served profiles can share one home chat (one Telegram group for the whole host) and owe it
    ONE notice per host restart. A positive Telegram chat id names the USER, though: the same id
    under two bot tokens is two conversations, so those stay keyed per served profile (#118233).
    """
    from gateway.delivery import looks_like_telegram_private_chat_id
    if (profile and profile != "default" and platform_value == "telegram"
            and looks_like_telegram_private_chat_id(chat_id)):
        platform_value = f"{profile}:{platform_value}"
    return _notice_target_key(platform_value, chat_id, thread_id)
