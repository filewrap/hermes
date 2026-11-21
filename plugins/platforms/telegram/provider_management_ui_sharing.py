"""Provider sharing actions for the Telegram provider-management UI.

Moved verbatim from ``provider_management_ui.py`` so that file stays within
its line budget: the sharing state lookup, the share toggle, the owner-only
revoke, and the share/revoke button rows + callback handling that render
them. No Telegram client state lives here — the functions take the UI state
dict and the provider dict, and run the credential writes off the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def provider_sharing(state: dict, provider: Optional[dict] = None) -> dict:
    """Sharing state for the acting user: can they share this provider, and is it shared?

    Only the user who added a credential can share it; the button is hidden
    for everyone else (and for providers with no owned entries). The state
    also carries the global definition's sharing (``shared_with_all``),
    whether the owner added it, and whether the acting user may revoke
    the sharing (the owner, when the provider is shared with all).
    """
    if provider is None:
        return {"can_share": False, "shared": False}
    actor = str(state.get("user_id") or "").strip()
    if not actor:
        return {"can_share": False, "shared": False}
    try:
        from gateway.provider_sharing import provider_sharing_state
        from hermes_cli.auth_commands import _normalize_provider

        # Resolve the UI slug to its pool key first: a keyed custom
        # provider arrives as ``custom:<key>`` but its credentials live
        # under the bare key.
        return provider_sharing_state(
            _normalize_provider(str(provider.get("slug") or "")), actor)
    except Exception:
        logger.debug("Provider sharing lookup failed; treating as unshared", exc_info=True)
        return {"can_share": False, "shared": False}


def provider_share_rows(state_id: str, state: dict, provider: dict) -> list:
    """Share/revoke button rows for a provider card.

    Owner-added definitions are global by definition: while shared
    they have no share toggle, only the owner's revoke. The adder's
    own toggle stays everywhere else — including an owner-added
    definition that was revoked, so the owner can share it again.
    """
    sharing = provider_sharing(state, provider)
    if sharing.get("can_share") and not (
        sharing.get("owner_added") and sharing.get("shared_with_all")
    ):
        if sharing.get("shared_with_all"):
            return [[("Stop sharing", f"pv:{state_id}:share-toggle", "danger")]]
        return [[("Share with all profiles", f"pv:{state_id}:share-toggle", "success")]]
    if sharing.get("can_revoke"):
        return [[("Revoke sharing", f"pv:{state_id}:share-revoke", "danger")]]
    return []


async def toggle_provider_sharing(state: dict, provider: dict) -> str:
    """Flip a provider's share flag for the user who added it.

    Anyone else — and a provider with no credential yet — gets a clear
    reason instead of a silent no-op; a failed write says so too.
    """
    sharing = provider_sharing(state, provider)
    actor = str(state.get("user_id") or "").strip()
    if not actor or not sharing.get("can_share"):
        if not sharing.get("has_entries"):
            return ("This provider has no credential to share yet. "
                    "Add a credential first, then share it with all profiles.")
        return "Only the user who added this provider can share it."
    new_shared = not bool(sharing.get("shared"))
    try:
        from hermes_cli.auth_commands import set_provider_shared_for_ui

        result = await asyncio.to_thread(
            set_provider_shared_for_ui, provider["slug"], actor, new_shared,
        )
        if isinstance(result, dict) and "shared" in result:
            new_shared = bool(result["shared"])
    except Exception:
        logger.warning("Could not update Telegram provider sharing", exc_info=True)
        return "Sharing could not be updated. Try again."
    return ("Provider shared with all profiles."
            if new_shared else "Sharing stopped. Only you can use this provider now.")


async def revoke_provider_sharing(state: dict, provider: dict) -> str:
    """Owner-only revoke: unshare a provider everywhere.

    The definition and every entry keep working for their adders; only
    the sharing goes. Anyone but the owner gets a clear reason instead
    of a silent no-op; a failed write says so too.
    """
    sharing = provider_sharing(state, provider)
    actor = str(state.get("user_id") or "").strip()
    if not actor or not sharing.get("can_revoke"):
        return "Only the owner can revoke sharing for this provider."
    try:
        from hermes_cli.auth_commands import revoke_provider_shared_for_ui

        await asyncio.to_thread(
            revoke_provider_shared_for_ui, provider["slug"], actor,
        )
    except Exception:
        logger.warning("Could not revoke Telegram provider sharing", exc_info=True)
        return "Sharing could not be revoked. Try again."
    return "Sharing revoked. This provider is no longer shared with all profiles."


async def handle_share_action(action: str, state: dict, provider: dict) -> str:
    """Notice for a share-toggle/share-revoke callback (raises when the provider is gone)."""
    if not provider:
        raise ValueError("Provider unavailable")
    if action == "share-revoke":
        return await revoke_provider_sharing(state, provider)
    return await toggle_provider_sharing(state, provider)
