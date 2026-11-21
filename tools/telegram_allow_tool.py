"""Admit or remove Telegram users, groups and channels, in the owner's words.

Any owner wording maps here ("add this user to this group/that group/that
channel allowlist", "make them allowed", "let them use it", "allow this
entire group", "remove this user from allowed"). The owner's words ARE the
confirmation: there is no confirmation card and no "allow or disallow?"
round-trip. The only double confirmation is on a resolution failure — the
tool says plainly that the target could not be resolved and asks the owner
to say it again with the exact target; a re-stated call with a resolvable
target proceeds directly, with no further confirmation.

Routing, by detected target kind — never resolve a group id as a user:

- A user id (positive digits) or an @username that resolves to an already
  started user, with scope ``global`` (the default): the launch-home store's
  ``allow_user`` — the SAME function the access UI calls — plus the same
  live adapter allowlist sync, and the per-user profile creation the grant
  path already does.
- A user id with a group/channel id as scope: the scoped grant on that
  chat's home store (``grant_scope_access`` / ``revoke_scope_access``).
  The chat must already have a shared profile; a scoped grant never
  silently enables a whole group.
- A group/channel id (negative digits, incl. ``-100...``) as the target:
  the whole chat, allowgc-style — enabling (or disabling) its shared group
  profile, exactly what the /allowgc confirm does. A bare group NAME cannot
  resolve and hits the failed-resolution path.

@username resolution goes through the store's ``resolve_started_user``
where the store exposes it (Local backend): only already-started users
resolve, never provision on lookup. A store without it cannot resolve a
username at all, so the call fails plainly and asks for the numeric id.
The owner can never revoke themselves: the store raises, and the tool
surfaces that plainly.

Owner-only in the same three layers as the other owner-gated tools:
tenant sessions never see it (it is not in ``agent/tenant_policy.SUPPORTED``),
the Telegram adapter hides it from every non-launch-owner sender via
``disabled_tools_for_source``, and this handler re-checks the owner itself.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.session_context import get_session_env
from tools.gateway_owner import requester_is_owner
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

TELEGRAM_ALLOW_TOOL_NAME = "telegram_allow"
_USER_ID_RE = re.compile(r"^[0-9]+$")
_CHAT_ID_RE = re.compile(r"^-[0-9]+$")
_USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{5,32}$")

TELEGRAM_ALLOW_SCHEMA = {
    "name": TELEGRAM_ALLOW_TOOL_NAME,
    "description": (
        "Allow or remove a Telegram user, group, or channel, in the owner's words. "
        "Owner-only. target: a user id (positive digits), an @username of a user who "
        "has already started the bot, or a group/channel id (negative digits, incl. "
        "-100...). For a replied-to user, pass their numeric id or @username as seen "
        "in the message. action: allow or disallow. scope: global (the default) or a "
        "group/channel id — when the owner speaks in a group or names one, pass that "
        "chat's id and the user is admitted only there; a group/channel id as the "
        "target admits or removes that whole chat. A bare group name cannot resolve. "
        "The owner's words are the confirmation: call directly, never ask allow-or-"
        "disallow first. If the target cannot be resolved, the result says so and "
        "asks the owner to say it again with the exact target."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Who or what to allow or remove: a Telegram user id, an @username, "
                    "or a group/channel id (negative digits)."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["allow", "disallow"],
                "description": "allow admits the target; disallow removes them.",
            },
            "scope": {
                "type": "string",
                "description": (
                    "Where the grant applies: global (the default), or one group/channel "
                    "id. Ignored when the target itself is a group/channel id."
                ),
            },
        },
        "required": ["target", "action"],
        "additionalProperties": False,
    },
}


def _resolution_failure(target: str) -> str:
    return tool_error(
        f"Could not resolve {target!r} to a Telegram user or chat. "
        "Say it again with the exact target — a numeric user id, an @username of "
        "a user who has already started the bot, or a group/channel id — and it "
        "will go through directly.",
        needs_owner_confirmation=True,
    )


def _launch_store():
    from hermes_constants import get_process_hermes_home
    from gateway.mongo_multiuser import store_for_home

    return store_for_home(get_process_hermes_home())


def _actor_for(store: Any):
    from gateway.mongo_multiuser import principal

    return principal(store.owner_user_id)


def _resolve_user_id(store: Any, target: str) -> str | None:
    """Numeric id as-is; @username only via a store that can resolve started
    users. Returns None when the target cannot be resolved."""
    if _USER_ID_RE.fullmatch(target):
        return target
    if not _USERNAME_RE.fullmatch(target):
        return None
    resolve = getattr(store, "resolve_started_user", None)
    if not callable(resolve):
        return None
    try:
        resolved = resolve(target)
    except Exception:
        logger.debug("Allow: username resolution failed for %r", target, exc_info=True)
        return None
    if not resolved:
        return None
    return str(resolved.get("user_id") or "")


def _sync_allowlist(action: str, user_id: str) -> None:
    from gateway.pairing import _sync_allowlist_add, _sync_allowlist_remove

    if action == "allow":
        _sync_allowlist_add(Platform.TELEGRAM.value, user_id)
    else:
        _sync_allowlist_remove(Platform.TELEGRAM.value, user_id)


def _grant_whole_chat(chat_id: str, action: str, owner_id: str) -> dict[str, Any]:
    from gateway.allowgc import create_group_profile, disable_group_profile

    if action == "allow":
        home = create_group_profile(chat_id, None, owner_id)
        return {
            "kind": "group",
            "chat_id": chat_id,
            "action": "allow",
            "scope": chat_id,
            "home": str(home),
            "note": "That whole chat can now use the bot.",
        }
    state = disable_group_profile(chat_id)
    if state is None:
        return {"error": f"Chat {chat_id} is not set up for shared access, so there is nothing to remove."}
    return {
        "kind": "group",
        "chat_id": chat_id,
        "action": "disallow",
        "scope": chat_id,
        "note": "Shared access for that chat is off; the profile is kept for seven days.",
    }


def _grant_user(user_id: str, action: str, scope: str) -> dict[str, Any]:
    from agent.mongo_tenants import TenantStoreError
    from gateway.mongo_multiuser import store_for_home

    allowed = action == "allow"
    if scope == "global":
        store = _launch_store()
        actor = _actor_for(store)
        try:
            store.allow_user(actor, user_id, allowed)
        except TenantStoreError as exc:
            return {"error": str(exc) or "Could not change access for this user."}
        _sync_allowlist(action, user_id)
        result: dict[str, Any] = {
            "kind": "user",
            "target": user_id,
            "action": action,
            "scope": "global",
        }
        if allowed:
            from gateway.user_profiles import maybe_create_user_profile

            maybe_create_user_profile(store, user_id)
            result["note"] = "Allowed everywhere."
        else:
            result["note"] = "Removed everywhere."
        return result

    from gateway.allowgc import resolve_home

    group_home = resolve_home(scope, reap=False)
    if group_home is None:
        return {"error": (
            f"Chat {scope} has no shared profile yet. Allow the whole chat first "
            "(target the group id), then grant the user there."
        )}
    store = store_for_home(Path(group_home))
    actor = _actor_for(store)
    try:
        if allowed:
            store.grant_scope_access(actor, user_id, scope)
        else:
            store.revoke_scope_access(actor, user_id, scope)
    except TenantStoreError as exc:
        message = str(exc) or "Could not change access for this user."
        if not allowed and "not found" in message.lower():
            return {
                "kind": "user",
                "target": user_id,
                "action": "disallow",
                "scope": scope,
                "note": f"That user was not allowed in chat {scope}.",
            }
        return {"error": message}
    return {
        "kind": "user",
        "target": user_id,
        "action": action,
        "scope": scope,
        "note": f"Allowed in chat {scope} only." if allowed else f"Removed from chat {scope}.",
    }


def telegram_allow(args: dict[str, Any], **kwargs: Any) -> str:
    """Admit or remove a Telegram user, group, or channel, directly."""
    if not requester_is_owner(kwargs.get("parent_agent")):
        return tool_error("Only the owner can allow or remove Telegram users.")
    if (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower() != Platform.TELEGRAM.value:
        return tool_error("Allowing Telegram users is a Telegram setting.")
    target = str(args.get("target") or "").strip()
    action = str(args.get("action") or "").strip().lower()
    scope = str(args.get("scope") or "global").strip().lower()
    if not target:
        return tool_error("Say who to allow or remove: a user id, an @username, or a group/channel id.")
    if action not in {"allow", "disallow"}:
        return tool_error("Action must be allow or disallow.")
    if scope != "global" and not _CHAT_ID_RE.fullmatch(scope):
        return tool_error("Scope must be global or a group/channel id (negative digits).")

    if _CHAT_ID_RE.fullmatch(target):
        if scope != "global" and scope != target:
            return tool_error(
                "A group/channel id as the target is the whole chat; pass it alone, without a different scope.")
        try:
            owner_id = str(_launch_store().owner_user_id)
        except Exception:
            return tool_error("Could not read the owner's id; the grant did not happen.")
        result = _grant_whole_chat(target, action, owner_id)
        if "error" in result:
            return tool_error(result["error"])
        return json.dumps(result, ensure_ascii=False)

    try:
        launch_store = _launch_store()
    except Exception:
        return tool_error("Could not open the access store; the grant did not happen.")
    user_id = _resolve_user_id(launch_store, target)
    if not user_id:
        return _resolution_failure(target)
    result = _grant_user(user_id, action, scope)
    if "error" in result:
        return tool_error(result["error"])
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name=TELEGRAM_ALLOW_TOOL_NAME,
    toolset="telegram_admin",
    schema=TELEGRAM_ALLOW_SCHEMA,
    handler=telegram_allow,
)
