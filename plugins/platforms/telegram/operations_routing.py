"""Dedicated routing for Telegram client controls and supervised bot peers."""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

CLIENT_COMMANDS = frozenset({"poll", "tgmessages", "tgfiles", "tginfo", "tgadmin", "tgprofile"})
BOT_COMMANDS = frozenset({"managedbots", "botpeers"})
TELEGRAM_OPERATION_COMMANDS = CLIENT_COMMANDS | BOT_COMMANDS


class TelegramOperationsRoutingMixin:
    """Keep client control dispatch separate from setup and conversational commands."""

    def _client_operations_ui(self):
        ui = getattr(self, "_telegram_client_operations_ui", None)
        if ui is None:
            from .client_operations_ui import TelegramClientOperationsUI
            ui = self._telegram_client_operations_ui = TelegramClientOperationsUI(self)
        return ui

    def _bot_operations_ui(self):
        ui = getattr(self, "_telegram_bot_operations_ui", None)
        if ui is None:
            from .bot_operations_ui import TelegramBotOperationsUI
            ui = self._telegram_bot_operations_ui = TelegramBotOperationsUI(self)
        return ui

    async def _on_managed_bot_update(self, update: Any) -> None:
        # Raw manager updates belong to this transport, never to a child creator's
        # potentially different session/profile route. Tokens are not retrieved.
        home = getattr(self, "_telegram_transport_home", None)
        if home is None or getattr(self, "_client", None) is None:
            return
        try:
            await self._bot_operations_ui().operations.handle_managed_update(update, profile_home=home)
        except Exception as exc:
            logger.warning("Telegram managed-bot update was not recorded (%s)", type(exc).__name__)

    def _observe_telegram_message(self, source: Any, message: Any) -> None:
        from gateway.session_identity import replace_source
        from gateway.session import SessionSource
        ui = self._client_operations_ui()
        ui.observe(source, message)
        if not isinstance(source, SessionSource):
            return  # synthetic sources have no native event identity to retain
        sources = getattr(self, "_telegram_observed_sources", None)
        if sources is None:
            sources = self._telegram_observed_sources = OrderedDict()
        key = (str(self._settings_home(source)), str(source.chat_id), str(source.thread_id or ""))
        sources[key] = replace_source(source)
        sources.move_to_end(key)
        while len(sources) > 256:
            sources.popitem(last=False)

    async def _on_poll_update(self, update: Any) -> None:
        # Never enumerate chats or request user-only poll methods. A verified
        # Telegram update can refresh only a poll already in this transport's cache.
        ui = getattr(self, "_telegram_client_operations_ui", None)
        if ui is None:
            return
        for source in list(getattr(self, "_telegram_observed_sources", {}).values()):
            try:
                ui.operations.observe_poll_update(source, update)
            except Exception as exc:
                logger.debug("Telegram poll cache scope is unavailable (%s)", type(exc).__name__)

    async def _handle_telegram_operation_command(self, message: Any, source: Any,
                                                 command: str, arguments: str,
                                                 *, reply_to=None) -> bool:
        if command not in TELEGRAM_OPERATION_COMMANDS:
            return False
        # Existing isolated-tenant restrictions remain unchanged. These controls
        # expose owner capabilities, not a new way around tenant command admission.
        if (getattr(source, "is_bot", False) or getattr(source, "profile_route_rejected", False)
                or not self._authorized(source) or not self._is_owner_user(source.user_id, source=source)):
            await self._send_rich_ui(source, "<p>Only the configured human owner can use these Telegram controls.</p>", reply_to=reply_to)
            return True
        ui = self._client_operations_ui() if command in CLIENT_COMMANDS else self._bot_operations_ui()
        await ui.handle_command(message, source, command, arguments, reply_to=reply_to)
        return True

    async def request_telegram_operation(self, source: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Agent tool entry point; mutations still require a native human confirmation."""
        if (getattr(source, "is_bot", False) or getattr(source, "profile_route_rejected", False)
                or not self._authorized(source) or not self._is_owner_user(source.user_id, source=source)):
            return {"error": "Only the configured human owner may request Telegram operations."}
        if action == "capabilities":
            from .bot_operations import BOT_OPERATION_PARAMETERS
            client = await self._client_operations_ui().request(source, action, {})
            bots = await self._bot_operations_ui().request(source, action, {})
            bot_parameters = {name: fields for name, fields in BOT_OPERATION_PARAMETERS.items()
                              if name not in {"capabilities", "managed.set_info"}}
            bots.update(actions=sorted(bot_parameters), parameters=bot_parameters,
                        manager_mode_enabled=bool(getattr(self, "_bot_can_manage_bots", False)),
                        token_export_exposed=False,
                        bot_to_bot="DM requires BOTH bots' BotFather bot-to-bot mode. Groups require supported mention/reply or admin/privacy visibility; local exact-chat grants remain mandatory. Incoming bots are inbox-only, never autonomous model turns.")
            return {"success": True, "client": client, "bots": bots,
                    "commands": sorted(TELEGRAM_OPERATION_COMMANDS),
                    "scope": "current profile and current chat; separately approved bot-peer routes only",
                    "confirmation": "Mutating requests return pending confirmation. Approve the exact Telegram card; a tool cannot confirm it."}
        from .client_operations import ALL_ACTIONS
        if action in ALL_ACTIONS:  # bot_profile* are client self-configuration, not bot-peer aliases
            return await self._client_operations_ui().request(source, action, params)
        if action.startswith(("bot_", "managed_", "peer_", "managed.", "peers.", "peer.", "inbox.")):
            return await self._bot_operations_ui().request(source, action, params)
        return await self._client_operations_ui().request(source, action, params)
