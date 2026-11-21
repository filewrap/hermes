"""Acting-user credential selection for the credential pool.

Gateway turns run as one Telegram user: the pool a turn spends must never
hand out another user's unshared key. The turn binds the acting user (and
the launch-home owner id) in a contextvar; pools loaded inside the turn pick
it up lazily at selection time, and ``_available_entries`` filters + orders
by it — the single choke point every selection path funnels through. Outside
a turn (CLI, cron, system callers) no user is bound and selection keeps its
legacy order: the caller there IS the owner.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent.credential_pool import PooledCredential

logger = logging.getLogger(__name__)

_ACTING_USER_CONTEXT: ContextVar[Optional[tuple[str, Optional[str]]]] = ContextVar(
    "credential_pool_acting_user", default=None)


@contextmanager
def bind_acting_user_context(user_id: Any, owner_id: Any = None):
    """Bind the acting user for credential selection in this context.

    ``user_id`` is the Telegram user id the turn runs as; ``owner_id`` is the
    launch-home owner id (None when unresolvable — the owner tier then simply
    never matches). No user bound means "system caller sees everything".
    """
    user = str(user_id or "").strip()
    if not user:
        yield
        return
    owner = str(owner_id or "").strip() or None
    token = _ACTING_USER_CONTEXT.set((user, owner))
    try:
        yield
    finally:
        _ACTING_USER_CONTEXT.reset(token)


def entry_visible_to(entry: Any, user_id: Any) -> bool:
    """Whether a pool entry may be used by a user.

    A credential added by a user is theirs alone until they share it; a
    credential with no ``added_by`` predates per-user sharing (or came from
    host config / an external adoption) and stays visible to everyone.
    ``user_id`` is a Telegram user id (or None for system/CLI callers, which
    see everything).
    """
    if user_id is None:
        return True
    extra = getattr(entry, "extra", None)
    if not isinstance(extra, dict):
        return True
    added_by = extra.get("added_by")
    if added_by in (None, ""):
        return True
    if str(added_by) == str(user_id):
        return True
    return bool(extra.get("shared"))


def _definition_shared_for_provider(provider: str) -> bool:
    """Whether the global definition for *provider* is shared with all profiles."""
    try:
        from hermes_cli.config_providers import global_provider_definition

        definition = global_provider_definition(provider)
    except Exception:
        logger.debug("Global definition lookup failed; treating %s as unshared", provider, exc_info=True)
        return False
    return bool(isinstance(definition, dict) and definition.get("shared"))


class CredentialPoolUserSelectionMixin:
    """Acting-user selection for ``CredentialPool`` (see the module docstring).

    Class-level defaults: no acting user means a system caller, legacy order,
    everything visible. Selection stamps the live context on first use (and
    re-stamps when the turn's user changes).
    """

    _acting_user_id: Optional[str] = None
    _owner_user_id: Optional[str] = None
    _definition_shared: bool = False

    def _bind_live_acting_user(self) -> None:
        """Stamp the acting user from the live context when it is missing or changed.

        Pools loaded outside a turn (or before it bound) still select for the
        turn's user once they run inside it; the turn's user is the authority
        for spending, so a live context that differs from the stamped one
        re-stamps. The global-definition lookup runs at most once per
        (pool, user) — never per selection.
        """
        ctx = _ACTING_USER_CONTEXT.get()
        if ctx is None:
            return
        user, owner = ctx
        if self._acting_user_id == user:
            return
        self._acting_user_id = user
        self._owner_user_id = owner
        self._definition_shared = _definition_shared_for_provider(self.provider)

    def _entry_usable_by_user(self, entry: PooledCredential) -> bool:
        """Whether the acting user may spend *entry* (the peek fast-path check)."""
        self._bind_live_acting_user()
        user = self._acting_user_id
        if user is None:
            return True
        return entry_visible_to(entry, user) or self._definition_shared

    def _usable_entries_for_user(
        self, entries: list[PooledCredential],
    ) -> list[PooledCredential]:
        """Entries the acting user may spend, best first.

        No acting user: everything, in pool order. Otherwise only entries the
        user may use — their own, shared, legacy (no owner), or any entry
        when the provider's global definition is shared (a shared definition
        IS the whole provider with full access) — ordered owner first, then
        the acting user's own, then shared, then legacy, then other users'
        unshared entries (reachable only via a shared definition). Stable
        inside a tier, so priority order survives.
        """
        self._bind_live_acting_user()
        user = self._acting_user_id
        if user is None:
            return list(entries)
        owner = self._owner_user_id
        definition_shared = self._definition_shared

        def _tier(entry: PooledCredential) -> int:
            extra = getattr(entry, "extra", None) or {}
            added_by = str(extra.get("added_by") or "")
            if owner and added_by and added_by == owner:
                return 0
            if added_by and added_by == user:
                return 1
            if bool(extra.get("shared")):
                return 2
            if not added_by:
                return 3
            return 4

        usable = [
            entry for entry in entries
            if entry_visible_to(entry, user) or definition_shared
        ]
        usable.sort(key=_tier)
        return usable
