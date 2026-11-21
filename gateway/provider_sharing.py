"""Per-user provider sharing for multiuser profiles, globalized.

A credential added by one user is theirs alone until they share it. Entries
without ``added_by`` predate this (or came from host config / an external
adoption) and stay visible to everyone, so existing setups keep working.
Profile-level auth material (env vars, host chains) is not a user's
credential and is visible to all authorized users.

Global provider sync: the launch home's config.yaml holds the one provider
definition store. A definition carries ``added_by`` (who added it) and
``shared`` (visible in every profile). Visibility is the definition grant
OR the pool grant: a definition grants when its adder is the acting user or
it is shared; pool entries grant per the existing per-entry rule (the
migration clause — an entry shared the old by-user way still opens the
provider to everyone). A definition with no ``added_by`` grants nothing by
itself; its entries decide.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


def global_provider_definition(provider: str) -> dict | None:
    """The global (launch-home) definition for *provider*, or None."""
    from hermes_cli.config_providers import global_provider_definition as _lookup

    return _lookup(provider)


def definition_visible_to_user(provider: str, user_id: Any) -> bool:
    """Whether the global definition alone grants *user_id* the provider.

    The adder always sees their own definition; a shared definition is
    visible in every profile. A legacy definition (no ``added_by``) grants
    nothing by itself — its pool entries decide (see
    :func:`provider_visible_to_user`). No definition at all grants nothing.
    """
    if user_id is None:
        return True
    user = str(user_id or "").strip()
    if not user:
        return True
    definition = global_provider_definition(provider)
    if not isinstance(definition, dict):
        return False
    added_by = str(definition.get("added_by") or "").strip()
    if added_by and added_by == user:
        return True
    return bool(definition.get("shared"))


def _launch_owner_id() -> str | None:
    """Launch-home owner id, or None when multiuser is off / unresolvable."""
    try:
        from gateway.mongo_multiuser import enabled, owner_user_id
        from hermes_cli.config_providers import global_provider_home

        home = global_provider_home()
        if not enabled(home):
            return None
        return owner_user_id(home)
    except Exception:
        logger.debug("Launch-home owner id unresolvable", exc_info=True)
        return None


def is_owner_user_id(user_id: Any, home: Any = None) -> bool:
    """True when *user_id* is the launch-home owner.

    No owner distinction exists unless multiuser is enabled for the launch
    home (``OWNER_TELEGRAM_ID`` set): there every admitted user is treated
    as the owner, matching ``_is_owner_actor``. Under multiuser the
    configured owner id decides; an undeterminable owner fails closed.
    """
    user = str(user_id or "").strip()
    if not user:
        return False
    try:
        from gateway.mongo_multiuser import enabled, owner_user_id

        if home is None:
            from hermes_cli.config_providers import global_provider_home

            home = global_provider_home()
        home = Path(home)
        if not enabled(home):
            return True
        return owner_user_id(home) == user
    except Exception:
        logger.debug("Owner check failed; failing closed", exc_info=True)
        return False


@contextmanager
def acting_user_scope(source: Any):
    """Bind the acting user of a gateway turn for credential selection.

    The user id comes from the source (``user_id``); the owner id from the
    launch home, fail-open None when unresolvable. A source with no user id
    binds nothing — selection keeps its legacy order.
    """
    from agent.credential_pool_user_selection import bind_acting_user_context

    user_id = getattr(source, "user_id", None)
    owner_id = _launch_owner_id()
    with bind_acting_user_context(user_id, owner_id):
        yield


def provider_sharing_state(provider: str, user_id: Any) -> dict:
    """Sharing state for the acting user: can they share it, and is it shared?

    ``can_share`` is True when the user added at least one entry for the
    provider; ``shared`` is True when every entry they added is shared.
    ``definition_*`` describe the global definition: who added it, whether
    it is shared with every profile, and whether the acting user may revoke
    that sharing (the owner, when the provider is shared with all).
    """
    user = str(user_id or "").strip()
    entries = _pool_entries(provider)
    own = [
        entry for entry in entries
        if str((getattr(entry, "extra", None) or {}).get("added_by") or "") == user
    ] if user else []
    definition = global_provider_definition(provider)
    definition_added_by = (
        str(definition.get("added_by") or "").strip()
        if isinstance(definition, dict) else ""
    )
    definition_shared = bool(isinstance(definition, dict) and definition.get("shared"))
    own_shared = bool(own) and all(
        bool((getattr(entry, "extra", None) or {}).get("shared")) for entry in own
    )
    shared_with_all = definition_shared or own_shared
    owner = _launch_owner_id()
    owner_added = bool(definition_added_by and owner and definition_added_by == owner)
    if not owner_added and definition_added_by == user:
        # No owner id on file (single-user install): the acting user is the
        # owner, so a definition they added is owner-added.
        owner_added = is_owner_user_id(user)
    return {
        "can_share": bool(own),
        "shared": own_shared,
        "has_entries": bool(entries),
        "definition_added_by": definition_added_by,
        "definition_shared": definition_shared,
        "shared_with_all": shared_with_all,
        "owner_added": owner_added,
        "can_revoke": shared_with_all and bool(user) and is_owner_user_id(user),
    }


def provider_visible_to_user(provider: str, user_id: Any) -> bool:
    """Whether a provider's credentials may be used by a user.

    The global definition grants (adder, or shared with every profile) or
    any pool entry grants (their own, shared, or legacy with no owner — the
    migration clause for shares made the old by-user way). With no pool
    entries at all the provider is visible (env / host-chain material is
    profile-level, not a user's credential). A definition with no adder
    grants nothing by itself, so a legacy definition plus a private entry
    stays hidden rather than visible-but-unusable.
    """
    if user_id is None:
        return True
    user = str(user_id or "").strip()
    if not user:
        return True
    if definition_visible_to_user(provider, user):
        return True
    from agent.credential_pool_user_selection import entry_visible_to

    entries = _pool_entries(provider)
    if not entries:
        return True
    return any(entry_visible_to(entry, user) for entry in entries)


def provider_has_visible_credentials(provider: str, user_id: Any) -> bool:
    """Whether the provider has any Hermes credential visible to the user."""
    return bool(_pool_entries(provider)) and provider_visible_to_user(provider, user_id)


def _provider_entry_slug(entry: dict, stored_key: str = "") -> str:
    """Bare slug for one stored definition: stored key, provider_key, or name slug.

    The ``custom:`` prefix is spelling, not identity — a keyed ``providers:``
    entry ``acme`` and a legacy ``custom_providers`` entry named ``acme``
    are the same endpoint.
    """
    from hermes_cli.providers import custom_provider_slug

    for raw in (stored_key, entry.get("provider_key")):
        slug = str(raw or "").strip().lower().removeprefix("custom:")
        if slug:
            return slug
    name = str(entry.get("name") or "").strip()
    if name:
        return custom_provider_slug(
            name, str(entry.get("provider_key") or ""),
        ).strip().lower().removeprefix("custom:")
    return ""


def _provider_entry_url(entry: dict) -> str:
    """Normalized base_url for one stored definition, or ``""``."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("base_url") or "").strip().rstrip("/").lower()


def _global_provider_identities(global_cfg: dict) -> tuple[set[str], set[str]]:
    """Bare slugs + base_urls of every definition in the global store."""
    slugs: set[str] = set()
    urls: set[str] = set()
    global_providers = global_cfg.get("providers")
    global_custom = global_cfg.get("custom_providers")
    entries: list[tuple[str, dict]] = []
    if isinstance(global_providers, dict):
        entries.extend(
            (str(key), entry) for key, entry in global_providers.items()
            if isinstance(entry, dict)
        )
    if isinstance(global_custom, list):
        entries.extend(("", entry) for entry in global_custom if isinstance(entry, dict))
    for stored_key, entry in entries:
        slug = _provider_entry_slug(entry, stored_key)
        if slug:
            slugs.add(slug)
        url = _provider_entry_url(entry)
        if url:
            urls.add(url)
    return slugs, urls


def union_provider_definitions(profile_cfg: dict, global_cfg: dict) -> dict:
    """Union provider definitions: profile's own + the global store.

    Returns a NEW mapping with ``providers`` and ``custom_providers``
    unioned; the global store wins on slug conflict and on base_url
    conflict across sections (the same endpoint stored twice renders one
    row, the global one). Everything else comes from the profile config
    untouched — the profile's own route, exclusions and model defaults
    are never globalized.
    """
    merged = dict(profile_cfg or {})
    global_cfg = global_cfg or {}
    global_providers = global_cfg.get("providers")
    global_custom = global_cfg.get("custom_providers")
    global_slugs, global_urls = _global_provider_identities(global_cfg)
    if not global_slugs and not global_urls:
        return merged
    if isinstance(global_providers, dict) and global_providers:
        out: dict = {}
        if isinstance(merged.get("providers"), dict):
            for key, entry in merged["providers"].items():
                if isinstance(entry, dict) and (
                    _provider_entry_slug(entry, str(key)) in global_slugs
                    or (_provider_entry_url(entry) and _provider_entry_url(entry) in global_urls)
                ):
                    continue
                out[key] = entry
        for key, entry in global_providers.items():
            out[key] = entry
        merged["providers"] = out
    if isinstance(global_custom, list) and global_custom:
        out_list: list = []
        if isinstance(merged.get("custom_providers"), list):
            for entry in merged["custom_providers"]:
                if isinstance(entry, dict) and (
                    _provider_entry_slug(entry) in global_slugs
                    or (_provider_entry_url(entry) and _provider_entry_url(entry) in global_urls)
                ):
                    continue
                out_list.append(entry)
        out_list.extend(entry for entry in global_custom if isinstance(entry, dict))
        merged["custom_providers"] = out_list
    return merged
