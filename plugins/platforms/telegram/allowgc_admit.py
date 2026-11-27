"""Allowgc auto-admission: grant a group member in the GROUP store.

Moved out of the Telegram adapter (line-cap offset) with no behaviour
change except logging: every silent skip now leaves a line carrying the
actor and chat id, so the next member who gets no reply is identifiable
from the logs instead of invisible (the 948247711 case). The already
allowed short-circuit logs at DEBUG — it fires on every admitted
member's command and would bury the denial lines at INFO.
"""
from __future__ import annotations

import logging
from typing import Any

from gateway.allowgc import group_home_for_source

logger = logging.getLogger(__name__)


def allowgc_auto_admit(source: Any, owner: Any, actor_id: Any, access: dict) -> dict:
    """Grant an allowgc group member in the GROUP store; return effective access.

    The grant is scoped to this group only — no sudo, no global allow.
    Sudo needs a real profile, so members can use the bot but cannot run
    owner commands such as /trigger.

    The adapter runs under the LAUNCH home — authorization stays with
    the receiving bot while the gc profile is a routed satellite — so
    the group store is resolved explicitly here. The grant must land in
    the group profile's store: the runner gate reads the group store to
    admit members, so a grant anywhere else is invisible to it.

    Fail-closed throughout: no grant when the user is already allowed in
    the GROUP store, banned, revoked, a bot, or opted out; no grant
    outside an active allowgc group; no grant for a globally
    (launch-store) banned user, and no grant at all when the launch store
    cannot be read or the group store cannot take the grant. Every
    refusal, failure, and silent skip logs a line carrying the actor and
    chat id, so a refused member is identifiable from the logs.
    """
    if getattr(source, "is_bot", False):
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: bot sender",
            actor_id, source.chat_id,
        )
        return access
    if getattr(source, "chat_type", "") not in {"group", "forum"}:
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: not a group",
            actor_id, source.chat_id,
        )
        return access
    from gateway.allowgc import is_active_for_chat, is_opted_out
    if not is_active_for_chat(source.chat_id):
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: allowgc inactive",
            actor_id, source.chat_id,
        )
        return access
    group_home = group_home_for_source(source)
    if group_home is None:
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: no group home",
            actor_id, source.chat_id,
        )
        return access
    if is_opted_out(group_home, actor_id):
        # Consulted BEFORE any admission decision (and before the
        # already-allowed early return below): an opted-out member is
        # never re-admitted, even with a live grant from before the
        # opt-out. Denied access, so callers that only read the
        # result deny as well; the handler's own opt-out check still
        # drops the message outright.
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: opted out",
            actor_id, source.chat_id,
        )
        return {**access, "allowed": False, "sudo": False}
    if access.get("banned") or access.get("revoked"):
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: banned/revoked",
            actor_id, source.chat_id,
        )
        return access
    try:
        from hermes_constants import get_routing_process_hermes_home
        from gateway.mongo_multiuser import store_for_home
        launch_store = store_for_home(get_routing_process_hermes_home())
        if launch_store.get_access(actor_id).get("banned"):
            logger.warning(
                "Telegram allowgc auto-admission refused for user %s in chat %s: "
                "globally banned",
                actor_id, source.chat_id,
            )
            # Refuse with denied access, not the passed-in dict: it may
            # be a stale pre-ban read that still claims allowed.
            return {**access, "allowed": False, "sudo": False}
    except Exception:
        logger.warning(
            "Telegram allowgc auto-admission refused for user %s in chat %s: "
            "launch ban check unavailable", actor_id, source.chat_id, exc_info=True,
        )
        return access
    try:
        group_store = store_for_home(group_home)
    except Exception:
        logger.warning(
            "Telegram allowgc auto-admission refused for user %s in chat %s: "
            "group store unavailable", actor_id, source.chat_id, exc_info=True,
        )
        return access
    # The already-allowed shortcut reads the GROUP store, never the
    # passed-in launch access: a launch-global grant (``allowed: True``
    # on the launch profile, e.g. via /allow) says nothing about this
    # group, and the old shortcut returned before any group grant was
    # written — so a user allowed by /allow was silently ignored in
    # every allowgc group, because the runner gate reads only the group
    # store. A user the group store already grants keeps the shortcut;
    # anyone else falls through to the grant write below.
    try:
        group_access = group_store.access_for_scope(
            actor_id, source.chat_type, str(source.chat_id))
    except Exception:
        logger.warning(
            "Telegram allowgc auto-admission refused for user %s in chat %s: "
            "group access read unavailable", actor_id, source.chat_id, exc_info=True,
        )
        return access
    if group_access.get("banned") or group_access.get("revoked"):
        logger.info(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: banned/revoked in group store",
            actor_id, source.chat_id,
        )
        return group_access
    if group_access.get("allowed"):
        logger.debug(
            "Telegram allowgc auto-admission skipped for user %s in chat %s: already allowed",
            actor_id, source.chat_id,
        )
        return group_access
    grant = getattr(group_store, "grant_scope_access", None)
    if not callable(grant):
        logger.error(
            "Telegram allowgc auto-admission refused for user %s in chat %s: "
            "group store %s cannot take a scoped grant",
            actor_id, source.chat_id, type(group_store).__name__,
        )
        return access
    try:
        from gateway.mongo_multiuser import principal
        # Sudo needs a real profile: the auto-admission grant is scoped
        # to this group only, so members can use the bot but cannot run
        # owner commands such as /trigger.
        grant(principal(str(owner)), actor_id, str(source.chat_id))
    except Exception:
        logger.warning(
            "Telegram allowgc auto-admission failed for user %s in chat %s",
            actor_id, source.chat_id, exc_info=True,
        )
        return access
    logger.info(
        "Telegram allowgc auto-admitted user %s in chat %s",
        actor_id, source.chat_id,
    )
    try:
        return group_store.access_for_scope(actor_id, source.chat_type, str(source.chat_id))
    except Exception:
        logger.warning(
            "Telegram allowgc access recheck failed for user %s in chat %s",
            actor_id, source.chat_id, exc_info=True,
        )
        return access
