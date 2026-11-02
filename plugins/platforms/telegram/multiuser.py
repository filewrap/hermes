"""Telegram commands and admission policy for opt-in Mongo multiuser mode."""
from __future__ import annotations

import re

_TARGET = re.compile(r"[0-9]+\Z")


def handle_command(store, actor_id, text: str, *, is_group: bool = False, current_group_id: str | None = None):
    """Execute one policy command. Returns (handled, response); never accepts usernames."""
    from gateway.mongo_multiuser import principal
    match = re.match(r"^\s*/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", str(text or ""), re.DOTALL)
    if not match:
        return False, ""
    command = match.group(1).lower()
    args = (match.group(2) or "").strip()
    actor = principal(str(actor_id))
    owner = str(actor_id) == str(store.owner_user_id)
    if command in {"allow", "revoke", "ban", "unban"}:
        if not owner:
            return True, "Only the configured owner can change access."
        fields = args.split()
        if not fields or len(fields) > 2 or not _TARGET.fullmatch(fields[0]):
            return True, f"Usage: /{command} <numeric Telegram user ID> [on|off]"
        target = fields[0]
        state = command in {"allow", "ban"}
        if len(fields) == 2:
            if command in {"revoke", "unban"} or fields[1].lower() not in {"on", "off"}:
                return True, f"Usage: /{command} <numeric Telegram user ID> [on|off]"
            state = fields[1].lower() == "on"
        if command in {"allow", "revoke"}:
            store.allow_user(actor, target, state)
            return True, f"User {target} {'allowed' if state else 'access revoked'}."
        store.ban_user(actor, target, state)
        return True, f"User {target} {'banned' if state else 'unbanned'}."
    if command == "gsetting":
        return True, "This command was renamed to /gsettings; the old spelling no longer changes settings."
    if command == "gsettings":
        scoped = is_group and current_group_id is not None and callable(getattr(store, "principal_for_scope", None))
        if scoped:
            actor = store.principal_for_scope(str(actor_id), "group", str(current_group_id))
        access = store.access_for_scope(str(actor_id), "group", str(current_group_id)) if scoped else store.get_access(str(actor_id))
        if access.get("banned"):
            return True, "Access denied."
        if not (owner or (access.get("sudo") if scoped else access.get("allowed"))):
            if is_group and not args:
                return True, "Your access is pending owner approval." if not access.get("allowed") else "Access denied."
            return True, "Access denied."
        if not is_group:
            return True, "Use /gsettings in a group."
        if current_group_id is not None:
            group_id = str(current_group_id)
            setting = args
            first, _, remaining = args.partition(" ")
            if first and "=" not in first and re.fullmatch(r"-?[0-9]+", first):
                if first != group_id:
                    return True, "Group settings can only target this group."
                setting = remaining
        else:
            group_id, _, setting = args.partition(" ")
        if not group_id:
            return True, "Use /gsettings in the current group."
        if not setting:
            value = store.get_group_settings(actor, group_id)
            return True, str(value or "No group settings configured.")
        key, sep, value = setting.partition("=")
        if not sep or key not in {"enabled", "name", "description", "default_model", "system_prompt"}:
            return True, "Unsupported group setting."
        if len(value) > 8000 or "\x00" in value:
            return True, "Group setting value is too long or invalid."
        if key == "enabled":
            lowered = value.strip().lower()
            if lowered not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                return True, "Use enabled=true or enabled=false."
            value = lowered in {"1", "true", "yes", "on"}
        store.set_group_settings(actor, group_id, {key: value})
        return True, "Group setting saved."
    return False, ""


def admitted(store, user_id: str | int) -> bool:
    access = store.get_access(str(user_id))
    return not access.get("banned", False) and (str(user_id) == str(store.owner_user_id) or access.get("allowed", False))
