"""Telegram commands and admission policy for opt-in Mongo multiuser mode."""
from __future__ import annotations

import re

_TARGET = re.compile(r"[0-9]+\Z")


def handle_command(store, actor_id, text: str):
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
    return False, ""


def admitted(store, user_id: str | int) -> bool:
    access = store.get_access(str(user_id))
    return not access.get("banned", False) and (str(user_id) == str(store.owner_user_id) or access.get("allowed", False))
