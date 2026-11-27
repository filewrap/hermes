"""Dialog enumeration for the Telegram client operations facade.

``list_chats`` is a peer-less read: it enumerates the bot account's own
dialogs, so it needs no current-chat peer and validates its limit here
instead of in ``TelegramClientOperations._validate_payload``.
"""

from typing import Any


async def list_chats(ops: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Dialogs the bot account participates in, via Telethon get_dialogs.

    Bots may call messages.GetDialogs; user-only history RPCs are not
    used. Each row carries the dialog's marked peer id — the same id
    Hermes routes the chat under — so it keys the same ``conv_`` ref as
    an observed message from that chat.
    """
    from telethon import utils as tg_utils

    from plugins.platforms.telegram.client_operations import MAX_PAGE

    limit = ops._int(params.get("limit", 20), "limit", minimum=1, maximum=MAX_PAGE)
    dialogs = await ops._client().get_dialogs(limit=limit)
    chats: list[dict[str, Any]] = []
    for dialog in dialogs or []:
        entity = getattr(dialog, "entity", None)
        peer_id = getattr(dialog, "id", None)
        if peer_id is None and entity is not None:
            try:
                peer_id = tg_utils.get_peer_id(entity)
            except Exception:  # health: allow BLE001 -- an unresolvable entity is skipped, never fatal
                peer_id = None
        if peer_id is None:
            continue
        if getattr(dialog, "is_user", False):
            chat_type = "dm"
        elif getattr(dialog, "is_channel", False) and getattr(entity, "broadcast", False):
            chat_type = "channel"
        else:
            chat_type = "group"
        last = getattr(dialog, "message", None)
        preview = str(getattr(last, "message", "") or "")[:200]
        date = getattr(dialog, "date", None)
        chats.append({
            "id": str(peer_id), "name": str(getattr(dialog, "name", "") or ""),
            "username": getattr(entity, "username", None),
            "type": chat_type, "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
            "last_message": preview or None,
            "last_message_date": date.isoformat() if hasattr(date, "isoformat") else None,
        })
    return {"chats": chats, "total": len(chats)}
