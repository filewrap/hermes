"""``hm:`` action callbacks dispatched by their ``:``-separated parts.

Extracted from ``TelethonAdapter._on_callback_query`` so that function's
complexity stays at its on-main value: owner callbacks, help paging,
personal-settings open, the guestbot on/off toggle, the sessions Delete
confirm flow, the autonomous Disable button, and the generic "unavailable"
fallback. Every path returns; a caller that reaches here has already passed
the ``hm:`` prefix ladder (control UI, busy/resume/rename/pages/reasoning).
"""
from __future__ import annotations

import html
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
        await _rerender_guestbot_menu(adapter, event, source)
        return
    if parts[1] == "guestbot" and len(parts) == 4 and parts[2] == "save" and parts[3] in {"on", "off"}:
        enabled = parts[3] == "on"
        try:
            adapter._persist_guest_save_history_setting(source, enabled)
        except Exception:
            logger.warning("Could not persist Telegram GuestBot save-history setting", exc_info=True)
            await answer_callback(event, "Could not save this setting to the active profile.", show_alert=True)
            return
        await answer_callback(
            event, f"Guest messages {'will be saved' if enabled else 'will not be saved'}.")
        await _rerender_guestbot_menu(adapter, event, source)
        return
    if parts[1] == "guestbot" and len(parts) >= 3 and parts[2] == "clear":
        await handle_guest_clear_callback(adapter, event, source, str(data))
        return
    if parts[1] == "delete":
        await handle_delete_callback(adapter, event, source, str(data))
        return
    if (str(data) == "hm:autonomous:enable"
            or str(data) == "hm:autonomous:disable"
            or str(data) == "hm:autonomous:status"
            or str(data) == "hm:autonomous:sessions"
            or str(data).startswith("hm:autonomous:sessions:")
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


async def _rerender_guestbot_menu(adapter: Any, event: Any, source: Any) -> None:
    """Edit the tapped message back to the GuestBot settings menu."""
    from .mtproto_rich import edit_request

    text, markup = adapter._guestbot_menu()
    await adapter._mtproto_rich_request(edit_request(
        adapter._peer_for(source.chat_id), getattr(event, "message_id", 0), text,
        reply_markup=markup,
    ))


async def handle_guest_clear_callback(adapter: Any, event: Any, source: Any, data: str) -> None:
    """Guest history clear: ``hm:guestbot:clear`` arms a confirm card,
    ``hm:guestbot:clear:confirm:<token>`` runs it, ``hm:guestbot:clear:cancel``
    restores the menu. Owner-only — it deletes the owner's stored sessions."""
    parts = str(data or "").split(":")
    if not adapter._is_owner_user(source.user_id, source=source):
        await answer_callback(event, "Only the configured owner can clear guest history.",
                              show_alert=True)
        return
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        await answer_callback(event, "This action is unavailable.", show_alert=True)
        return
    if len(parts) == 4 and parts[3] == "cancel":
        await answer_callback(event)
        await _rerender_guestbot_menu(adapter, event, source)
        return
    if len(parts) == 5 and parts[3] == "confirm":
        await _confirm_guest_clear(adapter, event, source, parts[4], runner)
        return
    if len(parts) == 3:
        await _arm_guest_clear(adapter, event, source, runner)
        return
    await answer_callback(event, "This Telegram action is unavailable.", show_alert=True)


async def _arm_guest_clear(adapter: Any, event: Any, source: Any, runner: Any) -> None:
    """Replace the menu with the clear confirm card (destructive)."""
    from .mtproto_rich import edit_request, inline_markup

    armed = await runner.arm_guest_history_clear(source)
    if armed is None:
        await answer_callback(event, "Guest history is unavailable.", show_alert=True)
        return
    token, session_count, message_count = armed
    if session_count == 0:
        await answer_callback(event, "No guest history to clear.")
        return
    sessions = "session" if session_count == 1 else "sessions"
    text = (f"<h1>Clear guest history?</h1>"
            f"<p>Delete {session_count} guest {sessions} ({message_count} messages)? "
            "This cannot be undone.</p>")
    markup = inline_markup([
        [("Yes, clear", f"hm:guestbot:clear:confirm:{token}", "danger")],
        [("Cancel", "hm:guestbot:clear:cancel", None)],
    ])
    await answer_callback(event)
    await adapter._mtproto_rich_request(edit_request(
        adapter._peer_for(source.chat_id), getattr(event, "message_id", 0),
        text, reply_markup=markup))


async def _confirm_guest_clear(adapter: Any, event: Any, source: Any,
                               confirm_token: str, runner: Any) -> None:
    """Run the armed clear, toast the outcome, and restore the menu in place."""
    from .callback_answer import answer_or_note

    outcome = await runner.clear_guest_history_confirmed(source, confirm_token)
    if outcome is None:
        await answer_or_note(adapter, event, "This clear request expired. Open /guestbot again.")
        return
    deleted = int(outcome.get("deleted_sessions") or 0)
    messages = int(outcome.get("deleted_messages") or 0)
    skipped = int(outcome.get("skipped") or 0)
    sessions = "session" if deleted == 1 else "sessions"
    note = f"Cleared {deleted} guest {sessions} ({messages} messages)."
    if skipped:
        note += f" Skipped {skipped} live {'session' if skipped == 1 else 'sessions'}."
    await answer_callback(event, note)
    await _rerender_guestbot_menu(adapter, event, source)


def _delete_page(raw: str) -> int:
    """Listing page from a callback part (1 when unparseable)."""
    try:
        return int(raw)
    except ValueError:
        return 1


async def handle_delete_callback(adapter: Any, event: Any, source: Any, data: str) -> None:
    """Sessions Delete flow: ``hm:delete:<page_token>:<page>:<id>`` arms a confirm card,
    ``hm:delete:confirm:<token>`` runs the armed delete, ``hm:delete:cancel:<page_token>:<page>``
    restores the listing. Owner-only; the runner re-checks session visibility at arm time and
    refuses a live session at confirm time."""
    parts = str(data or "").split(":")
    if not adapter._is_owner_user(source.user_id, source=source):
        await answer_callback(event, "Only the configured owner can delete sessions.",
                              show_alert=True)
        return
    runner = getattr(adapter, "gateway_runner", None)
    if runner is None:
        await answer_callback(event, "This action is unavailable.", show_alert=True)
        return
    if len(parts) == 4 and parts[2] == "confirm":
        await _confirm_session_delete(adapter, event, source, parts[3], runner)
        return
    if len(parts) == 5 and parts[2] == "cancel":
        await _rerender_sessions_listing(
            adapter, event, source, parts[3], _delete_page(parts[4]), runner)
        return
    if len(parts) == 5:
        await _arm_session_delete(
            adapter, event, source, parts[2], _delete_page(parts[3]), parts[4], runner)
        return
    await answer_callback(event, "This Telegram action is unavailable.", show_alert=True)


async def _source_with_card_thread(adapter: Any, event: Any, source: Any) -> Any:
    """Source with the thread of the tapped card.

    A Telethon CallbackQuery carries no message object, so ``_source`` falls
    back to the General topic (thread_id=1) for every forum tap, while the
    session the card lists carries the real topic id — the IDOR guard then
    denies the tap. Read the thread off the tapped card's own message, as
    ``_callback_card_thread_id`` does for the /new-confirm and exec-approval
    guards. Falls back to the source unchanged when the adapter cannot
    resolve it (test doubles, non-forum taps, unresolvable cards).
    """
    resolve = getattr(adapter, "_callback_card_thread_id", None)
    if not callable(resolve):
        return source
    thread_id = await resolve(event, source)
    if thread_id is None or str(thread_id) == str(getattr(source, "thread_id", None)):
        return source
    import dataclasses

    try:
        return dataclasses.replace(source, thread_id=str(thread_id))
    except TypeError:
        # A non-dataclass source (test doubles) has no replaceable thread.
        return source


async def _arm_session_delete(adapter: Any, event: Any, source: Any, page_token: str,
                              page: int, session_id: str, runner: Any) -> None:
    """Replace the listing with the delete confirm card (destructive, id retired permanently)."""
    from .mtproto_rich import edit_request, inline_markup

    source = await _source_with_card_thread(adapter, event, source)
    armed = await runner.arm_session_delete(source, session_id, page_token, page)
    if armed is None:
        await answer_callback(event, "This session cannot be deleted.", show_alert=True)
        return
    confirm_token, title = armed
    text = (f"<h1>Delete session?</h1>"
            f"<p>Delete <b>{html.escape(title, quote=False)}</b>? "
            "This cannot be undone — the session id is retired permanently.</p>")
    markup = inline_markup([
        [("Yes, delete", f"hm:delete:confirm:{confirm_token}", "danger")],
        [("Cancel", f"hm:delete:cancel:{page_token}:{page}", None)],
    ])
    await answer_callback(event)
    await adapter._mtproto_rich_request(edit_request(
        adapter._peer_for(source.chat_id), getattr(event, "message_id", 0),
        text, reply_markup=markup))


async def _confirm_session_delete(adapter: Any, event: Any, source: Any,
                                  confirm_token: str, runner: Any) -> None:
    """Run the armed delete, toast the outcome, and refresh the listing in place."""
    outcome = await runner.delete_session_confirmed(source, confirm_token)
    if outcome is None:
        from .callback_answer import answer_or_note

        await answer_or_note(adapter, event, "This delete request expired. Run /sessions again.")
        return
    if outcome["status"] == "active":
        await answer_callback(event, outcome["message"], show_alert=True)
        return
    await answer_callback(event, outcome["message"])
    await _rerender_sessions_listing(
        adapter, event, source, outcome["page_token"], outcome["page"], runner)


async def _rerender_sessions_listing(adapter: Any, event: Any, source: Any, page_token: str,
                                     page: int, runner: Any) -> None:
    """Edit the listing message back to the sessions list at *page* (cancel, or after a delete)."""
    from .callback_answer import answer_or_note
    from .command_cards import with_action_buttons
    from .mtproto_rich import edit_request

    rendered = runner.render_sessions_listing_page(page_token, page)
    if rendered is None:
        await answer_or_note(adapter, event, "This listing expired. Run /sessions again.")
        return
    html_text, copies = rendered
    await answer_callback(event)
    await adapter._mtproto_rich_request(edit_request(
        adapter._peer_for(source.chat_id), getattr(event, "message_id", 0),
        with_action_buttons(html_text, copies=copies)))
