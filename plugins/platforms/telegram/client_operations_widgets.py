"""Widget execution for telegram_client, split out of the facade.

This owns the widget half of the direct-action family: running one widget
action against the current chat (``execute_widget``), showing one pre-made
widget or its rich equivalent (``execute_widget_show``), and routing the
direct actions to the right executor (``execute_direct``). The facade
stays a thin dispatcher; the behaviour lives here. Telethon stays a lazy
import inside the functions that need it, like the facade.
"""

from __future__ import annotations

from typing import Any


async def execute_widget(ops: Any, source: Any, action: str, p: dict[str, Any], profile: str) -> dict[str, Any]:
    """Run one widget action against the current chat.

    The peer is always the current chat: no widget action takes a chat
    target. The route is re-checked right before the transport call,
    like _send_rich_message does — the adapter call is the send.
    """
    from plugins.platforms.telegram.mtproto_rich import inline_markup

    allowed, reason = ops._authorize(source)
    if not allowed or ops._profile(source) != profile:
        raise ValueError(reason or "The Telegram route changed before the widget operation could run")
    chat_id = str(source.chat_id)
    thread_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
    if action == "widget_send":
        result = await ops.adapter.send_widget(
            chat_id, p["html"], caption=p.get("caption", ""), width=p.get("width", 0),
            height=p.get("height", 0), allow_queries=p.get("allow_queries"),
            shared_widget=p.get("shared_widget"), public_data=p.get("public_data"),
            chat_data=p.get("chat_data"), reply_to=p.get("reply_to"),
            top_msg_id=thread_id,
            reply_markup=inline_markup(p.get("reply_markup")) if p.get("reply_markup") else None,
        )
        return ops._widget_send_data(result, chat_id, thread_id)
    if action == "widget_send_by_id":
        result = await ops.adapter.send_widget_by_id(
            chat_id, p["widget_id"], caption=p.get("caption", ""),
            chat_data=p.get("chat_data"), top_msg_id=thread_id,
        )
        return ops._widget_send_data(result, chat_id, thread_id)
    if action == "widget_upload":
        result = await ops.adapter.upload_widget(
            chat_id, p["html"], width=p.get("width", 0), height=p.get("height", 0),
            allow_queries=p.get("allow_queries"), shared_widget=p.get("shared_widget"),
            public_data=p.get("public_data"), chat_data=p.get("chat_data"),
        )
        checked = ops._widget_checked(result)
        widget_id = (checked.raw_response or {}).get("widget_id") if checked.raw_response else None
        if widget_id is None:
            raise RuntimeError("Telegram did not return a widget_id for the upload")
        return {"widget_id": widget_id, "chat_id": chat_id}
    if action == "widget_answer":
        if await ops.adapter.answer_widget_query(p["query_id"], p.get("result")) is not True:
            raise RuntimeError("Telegram widget query answer failed")
        return {"answered": True, "query_id": p["query_id"]}
    if action == "widget_edit_message":
        ops._widget_checked(await ops.adapter.edit_widget_message_data(
            chat_id, p["message_id"], public_data=p.get("public_data"),
            chat_data=p.get("chat_data")))
        return {"message_id": p["message_id"], "chat_id": chat_id}
    if action == "widget_edit_shared":
        ops._widget_checked(await ops.adapter.edit_shared_widget_data(
            p["widget_id"], p["public_data"], cache_time=p.get("cache_time")))
        return {"widget_id": p["widget_id"]}
    if action == "widget_edit_inline":
        from telethon import types

        inline_id = types.InputBotInlineMessageID(**p["inline_message_id"])
        ops._widget_checked(await ops.adapter.edit_inline_widget_data(
            inline_id, public_data=p.get("public_data"), chat_data=p.get("chat_data")))
        return {"inline_message_id": p["inline_message_id"]}
    if action == "widget_get_public_data":
        result = await ops.adapter.get_widget_public_data(
            chat_id, p["message_id"], hash=p.get("hash", 0))
        if result is None:
            raise RuntimeError("Telegram widget public-data read failed")
        from telethon.tl.types import messages as tl_messages

        if isinstance(result, tl_messages.WidgetPublicDataNotModified):
            return {"not_modified": True, "period": result.period}
        if isinstance(result, tl_messages.WidgetPublicData):
            return {"data": result.data, "period": result.period, "hash": result.hash,
                    "not_modified": False}
        raise RuntimeError("Telegram returned an unexpected widget public-data result")
    raise ValueError(f"Unsupported widget action: {action}")


async def execute_widget_show(ops: Any, source: Any, p: dict[str, Any], profile: str) -> dict[str, Any]:
    """Show one pre-made widget, or its rich equivalent outside a DM.

    The surface is decided by the resolved peer, the same ground truth
    the transport enforces: a DM gets the widget (plus the companion
    rich message first, when one was given or buttons were asked for —
    the classic widget keyboard is callback-only, so one-way buttons
    travel on the companion); any other chat gets the same content as
    one precise rich message with the buttons appended. The transport
    would refuse a widget anywhere but a DM, so a group can never
    receive one through this action.
    """
    import html

    from plugins.platforms.telegram.widget_messages import is_dm_peer

    allowed, reason = ops._authorize(source)
    if not allowed or ops._profile(source) != profile:
        raise ValueError(reason or "The Telegram route changed before the widget could be shown")
    chat_id = str(source.chat_id)
    thread_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
    peer = await ops._peer(source)
    if is_dm_peer(peer):
        companion_message_id = None
        if p.get("companion") is not None or p.get("buttons"):
            companion_html = p.get("companion") or f"<h1>{html.escape(p['title'])}</h1>"
            sent = await ops._send_rich_message(
                source, {"content": companion_html, "format": "html",
                         "buttons": p.get("buttons", [])}, profile)
            companion_message_id = sent.get("message_id")
        result = await ops.adapter.send_widget(
            chat_id, p["html"], top_msg_id=thread_id)
        data = ops._widget_send_data(result, chat_id, thread_id)
        data["surface"] = "widget"
        data["template"] = p["template"]
        if companion_message_id is not None:
            data["companion_message_id"] = companion_message_id
        return data
    rich = await ops._send_rich_message(
        source, {"content": p["rich_html"], "format": "html",
                 "buttons": p.get("buttons", [])}, profile)
    rich["surface"] = "rich"
    rich["template"] = p["template"]
    return rich


async def execute_direct(ops: Any, source: Any, action: str, p: dict[str, Any], profile: str) -> dict[str, Any]:
    """Route one direct action to its executor.

    The direct family sends immediately with no confirmation card:
    ``quote_reply`` and the plain rich send run on the facade, the
    widget actions and ``widget_show`` run here. The facade's
    ``WIDGET_ACTIONS`` set is late-imported so this module never
    creates a module-level cycle with the facade.
    """
    from .client_operations import WIDGET_ACTIONS

    if action == "quote_reply":
        return await ops._send_quote_reply(source, p, profile)
    if action == "widget_show":
        return await execute_widget_show(ops, source, p, profile)
    if action in WIDGET_ACTIONS:
        return await execute_widget(ops, source, action, p, profile)
    return await ops._send_rich_message(source, p, profile)
