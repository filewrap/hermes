"""Config-driven DM / group intake policy shared by the adapters that gate access themselves
(Weixin, WeCom, QQBot, WhatsApp, Yuanbao's ``AccessPolicy``).

Host contract — the adapter sets these on ``self`` before any predicate runs: ``_dm_policy`` /
``_group_policy`` (``open`` | ``allowlist`` | ``disabled`` | ``pairing``), ``_allow_from`` /
``_group_allow_from`` (iterable of allowlist entries). Platform-specific allowlist matching goes
through ``_entry_matches``; env-seeded allowlists that must be re-read live go through
``_live_dm_allow_from``.

Access is owner-or-allowlist only. There is no allow-all switch: a policy of ``"open"`` FAILS
CLOSED to the allowlist — it admits exactly the senders/chats the allowlist admits, never
everyone. ``"pairing"`` admits DMs only so the pairing handshake can run (the pairing gate
itself runs later); ``"disabled"`` and unknown policies admit nothing.

Every env read is profile-scoped and fails closed: under multiplexing ``os.environ`` holds the
DEFAULT profile's opt-in, which must never open a secondary bot's DMs (#93522).
"""

from __future__ import annotations

from typing import Iterable


class OwnAccessPolicyMixin:
    """Config-driven DM / group intake policy. ``"open"`` behaves as ``"allowlist"``."""

    @property
    def enforces_own_access_policy(self) -> bool:
        return True

    def _entry_matches(self, entries: Iterable[str], target: str) -> bool:
        return target in entries

    def _live_dm_allow_from(self) -> Iterable[str]:
        return self._allow_from

    def _is_dm_allowed(self, sender_id: str) -> bool:
        """Strict DM authorization — pairing does not imply access.

        ``"open"`` fails closed to the allowlist: it admits exactly the senders the
        allowlist admits, never everyone.
        """
        if self._dm_policy in {"allowlist", "open"}:
            return self._entry_matches(self._live_dm_allow_from(), sender_id)
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Whether a DM may reach gateway intake; ``pairing`` admits everyone with a principal so the
        handshake can run (the pairing gate itself runs later). A blank principal is never admitted."""
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        return self._dm_policy == "pairing" or self._is_dm_allowed(principal)

    def _is_group_allowed(self, chat_id: str, sender_id: str = "") -> bool:
        """``disabled``/``pairing``/unknown policies never forward group traffic at intake.

        ``"open"`` fails closed to the allowlist: it admits exactly the chats the group
        allowlist admits, never every chat.
        """
        if self._group_policy in {"allowlist", "open"}:
            return self._entry_matches(self._group_allow_from, chat_id)
        return False
