"""Native scoped settings/access controls, before isolated conversational dispatch.

Access roles are app permissions, not OS sudo and never a route into the owner's
agent, credentials or filesystem. The durable tenant store is authoritative.

Non-admitted senders never reach this handler in production (the adapter drops
them silently first); the access checks below stay silent as well, so a denial
never leaks a reply, reaction, or usage hint to an unlisted account.
"""
from __future__ import annotations

from typing import Any


async def handle_access_command(adapter: Any, event: Any, source: Any, name: str,
                                arguments: str, store: Any) -> bool:
    if name not in {"settings", "allow", "trigger", "allowlist", "disallow"}:
        return False
    private = source.chat_type in {"dm", "private", "direct"}
    group = source.chat_type in {"group", "forum"}
    access = store.access_for_scope(str(source.user_id), source.chat_type, str(source.chat_id))

    async def reply(text: str) -> None:
        await adapter.send(source.chat_id, text, reply_to=getattr(event.message, "id", None),
                           metadata={"thread_id": source.thread_id} if source.thread_id else None)

    # Admission first: a non-admitted sender is silently ignored, even when
    # the command itself would otherwise answer with a usage redirect.
    if not access.get("allowed") or access.get("banned"):
        return True
    if name == "settings" and not private:
        await reply("Your settings are private. Open a direct chat with this bot and send `/settings`.")
        return True
    if name == "trigger" and not group:
        await reply("`/trigger` works only in a group. Use `/settings` for your private preferences.")
        return True
    if name != "settings" and not access.get("sudo"):
        # Admitted but not an admin for this scope: silent denial. Group
        # settings and owner-only grants stay owner/admin-only without a
        # reply that would confirm the command exists to a lesser account.
        return True
    await adapter._cache_inbound_peer(event, source.chat_id)
    if name == "settings":
        adapter._skills_ui().cancel_for_source(source)
        await adapter._settings_ui().open(source, reply_to=getattr(event.message, "id", None))
        return True
    from .telegram_controls import handle_owner_command, resolve_replied_user_id
    reply_to_user_id = None
    if name in {"allow", "disallow"}:
        reply_to_user_id = await resolve_replied_user_id(event)
    await handle_owner_command(adapter, source, name, arguments,
                               reply_to=getattr(event.message, "id", None),
                               reply_to_user_id=reply_to_user_id)
    return True
