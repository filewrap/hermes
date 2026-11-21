#!/usr/bin/env python3
"""Owner gate shared by the F9b-e gateway service tools (dashboard, portal, plugins,
gateway, nodes, mobile_ui).

Same shape as the F9a presence gate (``tools/presence_tool.py``): the requester
must be provably the owner, fail-closed, in three layers — tenant sessions
never see these tools (none of them is in ``agent/tenant_policy.SUPPORTED``),
the Telegram adapter hides them from every non-launch-owner sender via
``disabled_tools_for_source``, and the handler re-checks here. The launch
owner's id is read from the LAUNCH (default) home, not the context home:
every ``tg-<id>`` profile stamps its own user as its ``OWNER_TELEGRAM_ID``,
so the context home would call every profiled user "the owner".

Local surfaces (``cli``/``local``) run as the owner-operator. A Telegram
group turn carries no DM personal context, so the handler trusts the adapter
layer there — the catalog is the enforcement boundary for group turns,
exactly as it is for every other owner-gated tool.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("tools.gateway_owner")

_LOCAL_PLATFORMS = frozenset({"cli", "local"})


def requester_is_owner(agent: Any) -> bool:
    """True only when the requester is provably the owner (fail-closed)."""
    try:
        from agent.tenant_scope import current_tenant_scope

        if current_tenant_scope() is not None:
            return False
    except Exception:
        logger.debug("gateway_owner: tenant scope unreadable", exc_info=True)
        return False
    try:
        from gateway.telegram_personal_settings import current_personal_memory_context

        context = current_personal_memory_context()
    except Exception:
        logger.debug("gateway_owner: personal memory context unreadable", exc_info=True)
        return False
    if context:
        try:
            from hermes_cli.profiles import get_profile_dir
            from gateway.mongo_multiuser import owner_user_id

            return str(context.get("user_id") or "") == owner_user_id(
                get_profile_dir("default")
            )
        except Exception:
            logger.debug("gateway_owner: owner id unreadable", exc_info=True)
            return False
    platform = (
        str(getattr(agent, "platform", "") or "").strip().lower()
        if agent is not None
        else ""
    )
    if platform in _LOCAL_PLATFORMS or platform == "":
        # Local operator (CLI/TUI/desktop, or a registry call with no agent):
        # the person at the keyboard is the owner.
        return True
    if platform == "telegram":
        # Group turn: no DM context to check; the adapter's per-source
        # catalog filter is the gate.
        return True
    # Any other remote platform (discord, slack, ...) cannot prove the
    # requester is the Telegram owner: deny.
    return False


def owner_telegram_id() -> Optional[str]:
    """Install owner's Telegram id, when the launch home's .env names it."""
    try:
        from hermes_cli.profiles import get_profile_dir
        from gateway.mongo_multiuser import owner_user_id

        return str(owner_user_id(get_profile_dir("default")))
    except Exception:
        logger.debug("gateway_owner: owner telegram id unreadable", exc_info=True)
        return None


def owner_only_error(tool_name: str) -> str:
    """Tool-error body for a denied non-owner request."""
    from tools.registry import tool_error

    return tool_error(f"{tool_name} is available to the owner only", success=False)
