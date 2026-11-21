"""Gateway /autonomous command: owner-only, DM-only autonomous mode switch.

Idle-only (``busy_policy="reject"`` in the registry): flipping the mode
mid-run would change the running turn's caps and its cached agent; /stop is
the mid-run control, and after /stop the owner can enable/disable cleanly.

Gating, in order, ALL SILENT on failure: Telegram only, DM only (the chat
is the actor's own DM), owner only. A non-owner / non-DM caller gets no
message, no reaction, and no log line naming the mode.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from agent.i18n import t
from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.run")

#: DM chat types, same set as the admission-branch pattern in
#: ``plugins/platforms/telegram/telegram_controls.py``.
_AUTONOMOUS_DM_CHAT_TYPES = frozenset({"dm", "private", "direct"})

_ENABLE_ARGS = frozenset({"enable", "on"})
_DISABLE_ARGS = frozenset({"disable", "off"})
_KILL_ARGS = frozenset({"kill"})


class GatewayAutonomousCommandsMixin:
    """``/autonomous`` — enable / disable / report autonomous mode."""

    async def _handle_autonomous_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /autonomous — owner-only, DM-only; silent denial otherwise."""
        source = getattr(event, "source", None)
        if source is None:
            return None
        raw_platform = getattr(source, "platform", None)
        platform = str(getattr(raw_platform, "value", raw_platform)).lower()
        if platform != "telegram":
            return None
        actor_id = str(getattr(source, "user_id", "") or "")
        if (str(getattr(source, "chat_type", "")) not in _AUTONOMOUS_DM_CHAT_TYPES
                or str(getattr(source, "chat_id", "")) != actor_id
                or not actor_id):
            return None
        try:
            adapter = self._delivery_adapter_for(source)
        except Exception:  # health: allow BLE001 -- a failing gate must deny silently, never raise into the runner (which would surface a reply to a caller who must get nothing)
            return None
        if adapter is None:
            return None
        checker = getattr(adapter, "_is_owner_user", None)
        if not callable(checker):
            return None
        try:
            if not checker(actor_id, source):
                return None
        except Exception:  # health: allow BLE001 -- the owner check is an adapter-provided callable; any failure denies silently
            return None

        from gateway import autonomous_state
        from hermes_constants import get_routing_process_hermes_home

        home = get_routing_process_hermes_home()
        raw_args = event.get_command_args().strip()
        parts = raw_args.split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        goal = parts[1].strip() if len(parts) > 1 else ""

        if action in _DISABLE_ARGS or action in _KILL_ARGS:
            return await self._handle_autonomous_end(adapter, event, source, home, action)
        if action == "":
            return await self._report_autonomous_status(adapter, event, source, home)
        if action not in _ENABLE_ARGS:
            return t("gateway.autonomous.usage")

        # Re-enable with a previous run to resume: ask first, change nothing
        # until the owner chooses. A live run skips the ask — re-enable
        # replaces it, as before.
        if await self._maybe_ask_autonomous_resume(adapter, event, source, home):
            return None

        # Enable: persist the master switch first — a config write failure
        # fails closed with a plain reply and changes nothing.
        try:
            from plugins.platforms.telegram.personal_settings_ui import TelegramPersonalSettingsUI

            TelegramPersonalSettingsUI._set_autonomous_mode(home, True)
        except Exception:
            logger.warning("Could not persist autonomous_mode setting", exc_info=True)
            return t("gateway.autonomous.save_failed")

        # A live run is replaced, never stacked: stop the old run (its
        # record is kept as the resume source) and drop its pinned message.
        if autonomous_state.is_active(home):
            pinned = autonomous_state.disable(home, stopped_by="autonomous_reenable")
            if pinned is not None:
                from plugins.platforms.telegram.autonomous_callback import unpin_and_delete_pinned

                await unpin_and_delete_pinned(adapter, pinned)

        # A fresh run starts here; a resumable previous run was already
        # offered above (the resume ask) and the owner chose fresh, or
        # there was none.
        try:
            session_key = self._session_key_for_source(
                await asyncio.to_thread(self._normalize_source_for_session_key, source))
            record = autonomous_state.create_run(home, session_key, goal)
            autonomous_state.start_run(home, record["run_id"])
        except Exception:
            logger.warning("Could not start the autonomous run", exc_info=True)
            return t("gateway.autonomous.start_failed")

        # Send the enable confirmation and pin it; the card carries the
        # green tag (it is a message of the mode) and a Disable button.
        # A failed card falls back to the plain reply below.
        message_id = await self._send_autonomous_enable_card(adapter, event, source)
        if message_id is not None:
            try:
                await adapter.pin_chat_message(chat_id=source.chat_id, message_id=message_id)
            except Exception:
                logger.debug("Autonomous enable pin failed", exc_info=True)
            autonomous_state.set_active(home, True, run_id=record["run_id"],
                                        pinned_message_id=message_id, chat_id=source.chat_id)
            return None
        return t("gateway.autonomous.status_on")

    async def _handle_autonomous_end(self, adapter: Any, event: MessageEvent,
                                     source: Any, home: Any, action: str) -> Optional[str]:
        """Terminal /autonomous verbs: disable and kill.

        Disable stops the flag and keeps the run as the resume source;
        kill is the kill-switch (backbone §4) — it ends the coordinator,
        the workers, the queued work and any revival, and the run is never
        offered as Resume. One branch here so the dispatch stays under its
        complexity cap; the two verbs differ only in which end-state the
        run gets.
        """
        from gateway import autonomous_state

        if action in _KILL_ARGS:
            return await self._handle_autonomous_kill(adapter, event, source, home)
        pinned = autonomous_state.disable(home, stopped_by="autonomous_disable")
        if pinned is not None:
            from plugins.platforms.telegram.autonomous_callback import unpin_and_delete_pinned

            await unpin_and_delete_pinned(adapter, pinned)
        # The master setting flips off too, so the settings row agrees
        # with the live state; a failed write is logged, never an error
        # — the disable itself already stands (see clear_master_setting).
        from plugins.platforms.telegram.autonomous_callback import clear_master_setting

        clear_master_setting(home)
        # The response message itself says the mode is disabled; the
        # pinned enable message is already unpinned + deleted above.
        return t("gateway.autonomous.disabled")

    async def _handle_autonomous_kill(self, adapter: Any, event: MessageEvent,
                                      source: Any, home: Any) -> Optional[str]:
        """Idle /autonomous kill: the kill-switch (backbone §4).

        Ends the coordinator, the workers, the queued work and any
        revival, and the run is never offered as Resume. Idle here means
        no turn is running — a waiting run is killed the same way; the
        busy path in run_busy.py handles the mid-run case.
        """
        from plugins.platforms.telegram.autonomous_callback import (
            kill_autonomous_run, kill_summary_text)

        summary = await kill_autonomous_run(self, source, via="kill_command")
        return kill_summary_text(summary)

    async def _maybe_ask_autonomous_resume(self, adapter: Any, event: MessageEvent,
                                           source: Any, home: Any) -> bool:
        """Ask to resume a previous run; True when the ask was delivered.

        A live run skips the ask (re-enable replaces it). An undeliverable
        card returns False so the caller falls through to a fresh run
        (the previous behaviour) rather than leaving the owner unable to
        enable at all; the old record survives for the next ask.
        """
        from gateway import autonomous_state

        if autonomous_state.is_active(home):
            return False
        resumable = autonomous_state.find_resumable_run(home)
        if resumable is None:
            return False
        asked = await self._send_autonomous_resume_card(adapter, event, source, resumable)
        if not asked:
            logger.debug("Autonomous resume ask undelivered; starting a fresh run")
        return asked

    async def _send_autonomous_resume_card(self, adapter: Any, event: MessageEvent,
                                           source: Any, run: dict) -> bool:
        """Send the resume-ask card; True when it was delivered.

        The card names the previous run (goal, where it stopped, steps,
        tokens) and offers Resume (re-open the record) or Start fresh (a
        new run on the same goal). No state changes on this path — the
        choice tap does the work.
        """
        from plugins.platforms.telegram.command_cards import lines_to_rich_html
        from plugins.platforms.telegram.mtproto_rich import rich_button, rich_button_row

        state_word = {"stopped": "stopped", "failed": "failed",
                      "needs_input": "is waiting for your input"}.get(
                          str(run.get("state") or ""), str(run.get("state") or ""))
        steps = int(run.get("steps") or 0)
        tokens = int(run.get("input_tokens") or 0) + int(run.get("output_tokens") or 0)
        tag_row = rich_button_row(
            [rich_button("autonomous", kind="disabled", style="success")])
        html = (tag_row + "\n" + lines_to_rich_html(
            t("gateway.autonomous.resume_title"),
            [t("gateway.autonomous.resume_body", goal=str(run.get("goal") or ""),
               state=state_word, steps=steps, tokens=tokens)])
            + "\n" + rich_button_row([
                rich_button("Resume", kind="callback",
                            value=f"hm:autonomous:resume:{run['run_id']}",
                            style="primary"),
                rich_button("Start fresh", kind="callback",
                            value=f"hm:autonomous:fresh:{run['run_id']}",
                            style="primary")]))
        send_card = getattr(adapter, "send_command_card", None)
        if not callable(send_card):
            return False
        try:
            message_id = await send_card(
                source=source, html=html, buttons=(),
                reply_to=self._reply_anchor_for_event(event))
        except Exception:
            logger.debug("Autonomous resume-ask card send failed", exc_info=True)
            return False
        return message_id is not None

    async def _report_autonomous_status(self, adapter: Any, event: MessageEvent,
                                        source: Any, home: Any) -> Optional[str]:
        """Bare /autonomous: rich status card (badge, last sessions, token
        burn, Enable/Disable/Resume buttons). A failed card falls back to
        the plain reply."""
        from gateway import autonomous_state

        message_id = await self._send_autonomous_status_card(adapter, event, source)
        if message_id is not None:
            return None
        active = autonomous_state.is_active(home)
        return t("gateway.autonomous.status_on" if active
                 else "gateway.autonomous.status_off")

    async def _send_autonomous_status_card(self, adapter: Any, event: MessageEvent,
                                           source: Any) -> Optional[str]:
        """Send the bare-/autonomous status card; the sent message id, or None."""
        from plugins.platforms.telegram.autonomous_callback import send_status_card

        return await send_status_card(
            adapter, source, reply_to=self._reply_anchor_for_event(event))

    async def _send_autonomous_enable_card(self, adapter: Any, event: MessageEvent,
                                           source: Any) -> Optional[str]:
        """Send the enable confirmation card; the sent message id, or None."""
        from plugins.platforms.telegram.autonomous_callback import send_enable_card

        return await send_enable_card(
            adapter, source, reply_to=self._reply_anchor_for_event(event))
