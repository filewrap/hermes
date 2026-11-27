"""Guest-history commands for GatewayRunner: clear the stored guest sessions.

Cohesive cluster lifted out of ``slash_commands_session`` (which sits at the
file-size cap): the guest-history clear arms a single-use confirm token, then
deletes every ``guest-<user_id>`` session in the profile — the same three
steps as a single session delete (store-level ``delete_session``, wakeup
cancel, session-store removal) plus evicting each deleted session's cached
agent, or its in-memory history would resurrect the deleted session on the
next guest turn through the cached-history lag guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from pathlib import Path

logger = logging.getLogger("gateway.run")


class GatewayGuestCommandsMixin:
    """Guest-history clear (armed by the GuestBot settings menu, owner-only)."""

    # ------------------------------------------------------- guest history clear

    def _guest_clear_confirms(self) -> dict:
        """Single-use guest-history-clear tokens (pruned like the delete tokens)."""
        state = getattr(self, "_guest_clear_confirm_tokens", None)
        if state is None:
            state = {}
            self._guest_clear_confirm_tokens = state
        now = time.monotonic()
        for token in [t for t, v in state.items() if now - v.get("created_at", 0) > 300]:
            del state[token]
        return state

    async def _list_guest_sessions(self) -> list[tuple[str, str, int]]:
        """``(session_id, session_key, message_count)`` for every guest session in this profile.

        A guest session is one whose thread is ``guest-<user_id>`` — the same
        predicate the guest turn path uses. The session key is carried along
        so the caller can evict the session's cached agent after deleting it.
        """
        from gateway.guest_history import is_guest_thread_id
        from persistence.conversation.sessions import SessionOrder, SessionQuery, SessionWindow

        store = getattr(self, "_session_db", None)
        if store is None:
            return []
        entries = await store.aio.sessions.list_sessions(
            SessionQuery(sources=("telegram",), session_key=None,
                         order=SessionOrder.LAST_ACTIVE),
            SessionWindow(limit=None))
        guests = []
        for entry in entries or []:
            session = getattr(entry, "session", None)
            if session is None:
                continue
            thread_id = getattr(session, "thread_id", None)
            session_key = str(getattr(session, "session_key", "") or "")
            if is_guest_thread_id(thread_id) or ":guest-" in session_key:
                guests.append((str(session.session_id), session_key,
                               int(getattr(session, "message_count", 0) or 0)))
        return guests

    async def arm_guest_history_clear(self, source) -> tuple[str, int, int] | None:
        """Arm a guest-history clear: ``(confirm_token, session_count, message_count)``.

        None when the store is unavailable. Zero counts arm fine — the
        confirm then reports nothing to clear.
        """
        if getattr(self, "_session_db", None) is None:
            return None
        guests = await self._list_guest_sessions()
        token = secrets.token_hex(8)
        self._guest_clear_confirms()[token] = {"created_at": time.monotonic()}
        return token, len(guests), sum(count for _sid, _key, count in guests)

    async def clear_guest_history_confirmed(self, source, confirm_token: str) -> dict | None:
        """Run an armed guest-history clear: delete every guest session in this profile.

        ``None`` for an unknown/expired token; otherwise ``{"deleted_sessions",
        "deleted_messages", "skipped"}``. A live session (``SessionWriteGuarded``)
        is skipped, never deleted. Each deleted session's cached agent is
        evicted, or its in-memory history would resurrect the deleted session
        on the next guest turn through the cached-history lag guard. Memories
        are untouched, like a single session delete.
        """
        from persistence import SessionWriteGuarded

        record = self._guest_clear_confirms().pop(str(confirm_token or ""), None)
        if record is None:
            return None
        sessions_dir = Path(self._resolve_profile_home_for_source(source)) / "sessions"
        from gateway.platforms.api_server_store import delete_session

        deleted_sessions = 0
        deleted_messages = 0
        skipped = 0
        for session_id, session_key, message_count in await self._list_guest_sessions():
            try:
                deleted = await asyncio.to_thread(
                    delete_session, self._session_db, session_id, sessions_dir)
            except SessionWriteGuarded:
                skipped += 1
                continue
            self._cancel_wakeups_for_session(session_id)
            if deleted:
                session_store = getattr(self, "session_store", None)
                if session_store is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(session_store.remove_by_session_id, session_id)
                with contextlib.suppress(Exception):
                    from gateway.session_ingress_media import collect_unheld_api_images

                    await asyncio.to_thread(collect_unheld_api_images, self._session_db)
                if session_key:
                    with contextlib.suppress(Exception):
                        self._evict_cached_agent(session_key)
                deleted_sessions += 1
                deleted_messages += message_count
        return {"deleted_sessions": deleted_sessions,
                "deleted_messages": deleted_messages, "skipped": skipped}
