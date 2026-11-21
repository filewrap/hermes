"""The browser vault's one-time-code prompt on Telegram: a reply the bot deletes on receipt.

Telegram has no private form a bot can open, so the code arrives as a normal message. The gateway asks
through ``send_vault_code_prompt`` (``gateway/session_vault_code_card.py``); the card is a ForceReply
prompt aimed at the turn's author. Their next text in that chat (a reply to the card, or any message in a
private chat) is taken off the inbound path before batching or the agent see it: the bot deletes it, hands
the code to ``submit``, and never turns it into a MessageEvent. Bots may delete incoming messages in private
chats; in a group the bot needs delete rights, and the author is told to remove it when the delete fails.

Telethon port of upstream's PTB-shaped ``telegram_vault_code`` (new in the v0.23.x gap): the behaviour
is identical, the transport is MTProto — ``ReplyKeyboardForceReply`` instead of PTB's ``ForceReply``,
the card goes out through ``mtproto_rich.send_request`` (``rich_format="html"``) with the topic id
from ``_reply_top_target``, the delete is the adapter's own ``delete_message`` (its ``True`` means
the delete RPC succeeded, so there is no flood-queued case to distinguish), and the card edit uses
``mtproto_rich.edit_request``.

Upstream's ``_DropPendingCodeUpdates`` log filter is intentionally NOT ported: it exists because
python-telegram-bot dumps every getUpdates result and webhook body at DEBUG before any handler
runs. The MTProto path has no getUpdates/webhook dump — Telethon logs no inbound plaintext at
DEBUG, and the fork's inbound path logs ids only — so there is nothing to filter. The code itself
is never logged by this mixin (cleared right after ``submit``), and interception happens before
the message reaches batching, so the code never becomes a turn either.

Wiring (not in this file): add ``TelegramVaultCodeMixin`` to the Telethon adapter's bases and
call ``consume_vault_code_reply(message)`` at the top of the inbound path (before
``_process_inbound_serialized`` batches the message). Required adapter surface: ``_client``,
``_peer_for``, ``delete_message``, ``_reply_top_target`` (``_sent_message_id`` used when present).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from gateway.platforms.base import SendResult

logger = logging.getLogger(__name__)

_HINT_LIMIT = 300
_PLACEHOLDER_LIMIT = 64  # Bot API cap on ForceReply.input_field_placeholder; kept for the same UX


@dataclass
class _CodeReply:
    submit: Callable[[str], bool]
    user_id: str
    chat_id: str
    prompt_message_id: str
    prompt_text: str
    expires_at: float


def _t(key: str, **kwargs: Any) -> str:
    from agent.i18n import t
    return t(key, **kwargs)


_RETIRED_MAX = 512  # cards whose late replies are still swallowed (bounded; oldest dropped first)


class TelegramVaultCodeMixin:
    """``send_vault_code_prompt`` (the capability the gateway detects) and the inbound interception."""

    _client: Any

    def _vault_code_replies(self) -> dict:
        """Live cards keyed by ``(chat_id, card message_id)``: each has its own record and its own timer."""
        replies = getattr(self, "_vault_code_reply_state", None)
        if replies is None:
            replies = self._vault_code_reply_state = {}
        return replies

    def _retired_vault_code_cards(self) -> dict:
        """Cards that ended, ``(chat_id, card message_id) -> author user_id`` in retirement order: the author's
        late reply to one is still a code and must never become chat text."""
        retired = getattr(self, "_vault_code_retired_state", None)
        if retired is None:
            retired = self._vault_code_retired_state = {}
        return retired

    def _vault_code_message_id(self, response: Any, request: Any, peer: Any, topic_id: Any) -> Optional[str]:
        """Message id of a sent card: the adapter's receipt parser when present, else the updates."""
        parse = getattr(self, "_sent_message_id", None)
        if callable(parse):
            try:
                message_id = parse(response, random_id=getattr(request, "random_id", None),
                                   peer=peer, topic_id=topic_id)
                if message_id is not None:
                    return str(message_id)
            except Exception:
                logger.debug("[%s] vault code card id parse failed", getattr(self, "name", "Telegram"),
                             exc_info=True)
        for update in getattr(response, "updates", None) or ():
            message = getattr(update, "message", None)
            message_id = getattr(message, "id", None)
            if message_id is not None:
                return str(message_id)
        message_id = getattr(response, "id", None)
        return str(message_id) if message_id is not None else None

    async def send_vault_code_prompt(self, chat_id: str, site: str, hint: str, *, user_id: str,
                                     submit: Callable[[str], bool], timeout_s: float,
                                     metadata: Optional[dict] = None) -> SendResult:
        from telethon import types as _tl_types
        from plugins.platforms.telegram.mtproto_rich import send_request

        text = f"<b>{html.escape(_t('platform.shared.vault_code.title', site=site))}</b>\n" + html.escape(
            _t("platform.telegram.vault_code.body", site=site))
        if hint:
            text += "\n" + html.escape(hint[:_HINT_LIMIT])
        if str(chat_id).startswith("-"):
            # In a group, ForceReply(selective) opens the reply field only for users the card mentions.
            text = f'<a href="tg://user?id={int(user_id)}">\u2063</a>' + text
        reply_markup = _tl_types.ReplyKeyboardForceReply(
            selective=True, placeholder=_t("platform.shared.vault_code.input_label")[:_PLACEHOLDER_LIMIT])
        topic_id = self._reply_top_target(metadata)
        peer = self._peer_for(chat_id)
        request = send_request(peer, text, top_msg_id=topic_id, reply_markup=reply_markup,
                               rich_format="html")
        try:
            response = await self._client(request)
        except Exception as exc:  # health: allow BLE001 -- a prompt-send failure is a failed SendResult, never a raise
            return SendResult(success=False, error=f"Telegram vault code prompt failed: {type(exc).__name__}")
        message_id = self._vault_code_message_id(response, request, peer, topic_id)
        if message_id is None:
            return SendResult(success=False, error="Telegram vault code prompt returned no message_id")
        key = (str(chat_id), message_id)
        self._vault_code_replies()[key] = _CodeReply(
            submit=submit, user_id=str(user_id), chat_id=str(chat_id),
            prompt_message_id=message_id, prompt_text=text, expires_at=time.monotonic() + timeout_s)
        asyncio.get_running_loop().call_later(
            timeout_s, lambda: asyncio.ensure_future(self._retire_vault_code_reply(key, "ended")))
        return SendResult(success=True, message_id=message_id)

    def _pop_vault_code_reply(self, key: tuple) -> Optional[_CodeReply]:
        reply = self._vault_code_replies().pop(key, None)
        if reply is not None:
            retired = self._retired_vault_code_cards()
            retired[key] = reply.user_id
            while len(retired) > _RETIRED_MAX:
                retired.pop(next(iter(retired)))
        return reply

    async def _retire_vault_code_reply(self, key: tuple, outcome: str) -> None:
        reply = self._pop_vault_code_reply(key)
        if reply is not None:
            await self._mark_vault_code_card(reply, outcome)

    async def _mark_vault_code_card(self, reply: _CodeReply, outcome: str) -> None:
        notice = _t("platform.shared.vault_code.submitted_footer" if outcome == "submitted"
                    else "platform.shared.vault_code.ended")
        try:
            from plugins.platforms.telegram.mtproto_rich import edit_request
            await self._client(edit_request(
                self._peer_for(reply.chat_id), int(reply.prompt_message_id),
                f"{reply.prompt_text}\n\n{notice}", rich_format="html"))
        except Exception:  # health: allow BLE001 -- the card's outcome line is cosmetic; the request is already settled
            logger.debug("[%s] vault code card edit failed", getattr(self, "name", "Telegram"), exc_info=True)

    def _match_vault_code_card(self, msg) -> tuple[Optional[tuple], bool]:
        """``(live card key or None, is the author's reply to an ended card)`` for an inbound text.

        One rule: only the card's author can answer it. A reply aimed at a known card (live or ended) counts
        only from that author; any other private-chat text from the author answers their newest live card.

        Reads the Telethon ``Message`` shape (``sender_id`` / ``reply_to_msg_id`` / ``is_private`` /
        ``id``), with the PTB attribute names as fallbacks so test doubles of either shape work.
        """
        chat_id = str(getattr(msg, "chat_id", "") or "")
        user_id = str(getattr(msg, "sender_id", None)
                      or getattr(getattr(msg, "from_user", None), "id", "") or "")
        replied_to = getattr(msg, "reply_to_msg_id", None)
        if replied_to is None:
            replied_to = getattr(getattr(msg, "reply_to_message", None), "message_id", None)
        now = time.monotonic()
        if replied_to is not None:
            key = (chat_id, str(replied_to))
            reply = self._vault_code_replies().get(key)
            if reply is not None and reply.user_id == user_id:
                return (key if now < reply.expires_at else None), now >= reply.expires_at
            if self._retired_vault_code_cards().get(key) == user_id:
                return None, True
        is_private = getattr(msg, "is_private", None)
        if is_private is None:
            is_private = str(getattr(getattr(msg, "chat", None), "type", "")) == "private"
        if not is_private:
            return None, False
        live = [k for k, r in self._vault_code_replies().items()
                if k[0] == chat_id and r.user_id == user_id and now < r.expires_at]
        return (live[-1] if live else None), False

    async def consume_vault_code_reply(self, msg) -> bool:
        """True when ``msg`` answered a code card (live or ended): deleted, never a turn."""
        key, to_ended_card = self._match_vault_code_card(msg)
        reply = self._pop_vault_code_reply(key) if key is not None else None
        if reply is None and not to_ended_card:
            return False
        chat_id = str(getattr(msg, "chat_id", "") or "")
        message_id = str(getattr(msg, "id", None) or getattr(msg, "message_id", "") or "")
        code = (getattr(msg, "text", "") or "").strip()
        # Confirmed: a delete queued behind a flood wait must not read as done, or the warning never goes out.
        # The Telethon adapter's delete returns True only once the delete RPC succeeded.
        deleted = await self.delete_message(chat_id, message_id) if message_id else False
        accepted = reply is not None and bool(code) and reply.submit(code)
        code = ""
        if reply is not None:
            await self._mark_vault_code_card(reply, "submitted" if accepted else "ended")
        if reply is not None:
            notice_card = reply
        else:  # an ended-card reply always carries a reply target
            target = getattr(msg, "reply_to_msg_id", None)
            if target is None:
                target = getattr(getattr(msg, "reply_to_message", None), "message_id", "")
            notice_card = _CodeReply(submit=lambda _c: False, user_id="", chat_id=chat_id,
                                     prompt_message_id=str(target), prompt_text="", expires_at=0.0)
        if not deleted:
            await self._vault_code_notice(notice_card, "platform.telegram.vault_code.delete_failed")
        if not accepted:
            await self._vault_code_notice(notice_card, "platform.shared.vault_code.ended")
        return True

    async def _vault_code_notice(self, reply: _CodeReply, key: str) -> None:
        try:
            await self._client.send_message(
                self._peer_for(reply.chat_id), _t(key),
                reply_to=int(reply.prompt_message_id))
        except Exception:  # health: allow BLE001 -- a notice the chat refuses must not fail the interception
            logger.debug("[%s] vault code notice failed", getattr(self, "name", "Telegram"), exc_info=True)
