"""Owner-controlled Telegram group membership alerts and scoped actions.

Only bot membership transitions are reported. Notifications are sent directly to
an already verified, configured profile Operations topic; this module never
creates or guesses a logging destination.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone, UTC
from html import escape
from pathlib import Path
from typing import Any


_ACTION_TTL_SECONDS = 600
_MAX_ACTIONS = 500


def _clean_label(value: Any, fallback: str, limit: int = 100) -> str:
    text = " ".join(str(value or "").split())
    return escape(text[:limit], quote=True) if text else escape(fallback, quote=True)


def _participant_state(participant: Any) -> str:
    name = type(participant).__name__.casefold() if participant is not None else ""
    if "banned" in name or "kicked" in name:
        return "banned"
    if participant is None or "left" in name:
        return "left"
    return "member"


def _transition(update: Any, bot_id: int) -> str | None:
    try:
        user_id = int(getattr(update, "user_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if user_id != int(bot_id):
        return None
    previous = _participant_state(getattr(update, "prev_participant", None))
    current = _participant_state(getattr(update, "new_participant", None))
    if current == "member" and previous in {"left", "banned"}:
        return "added"
    if current in {"left", "banned"} and previous == "member":
        return "banned" if current == "banned" else "removed"
    return None


def _event_time(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value, tz=UTC)
    else:
        return "Not provided by Telegram"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class TelegramGroupMembershipNotifier:
    """Emit content-free membership events to the verified private audit forum."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.actions: dict[str, dict[str, Any]] = getattr(adapter, "_group_membership_actions", {})
        adapter._group_membership_actions = self.actions

    def _controller(self):
        manager = getattr(self.adapter, "_forum_audit_transport", None)
        if manager is None:
            return None
        raw_home = getattr(self.adapter, "_telegram_transport_home", None)
        if raw_home in (None, ""):
            return None
        home = Path(raw_home).expanduser().resolve(strict=False)
        try:
            controller = manager._profile_controller(home)
        except Exception:
            return None
        service = getattr(controller, "_service", None) if controller is not None else None
        if (controller is None or getattr(controller, "_revoked", False)
                or service is None or getattr(service, "enabled", False) is not True):
            return None
        try:
            if int(service.operations_topic_id) <= 0 or int(service.forum_id) == 0:
                return None
        except (TypeError, ValueError, AttributeError):
            return None
        return controller

    def _is_owner(self, user_id: Any, home: Path) -> bool:
        try:
            from gateway.run import _profile_runtime_scope
            with _profile_runtime_scope(home):
                return bool(self.adapter._is_owner_user(str(user_id or "")))
        except Exception:
            return False

    async def handle_update(self, update: Any) -> None:
        """Report only an actual add/remove/ban transition for this bot account."""
        bot_id = getattr(self.adapter, "_bot_id", None)
        if bot_id is None:
            return
        kind = _transition(update, int(bot_id))
        if kind is None:
            return
        actor_id = str(getattr(update, "actor_id", "") or "")
        if not actor_id.isdecimal():
            return
        controller = self._controller()
        if controller is None:
            return
        home = controller.home
        # The configured owner may add the bot without generating an alert.
        if kind == "added" and self._is_owner(actor_id, home):
            return
        from telethon import types, utils
        raw_chat_id = getattr(update, "channel_id", None)
        is_channel = raw_chat_id is not None
        if is_channel:
            peer = types.PeerChannel(int(raw_chat_id))
        else:
            raw_chat_id = getattr(update, "chat_id", None)
            if raw_chat_id is None:
                return
            peer = types.PeerChat(int(raw_chat_id))
        try:
            chat_id = str(utils.get_peer_id(peer))
        except Exception:
            return
        try:
            chat = await self.adapter._client.get_entity(peer)
            chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
        except Exception:
            chat_title = None
        try:
            actor = await self.adapter._client.get_entity(int(actor_id))
            actor_name = " ".join(filter(None, [getattr(actor, "first_name", None), getattr(actor, "last_name", None)]))
            actor_name = actor_name or ("@" + str(getattr(actor, "username", "")).lstrip("@"))
        except Exception:
            actor_name = ""
        verb = {
            "added": "Naina was added",
            "removed": "Naina was removed",
            "banned": "Naina was banned",
        }[kind]
        reason = getattr(update, "reason", None)
        reason_text = (" ".join(str(reason).split())[:300] if isinstance(reason, str) and reason.strip()
                       else "Telegram did not provide a reason.")
        text = (
            "<h1>Telegram group activity</h1>"
            f"<p><b>Event:</b> {_clean_label(verb, 'Membership changed')}</p>"
            f"<p><b>Who:</b> {_clean_label(actor_name, 'Telegram user ' + actor_id)} "
            f"(<code>{escape(actor_id)}</code>)</p>"
            f"<p><b>Where:</b> {_clean_label(chat_title, 'Telegram chat ' + chat_id)} "
            f"(<code>{escape(chat_id)}</code>)</p>"
            f"<p><b>Chat type:</b> {'Channel' if is_channel else 'Group'}</p>"
            f"<p><b>When:</b> {_clean_label(_event_time(getattr(update, 'date', None)), 'Not provided by Telegram')}</p>"
            f"<p><b>Why:</b> {_clean_label(reason_text, 'Not provided by Telegram', 300)}</p>"
        )
        markup = None
        tokens: list[str] = []
        if kind == "added" and actor_id != str(bot_id) and actor_id.isdecimal():
            from .mtproto_rich import inline_markup
            now = time.monotonic()
            for old_token, old_state in list(self.actions.items()):
                if not isinstance(old_state, dict) or now - float(old_state.get("created", 0)) > _ACTION_TTL_SECONDS:
                    self.actions.pop(old_token, None)
            while len(self.actions) > _MAX_ACTIONS - 2:
                self.actions.pop(next(iter(self.actions)))
            rows = []
            for action, label in (("block", "Block this chat"), ("ban", "Ban inviter")):
                token = secrets.token_urlsafe(7)
                tokens.append(token)
                self.actions[token] = {
                    "created": now, "home": str(home), "chat_id": chat_id,
                    "chat_kind": "channel" if is_channel else "group",
                    "actor_id": actor_id, "action": action,
                }
                rows.append((label, f"gma:{token}", "danger"))
            markup = inline_markup([rows])
        try:
            peer = await self.adapter._input_peer_for(str(controller._service.forum_id))
            from .mtproto_rich import send_request
            request = send_request(
                peer, text, reply_to=int(controller._service.operations_topic_id),
                top_msg_id=int(controller._service.operations_topic_id),
                reply_markup=markup, no_webpage=True, rich_format="html",
            )
            response = await self.adapter._mtproto_rich_request(request)
            message_id = self.adapter._sent_message_id(
                response, peer=peer, topic_id=int(controller._service.operations_topic_id),
            )
            if message_id:
                for token in tokens:
                    self.actions[token]["message_id"] = str(message_id)
                    self.actions[token]["audit_chat_id"] = str(controller._service.forum_id)
                    self.actions[token]["topic_id"] = str(controller._service.operations_topic_id)
        except Exception as exc:
            for token in tokens:
                self.actions.pop(token, None)
            # Do not log the untrusted update, message text, or exception body.
            import logging
            logging.getLogger(__name__).warning(
                "Telegram group membership alert delivery failed (%s)", type(exc).__name__
            )

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        parts = str(data or "").split(":")
        if len(parts) not in {2, 3} or parts[0] != "gma":
            return False
        token = parts[1]
        state = self.actions.get(token)
        if not isinstance(state, dict):
            await event.answer("This group action expired or is unavailable.", alert=True)
            return True
        actor = str(getattr(source, "user_id", "") or "")
        home = Path(state.get("home", "")).expanduser().resolve(strict=False)
        owner = self.adapter._is_owner_user(actor, source=source)
        resolver = getattr(getattr(self.adapter, "gateway_runner", None), "_resolve_profile_home_for_source", None)
        try:
            resolved_home = resolver(source) if callable(resolver) else getattr(self.adapter, "_telegram_transport_home", None)
            resolved_home = Path(resolved_home).resolve(strict=False) if resolved_home is not None else None
        except (TypeError, ValueError, OSError):
            resolved_home = None
        valid = (
            owner and resolved_home == home
            and str(source.chat_id) == str(state.get("audit_chat_id", ""))
            and str(getattr(source, "thread_id", "") or "") == str(state.get("topic_id", ""))
            and str(getattr(event, "message_id", "")) == str(state.get("message_id", ""))
            and time.monotonic() - float(state.get("created", 0)) <= _ACTION_TTL_SECONDS
        )
        if not valid:
            await event.answer("Only the configured owner can use this action in its original audit topic.", alert=True)
            return True
        controller = self._controller()
        if controller is None or controller.home != home:
            await event.answer("The configured audit route is no longer available.", alert=True)
            return True
        step = parts[2] if len(parts) == 3 else "review"
        if step == "cancel":
            self.actions.pop(token, None)
            await event.answer("Cancelled. No change was made.")
            await self._edit(event, source, "<h2>Action cancelled</h2><p>No change was made.</p>", None)
            return True
        if step == "review":
            action = state.get("action")
            if action == "block":
                text = (
                    "<h2>Confirm chat block</h2>"
                    f"<p>Block chat <code>{escape(str(state['chat_id']))}</code>? "
                    "Messages in this exact group/channel will be silently ignored by Naina.</p>"
                )
                label = "Confirm block"
            elif action == "ban":
                text = (
                    "<h2>Confirm inviter ban</h2>"
                    f"<p>Ban Telegram user <code>{escape(str(state['actor_id']))}</code> "
                    f"from chat <code>{escape(str(state['chat_id']))}</code>? This affects only that user in that chat.</p>"
                )
                label = "Confirm ban"
            else:
                await event.answer("This action is unavailable.", alert=True)
                return True
            from .mtproto_rich import inline_markup
            markup = inline_markup([[
                (label, f"gma:{token}:confirm", "danger"),
                ("Cancel", f"gma:{token}:cancel", "primary"),
            ]])
            await event.answer("Review the action before confirming.")
            await self._edit(event, source, text, markup)
            return True
        if step != "confirm":
            await event.answer("This action is unavailable.", alert=True)
            return True
        action = state.get("action")
        try:
            if action == "block":
                from .telegram_chat_settings import update_chat_settings
                update_chat_settings(home, "group", str(state["chat_id"]), {"blocked": True})
                result_text = (
                    "<h2>Chat blocked</h2>"
                    f"<p>Messages from chat <code>{escape(str(state['chat_id']))}</code> are now silently ignored.</p>"
                )
            elif action == "ban":
                result_text = await self._ban_inviter(state)
            else:
                await event.answer("This action is unavailable.", alert=True)
                return True
        except Exception as exc:
            result_text = f"<h2>Action not applied</h2><p>No successful change was confirmed ({escape(type(exc).__name__)}).</p>"
        self.actions.pop(token, None)
        await event.answer("Action processed.")
        await self._edit(event, source, result_text, None)
        return True

    async def _ban_inviter(self, state: dict[str, Any]) -> str:
        from telethon import functions, types
        chat_id = str(state.get("chat_id", ""))
        actor_id = str(state.get("actor_id", ""))
        if not chat_id or not actor_id.isdecimal() or actor_id == str(getattr(self.adapter, "_bot_id", "")):
            raise ValueError("Invalid scoped ban target")
        if self._is_owner(actor_id, Path(state["home"])):
            return "<h2>Ban not applied</h2><p>The configured owner is excluded from this action.</p>"
        peer = await self.adapter._input_peer_for(chat_id)
        if not isinstance(peer, types.InputPeerChannel):
            return (
                "<h2>Ban not applied</h2>"
                "<p>Telegram does not support this ban request for a basic group chat; no change was made.</p>"
            )
        participant = await self.adapter._client.get_input_entity(int(actor_id))
        if not isinstance(participant, (types.InputPeerUser, types.InputPeerUserFromMessage)):
            raise ValueError("The inviter did not resolve to a Telegram user")
        channel = types.InputChannel(peer.channel_id, peer.access_hash)
        rights = types.ChatBannedRights(until_date=None, view_messages=True)
        try:
            await self.adapter._mtproto_rich_request(functions.channels.EditBannedRequest(
                channel=channel, participant=participant, banned_rights=rights,
            ))
        except Exception as exc:
            name = type(exc).__name__
            if name in {"UserAdminInvalidError", "UserCreatorError", "ParticipantIdInvalidError"}:
                detail = "Telegram will not allow banning this chat owner or administrator."
            elif name in {"ChatAdminRequiredError", "RightForbiddenError", "AdminRankInvalidError"}:
                detail = "The bot may not have permission to ban users in this chat."
            else:
                detail = f"Telegram rejected the ban ({escape(name)}); the ban was not applied."
            return f"<h2>Ban not applied</h2><p>{detail}</p>"
        return (
            "<h2>Ban applied</h2>"
            f"<p>Telegram confirmed user <code>{escape(actor_id)}</code> was banned from chat <code>{escape(chat_id)}</code>.</p>"
        )

    async def _edit(self, event: Any, source: Any, text: str, markup: Any) -> None:
        from .mtproto_rich import edit_request
        peer = await self.adapter._input_peer_for(str(source.chat_id))
        request = edit_request(peer, int(event.message_id), text, reply_markup=markup, no_webpage=True, rich_format="html")
        await self.adapter._mtproto_rich_request(request)
