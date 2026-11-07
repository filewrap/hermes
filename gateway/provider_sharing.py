"""Per-user provider sharing for multiuser profiles.

A credential added by one user is theirs alone until they share it. Entries
without ``added_by`` predate this (or came from host config / an external
adoption) and stay visible to everyone, so existing setups keep working.
Profile-level auth material (env vars, host chains) is not a user's
credential and is visible to all authorized users.
"""

from __future__ import annotations

from typing import Any


class ProviderNotSharedError(Exception):
    """Raised when a turn resolves to a provider the acting user may not use."""

    def __init__(self, provider: str):
        super().__init__(f"Provider {provider} was added by another user and is not shared")
        self.provider = provider


def _pool_entries(provider: str) -> list:
    """Pool entries under either spelling of a custom provider id.

    One endpoint arrives as ``custom:<key>`` in UI rows while a keyed
    ``providers:`` entry keeps its credentials under the bare key (and a
    legacy entry keeps them under ``custom:<name>``). A lookup under one
    spelling alone misses the other's entries, so sharing state and
    visibility consult both, deduplicated by pool key + entry id.
    """
    slug = str(provider or "").strip().lower()
    keys = [slug]
    if slug.startswith("custom:"):
        keys.append(slug.removeprefix("custom:"))
    elif slug:
        keys.append(f"custom:{slug}")
    entries: list = []
    seen: set = set()
    for key in keys:
        try:
            from agent.credential_pool import load_pool

            for entry in load_pool(key).entries():
                marker = (key, getattr(entry, "id", None))
                if marker not in seen:
                    seen.add(marker)
                    entries.append(entry)
        except Exception:
            continue
    return entries


def provider_sharing_state(provider: str, user_id: Any) -> dict:
    """Sharing state for the acting user: can they share it, and is it shared?

    ``can_share`` is True when the user added at least one entry for the
    provider; ``shared`` is True when every entry they added is shared.
    """
    user = str(user_id or "").strip()
    entries = _pool_entries(provider)
    own = [
        entry for entry in entries
        if str((getattr(entry, "extra", None) or {}).get("added_by") or "") == user
    ] if user else []
    if not own:
        return {"can_share": False, "shared": False, "has_entries": bool(entries)}
    shared = all(bool((getattr(entry, "extra", None) or {}).get("shared")) for entry in own)
    return {"can_share": True, "shared": shared, "has_entries": True}


def provider_visible_to_user(provider: str, user_id: Any) -> bool:
    """Whether a provider's credentials may be used by a user.

    Visible when any pool entry is visible to them (their own, shared, or
    legacy with no owner), or when the provider has no pool entries at all
    (env / host-chain material is profile-level, not a user's credential).
    """
    if user_id is None:
        return True
    user = str(user_id or "").strip()
    if not user:
        return True
    from agent.credential_pool import entry_visible_to

    entries = _pool_entries(provider)
    if not entries:
        return True
    return any(entry_visible_to(entry, user) for entry in entries)


def provider_has_visible_credentials(provider: str, user_id: Any) -> bool:
    """Whether the provider has any Hermes credential visible to the user."""
    return bool(_pool_entries(provider)) and provider_visible_to_user(provider, user_id)
