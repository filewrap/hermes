"""Native scoped settings/access controls, before isolated conversational dispatch.

Access roles are app permissions, not OS sudo and never a route into the owner's
agent, credentials or filesystem. The durable tenant store is authoritative.
"""
from __future__ import annotations

from typing import Any


async def handle_access_command(adapter: Any, event: Any, source: Any, name: str,
                                arguments: str, store: Any) -> bool:
    if name not in {"settings", "gsettings", "gsetting", "allow", "allowall", "allowlist", "disallow"}:
        return False
    private = source.chat_type in {"dm", "private", "direct"}
    group = source.chat_type in {"group", "forum"}
    access = store.access_for_scope(str(source.user_id), source.chat_type, str(source.chat_id))

    async def reply(text: str) -> None:
        await adapter.send(source.chat_id, text, reply_to=getattr(event.message, "id", None),
                           metadata={"thread_id": source.thread_id} if source.thread_id else None)

    if name == "gsetting":
        await reply("This command was renamed to `/gsettings`. Use `/gsettings` inside the group; the old spelling no longer opens or changes settings.")
        return True
    if name == "settings" and not private:
        await reply("Your settings are private. Open a direct chat with this bot and send `/settings`.")
        return True
    if name in {"gsettings", "allowall"} and not group:
        await reply(f"`/{name}` works only in a group. Use `/settings` for your private preferences.")
        return True
    if not access.get("allowed") or access.get("banned"):
        await reply("This account is not authorized to use these controls. Start the bot privately and ask the owner for access.")
        return True
    if name != "settings" and not access.get("sudo"):
        await reply("Only the owner or an explicitly authorized user can change access or group settings in this scope.")
        return True
    await adapter._cache_inbound_peer(event, source.chat_id)
    if name == "settings":
        adapter._skills_ui().cancel_for_source(source)
        await adapter._settings_ui().open(source, reply_to=getattr(event.message, "id", None))
        return True
    from .telegram_controls import handle_owner_command, show_settings
    if name == "gsettings":
        note = ""
        if arguments:
            from .multiuser import handle_command
            _, note = handle_command(store, str(source.user_id), "/gsettings " + arguments,
                                     is_group=True, current_group_id=str(source.chat_id))
        await show_settings(adapter, source, note=note, reply_to=getattr(event.message, "id", None))
    else:
        await handle_owner_command(adapter, source, name, arguments,
                                   reply_to=getattr(event.message, "id", None))
    return True
