"""``hm:`` action callbacks dispatched by their ``:``-separated parts.

Extracted from ``TelethonAdapter._on_callback_query`` so that function's
complexity stays at its on-main value: owner callbacks, help paging,
personal-settings open, the guestbot on/off toggle, the autonomous
Disable button, and the generic "unavailable" fallback. Every path
returns; a caller that reaches here has already passed the ``hm:`` prefix
ladder (control UI, busy/resume/rename/pages/reasoning).
"""
from __future__ import annotations

import logging
from typing import Any

from .callback_answer import answer_callback

logger = logging.getLogger(__name__)


async def handle_hm_action_callback(adapter: Any, event: Any, source: Any, data: str) -> None:
    """Dispatch one ``hm:`` action callback. Always answers or handles."""
    parts = str(data).split(":")
    if len(parts) >= 3 and parts[1] in {"settings", "allow", "allowlist", "admission", "allowgc"}:
        from .telegram_controls import handle_owner_callback

        await handle_owner_callback(adapter, event, source, str(data))
        return
    if len(parts) >= 4 and parts[1] == "help":
        await answer_callback(event)
        view, page = parts[2], int(parts[3])
        await adapter._edit_rich_ui(event, view=view, page=page)
        return
    if len(parts) in {3, 5} and parts[1] == "personal" and parts[2] == "open":
        if source.chat_type not in {"dm", "private", "direct"}:
            await answer_callback(event, "Personal settings are DM-only.", show_alert=True)
            return
        await answer_callback(event)
        adapter._skills_ui().cancel_for_source(source)
        kwargs = {"message_id": getattr(event, "message_id", None)}
        if len(parts) == 5:
            kwargs["help_origin"] = (parts[3], int(parts[4]))
        await adapter._settings_ui().open(source, **kwargs)
        return
    if parts[1] == "guestbot" and len(parts) == 3 and parts[2] in {"on", "off"}:
        enabled = parts[2] == "on"
        try:
            adapter._persist_guestbot_setting(source, enabled)
        except Exception:
            logger.warning("Could not persist Telegram GuestBot setting", exc_info=True)
            await answer_callback(event, "Could not save this setting to the active profile.", show_alert=True)
            return
        await answer_callback(event, f"GuestBot {'enabled' if enabled else 'disabled'}.")
        from .mtproto_rich import edit_request

        text, markup = adapter._guestbot_menu()
        await adapter._mtproto_rich_request(edit_request(
            adapter._peer_for(source.chat_id), getattr(event, "message_id", 0), text,
            reply_markup=markup,
        ))
        return
    if (str(data) == "hm:autonomous:enable"
            or str(data) == "hm:autonomous:disable"
            or str(data) == "hm:autonomous:limits"
            or str(data).startswith("hm:autonomous:limits:")
            or str(data).startswith("hm:autonomous:set:")
            or str(data).startswith("hm:autonomous:models:")
            or str(data).startswith("hm:autonomous:resume:")
            or str(data).startswith("hm:autonomous:fresh:")):
        from .autonomous_callback import handle_autonomous_callback

        await handle_autonomous_callback(adapter, event, source, str(data))
        return
    await answer_callback(event, "This Telegram action is unavailable.", show_alert=True)
