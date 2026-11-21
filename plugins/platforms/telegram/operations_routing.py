"""Dedicated routing for Telegram client controls and supervised bot peers."""
from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

CLIENT_COMMANDS = frozenset({"poll", "tgmessages", "tgfiles", "tginfo", "tgadmin", "tgprofile"})
BOT_COMMANDS = frozenset({"managedbots", "botpeers", "b2b", "biz"})
TELEGRAM_OPERATION_COMMANDS = CLIENT_COMMANDS | BOT_COMMANDS

# Telegram polls cap the question at 255 characters (the operations layer allows
# 300, but the server rejects longer questions). The clarify footer is a divider
# line plus the locale string; when question + footer no longer fits, the footer
# rides as the poll's description (SendMedia caption) instead.
CLARIFY_POLL_MAX_QUESTION_CHARS = 255
CLARIFY_POLL_FOOTER_DIVIDER = "\n\n—\n"


def build_clarify_poll_text(question: str, choices: Any) -> tuple[str, str] | None:
    """Render a single-select clarify as ``(poll question, description)``.

    The poll question carries the ``❓`` heading plus the footer (divider +
    "Reply to this poll to send your answer.") when it fits in 255 characters;
    otherwise the footer goes in the description caption below the poll. Returns
    None when the question cannot be a poll at all — no choices (open-ended),
    a question that alone exceeds 255 characters, or options that break the
    poll limits (1–12 unique non-empty strings, each ≤ 100 characters) — so the
    caller falls back to the numbered-text card instead of truncating anything.
    """
    from agent.i18n import t

    raw = str(question or "").strip()
    # The runner appends the skip hint for messaging platforms; the poll footer
    # replaces it (skip still works: replying "skip" to the poll skips it).
    skip_hint = t("gateway.clarify.skip_hint")
    if skip_hint and raw.endswith("\n" + skip_hint):
        raw = raw[: -len("\n" + skip_hint)].rstrip()
    headed = t("gateway.clarify.question", question=raw)
    if not raw or len(headed) > CLARIFY_POLL_MAX_QUESTION_CHARS:
        return None
    labels = [str(choice).strip() for choice in (choices or [])]
    if (not labels or len(labels) > 12 or any(not label or len(label) > 100 for label in labels)
            or len(set(labels)) != len(labels)):
        return None
    footer = CLARIFY_POLL_FOOTER_DIVIDER + t("gateway.clarify.poll_footer")
    if len(headed) + len(footer) <= CLARIFY_POLL_MAX_QUESTION_CHARS:
        return headed + footer, ""
    return headed, footer


def _clarify_reply_anchor(metadata: Any, source: Any) -> int | None:
    """Message id a clarify poll replies to: the runner-stamped triggering id,
    else the DM-topic anchor, else the observed source's own message (the last
    thread message). None when there is no message to reply to."""
    for key in ("clarify_reply_to_message_id", "telegram_reply_to_message_id"):
        raw = (metadata or {}).get(key)
        if raw is not None and str(raw).strip().isdigit():
            return int(str(raw).strip())
    raw = getattr(source, "message_id", None)
    if raw is not None and str(raw).strip().isdigit():
        return int(str(raw).strip())
    return None


def _clarify_respondent_name(source: Any, respondent_id: Any) -> str | None:
    """Display name for the intended user, from the observed source when it is
    that user; None otherwise (the caller falls back to a bare mention)."""
    if str(getattr(source, "user_id", "") or "") == str(respondent_id):
        name = str(getattr(source, "user_name", "") or "").strip()
        return name or None
    return None


def _clarify_vote_index_from_options(options: list, choices: list) -> int | None:
    """Index of the choice a vote's option bytes name, or None.

    Option bytes are opaque per-poll identifiers: polls this adapter creates
    use ``bytes([i])``; older polls used ``bytes([65 + i])``. Either form
    names its index; anything else names nothing.
    """
    for i in range(len(choices)):
        if bytes([i]) in options or bytes([65 + i]) in options:
            return i
    return None


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
            result = await self._bot_operations_ui().operations.handle_managed_update(update, profile_home=home)
        except Exception as exc:
            logger.warning("Telegram managed-bot update was not recorded (%s)", type(exc).__name__)
            return
        if not result.get("ok"):
            return
        # The roster changed: re-render any open managed card for this profile so
        # the new child appears without the owner re-running the command.
        try:
            await self._bot_operations_ui().refresh_managed_cards(str(home), result.get("bot_id"))
        except Exception as exc:
            logger.warning("Managed-bot card refresh failed (%s)", type(exc).__name__)

    def _observe_telegram_message(self, source: Any, message: Any) -> None:
        from gateway.session_identity import replace_source
        from gateway.session import SessionSource
        ui = self._client_operations_ui()
        ui.observe(source, message)
        if not isinstance(source, SessionSource):
            return  # synthetic sources have no native event identity to retain
        # F4: the same admitted inbound message settles a correlated-reply
        # waiter on this chat and refreshes the conversation registry's
        # address row. Best-effort and fail-open: observation never breaks
        # on it, and the message still routes to its own session's turn.
        try:
            raw = getattr(message, "raw_message", None) or message
            msg_id = getattr(raw, "id", None)
            if msg_id is not None:
                date = getattr(raw, "date", None)
                from tools.conversation_reply_wait import notify_inbound
                notify_inbound(
                    self._settings_home(source), "telegram", source.chat_id,
                    getattr(source, "thread_id", None) or None,
                    message_id=int(msg_id),
                    sender_id=getattr(raw, "sender_id", None),
                    is_bot=bool(getattr(source, "is_bot", False)),
                    text=str(getattr(raw, "message", "") or ""),
                    date=date.isoformat() if hasattr(date, "isoformat") else date,
                    reply_to_msg_id=getattr(raw, "reply_to_msg_id", None),
                )
            from tools.conversation_registry import record_conversation
            record_conversation(
                "telegram", source.chat_id, thread_id=getattr(source, "thread_id", None) or None,
                type=str(getattr(source, "chat_type", "") or "") or None,
                name=getattr(source, "chat_name", None) or getattr(source, "user_name", None),
                last_message_id=int(msg_id) if msg_id is not None else None,
            )
        except Exception:  # health: allow BLE001 -- the F4 hook never breaks observation
            logger.debug("conversation reply hook failed", exc_info=True)
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

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        """Single-select clarify as a native Telegram poll.

        Multi-select stays the numbered-text card (one vote expires the poll, which
        cannot collect several selections), and anything that cannot be a poll —
        an oversize question, an unusable option, no observed source to send as —
        falls back to the base text card. The poll is non-anonymous: Telegram only
        delivers ``updateMessagePollVote`` for polls the bot itself sent that are
        non-anonymous, so an anonymous poll could never resolve. Its ``open_period``
        matches the clarify timeout, so a poll the process never retires still
        closes on the server at the same moment the wait expires.
        """
        from tools import clarify_gateway as clarify_mod

        entry = clarify_mod.get_pending(clarify_id)
        if entry is not None and entry.multi_select:
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        built = build_clarify_poll_text(question, choices)
        if built is None:
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        thread_id = str((metadata or {}).get("thread_id") or "")
        source = self._clarify_poll_source(chat_id, thread_id)
        if source is None:
            logger.debug("Clarify poll has no observed source for chat %s; using the text card", chat_id)
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        poll_question, description = built
        params: dict[str, Any] = {
            "question": poll_question, "options": list(choices), "description": description,
            "anonymous": False, "multiple_choice": False, "revoting": False, "quiz": False,
        }
        # The poll replies to the turn's triggering message (the last thread
        # message): the runner stamps it in the metadata, else the observed
        # source's own message id. With no message to reply to, the poll tags
        # the intended user in its caption instead, so they still get pinged.
        reply_to = _clarify_reply_anchor(metadata, source)
        if reply_to is not None:
            params["reply_to"] = reply_to
        elif entry is not None and entry.respondent_id is not None:
            name = (str((metadata or {}).get("respondent_name") or "")
                    or _clarify_respondent_name(source, entry.respondent_id)
                    or "you")
            try:
                params["mention"] = {"user_id": int(str(entry.respondent_id)), "name": name}
            except (TypeError, ValueError):
                params["mention"] = None
            if params["mention"] is not None:
                from agent.i18n import t
                from tools import clarify_gateway as _cm

                description = t("gateway.clarify.poll_tag", name=name,
                                minutes=max(1, round(_cm.get_clarify_timeout() / 60)))
                params["description"] = description
        timeout = clarify_mod.get_clarify_timeout()
        if 5 <= timeout <= 2628000:
            params["open_period"] = timeout
        result = await self._client_operations_ui().request(source, "poll_create", params)
        if not result.get("ok"):
            error = result.get("error") or {}
            logger.warning("Clarify poll could not be created (%s); using the text card",
                           error.get("message") or result.get("status"))
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        data = result.get("data") or {}
        message_id = data.get("message_id")
        poll_id = data.get("poll_id")
        if not message_id or not poll_id:
            logger.warning("Clarify poll creation returned no message/poll id; using the text card")
            return await super().send_clarify(
                chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id,
                session_key=session_key, metadata=metadata)
        clarify_mod.register_clarify_poll(
            clarify_id, int(poll_id), int(message_id), chat_id, thread_id)
        logger.info("Clarify poll registered for clarify %s: poll %s (message %s)",
                    clarify_id, poll_id, message_id)
        # Typed replies stay open: a reply to the poll is the "your own answer" path.
        clarify_mod.mark_awaiting_text(clarify_id)
        from gateway.platforms.base import SendResult

        return SendResult(success=True, message_id=str(message_id))

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """Retire a clarify poll (timeout, answered by vote or text, cancelled):
        stop it, then delete its message, so no dead poll lingers answerable.
        The notice is not posted anywhere."""
        from tools import clarify_gateway as clarify_mod

        poll = clarify_mod.get_clarify_poll(clarify_id)
        if poll is None:
            logger.info("Clarify retire skipped for %s: no poll tracked", clarify_id)
            return
        await self._retire_clarify_poll(poll)

    def _clarify_poll_source(self, chat_id: Any, thread_id: str) -> Any | None:
        """The observed source for the turn's chat/thread — the identity the poll
        send runs as. The turn's triggering message is observed before the turn
        runs, so this is normally present; None means the text card instead."""
        for source in (getattr(self, "_telegram_observed_sources", None) or {}).values():
            if (str(getattr(source, "chat_id", "")) == str(chat_id)
                    and str(getattr(source, "thread_id", "") or "") == str(thread_id or "")):
                return source
        return None

    async def _retire_clarify_poll(self, poll: Any) -> None:
        """Stop a clarify poll and delete its message; the registry entry is
        dropped either way, so a failed stop or delete cannot wedge the next vote.
        Every outcome logs: a poll left standing after an answer is otherwise
        invisible in the host log (delete_message swallows its own failures
        and returns False, so the return value is checked, not just the raise)."""
        from tools import clarify_gateway as clarify_mod

        stopped = False
        deleted = False
        try:
            source = self._clarify_poll_source(poll.chat_id, poll.thread_id)
            if source is None:
                logger.warning("Clarify poll stop skipped for clarify %s (message %s): "
                               "no observed source for chat %s",
                               poll.clarify_id, poll.message_id, poll.chat_id)
            else:
                await self._client_operations_ui().request(
                    source, "poll_stop", {"message_id": int(poll.message_id)})
                stopped = True
        except Exception:
            logger.warning("Clarify poll stop failed for clarify %s (message %s)",
                           poll.clarify_id, poll.message_id, exc_info=True)
        try:
            delete = getattr(self, "delete_message", None)
            if callable(delete):
                deleted = bool(await delete(str(poll.chat_id), str(poll.message_id)))
                if not deleted:
                    logger.warning("Clarify poll delete failed for clarify %s (message %s): "
                                   "delete_message returned False",
                                   poll.clarify_id, poll.message_id)
        except Exception:
            logger.warning("Clarify poll delete failed for clarify %s (message %s)",
                           poll.clarify_id, poll.message_id, exc_info=True)
        finally:
            clarify_mod.drop_clarify_poll(poll.clarify_id)
        logger.info("Clarify poll retired for clarify %s (message %s): stop=%s delete=%s",
                    poll.clarify_id, poll.message_id,
                    "ok" if stopped else "failed", "ok" if deleted else "failed")

    async def _on_poll_vote(self, update: Any) -> None:
        """A vote on a clarify poll: the first vote from the configured respondent
        resolves the clarify and stops the poll. Votes from anyone else are
        ignored — they do not count and do not expire the poll. A retracted vote
        (no options) carries no answer and is ignored too.

        Every branch logs: a vote that never arrives leaves no entry line, a
        vote that arrives but is dropped names the branch that dropped it, so
        the host log can tell the two apart. A vote whose positions carry no
        usable index still resolves through its option bytes.
        """
        from tools import clarify_gateway as clarify_mod

        try:
            poll_id = int(getattr(update, "poll_id", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("Clarify poll vote with an unusable poll id %r ignored",
                           getattr(update, "poll_id", None))
            return
        if not poll_id:
            logger.warning("Clarify poll vote with no poll id ignored")
            return
        logger.info("Clarify poll vote received for poll %s", poll_id)
        found = clarify_mod.get_clarify_poll_for_poll_id(poll_id)
        if found is None:
            # Not a tracked clarify poll — a restart drops the in-memory poll
            # index, so a vote after one lands here.
            logger.warning("Clarify poll vote for unknown poll %s ignored "
                           "(not a tracked clarify poll)", poll_id)
            return
        poll, entry = found
        if entry is None:
            # Resolved or expired before the vote arrived: stop the stale poll.
            logger.info("Clarify poll vote for poll %s arrived after resolve/expire; "
                        "retiring the stale poll", poll_id)
            await self._retire_clarify_poll(poll)
            return
        try:
            from telethon import utils as tg_utils

            voter_id = str(tg_utils.get_peer_id(getattr(update, "peer", None)))
        except Exception:
            logger.warning("Clarify poll vote for poll %s ignored: voter peer %r "
                           "could not be resolved", poll_id,
                           getattr(update, "peer", None), exc_info=True)
            return
        if entry.respondent_id is not None and voter_id != str(entry.respondent_id):
            logger.info("Clarify poll vote for poll %s from %s ignored (respondent is %s); "
                        "poll stays open", poll_id, voter_id, entry.respondent_id)
            return
        try:
            positions = [int(p) for p in (getattr(update, "positions", None) or [])]
        except (TypeError, ValueError):
            positions = []
        options = list(getattr(update, "options", None) or [])
        index = None
        if positions:
            candidate = positions[0]
            if 0 <= candidate < len(entry.choices or []):
                index = candidate
        if index is None:
            index = _clarify_vote_index_from_options(options, entry.choices or [])
        if index is None or not entry.choices:
            logger.warning("Clarify poll vote for poll %s from %s ignored: no usable choice "
                           "(positions=%r, options=%r, %d choices)",
                           poll_id, voter_id, positions, options,
                           len(entry.choices or []))
            return
        clarify_mod.resolve_gateway_clarify(entry.clarify_id, entry.choices[index])
        logger.info("Clarify poll vote for poll %s from %s resolved clarify %s with choice %d",
                    poll_id, voter_id, entry.clarify_id, index)
        await self._retire_clarify_poll(poll)

    async def _handle_telegram_operation_command(self, message: Any, source: Any,
                                                 command: str, arguments: str,
                                                 *, reply_to=None) -> bool:
        if command not in TELEGRAM_OPERATION_COMMANDS:
            return False
        # Client controls are admitted-user; bot controls are owner-only,
        # gated in the bot-operations UI layer. Bots, rejected routes and
        # unadmitted senders are denied silently, like every other denial.
        if (getattr(source, "is_bot", False) or getattr(source, "profile_route_rejected", False)
                or not self._authorized(source)):
            return True
        if command == "biz":
            # Owner-only: business_ui refuses non-owners, mirroring the
            # managedbots path. Lazy import keeps the UI out of the
            # routing module's import graph.
            from .business_ui import handle_command
            await handle_command(self, message, source, command, arguments,
                                 reply_to=reply_to)
            return True
        ui = self._client_operations_ui() if command in CLIENT_COMMANDS else self._bot_operations_ui()
        await ui.handle_command(message, source, command, arguments, reply_to=reply_to)
        return True

    async def request_telegram_operation(self, source: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Agent tool entry point; client operations are admitted-user and bot operations are owner-only (gated in the bot-operations UI), except peer.send which is guard-free for admitted users. Only peer.send executes immediately on call; peer.add, peer.remove, peer.unban, peer.set_settings, peer.set_permissions and managed.access.set stage an owner confirmation card first."""
        if (getattr(source, "is_bot", False) or getattr(source, "profile_route_rejected", False)
                or not self._authorized(source)):
            return {"error": "Telegram operations are not available for this sender."}
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
                    "confirmation": "peer.send executes immediately on call; peer.add, peer.remove, peer.unban, peer.set_settings, peer.set_permissions and managed.access.set stage an owner confirmation card first."}
        from .client_operations import ALL_ACTIONS
        if action in ALL_ACTIONS:  # bot_profile* are client self-configuration, not bot-peer aliases
            return await self._client_operations_ui().request(source, action, params)
        if action.startswith(("bot_", "managed_", "peer_", "managed.", "peers.", "peer.", "inbox.")):
            return await self._bot_operations_ui().request(source, action, params)
        return await self._client_operations_ui().request(source, action, params)
