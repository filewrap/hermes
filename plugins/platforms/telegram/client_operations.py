"""Scoped Telegram MTProto operations used by the Telegram UI and agent tool.

This module deliberately does not register gateway commands or tools. Integrators call
``TelegramClientOperations.request(source, action, params)`` with the inbound source so every
operation stays bound to the current chat, actor, thread and profile.
"""
from __future__ import annotations

import asyncio
import datetime
import errno
import hashlib
import logging
import os
import re
import shutil
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .download_connections import DownloadConnectionPool
from .gateway_logs import MAX_LOG_LINES, read_gateway_logs
from .parallel_download import PARALLEL_PARTS, IncompletePartError, download_document, supports_parallel

logger = logging.getLogger(__name__)

READ_ACTIONS = {
    "poll_results", "messages_history", "messages_search", "message_get",
    "files_metadata", "files_download", "chat_info", "chat_topics", "user_info", "bot_profile", "capabilities",
    "request",
}
MUTATION_ACTIONS = {
    "poll_create", "poll_stop", "chat_title", "chat_about", "chat_permissions",
    "user_ban", "user_unban", "messages_delete", "bot_profile_update",
    "pin_message", "unpin_message", "big_reaction",
    "ephemeral_send", "ephemeral_delete", "cross_chat_reply", "send_message",
}
# Owner-only widget actions: direct sends/edits/reads against the current
# chat, executed immediately like rich_message_send — no confirmation card.
# They are owner-gated through OWNER_READ_ACTIONS (see _authorize) and are
# deliberately NOT part of the `request` allowlist: widget methods are not
# read-only, and the blocked prefixes already bar messages.send/edit/set.
WIDGET_ACTIONS = {
    "widget_send", "widget_send_by_id", "widget_upload", "widget_answer",
    "widget_edit_message", "widget_edit_shared", "widget_edit_inline",
    "widget_get_public_data",
}
DIRECT_ACTIONS = {"rich_message_send", "quote_reply"} | WIDGET_ACTIONS
OWNER_READ_ACTIONS = {"gateway_logs"} | WIDGET_ACTIONS
ALL_ACTIONS = READ_ACTIONS | OWNER_READ_ACTIONS | MUTATION_ACTIONS | DIRECT_ACTIONS
MAX_PAGE = 50
DOWNLOAD_IDLE_TIMEOUT = 120.0
DOWNLOAD_DISK_RESERVE_BYTES = 16 * 1024 * 1024
MAX_OBSERVED_MESSAGES = 500
_RECIPIENT_USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")


class DownloadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.download_code = code

CLIENT_ACTION_PARAMETERS = {
    "poll_create": {"question": "required string, 1–300 characters", "options": "required list of 1–12 unique strings, each 1–100 characters",
        "description": "optional post text above the poll, 0–1024 characters (the SendMedia caption)",
        "anonymous": "optional boolean, default true", "multiple_choice": "optional boolean, default false",
        "quiz": "optional boolean, default false", "correct_answers": "required for quiz: list of 0-based correct answer indexes (several allowed)",
        "solution": "optional quiz explanation, at most 200 characters and 2 line breaks",
        "open_period": "optional integer seconds until auto-close, 5–2628000; cannot combine with close_date",
        "close_date": "optional unix time when the poll auto-closes, 5–2628000 seconds ahead; cannot combine with open_period",
        "country_codes": "optional list of 0–12 ISO country codes; channels only",
        "allow_adding_options": "optional boolean, default false; not for anonymous polls or quizzes",
        "revoting": "optional boolean; default true for regular polls, false for quizzes",
        "shuffle_options": "optional boolean, default false",
        "hide_results_until_close": "optional boolean, default false",
        "members_only": "optional boolean, default false; channels only"},
    "poll_results": {"message_id": "required positive message ID in current chat/topic; observed vote counts only"},
    "poll_stop": {"message_id": "required positive poll message ID in current chat/topic"},
    "messages_history": {"limit": "optional integer 1–50, default 20", "offset_id": "optional exclusive older-than message ID, default 0"},
    "messages_search": {"query": "required string, 1–256 characters; bounded observed messages only",
        "limit": "optional integer 1–50, default 20", "offset_id": "optional exclusive older-than message ID, default 0"},
    "message_get": {"message_id": "required positive message ID; returned chat/topic is verified before exposing content"},
    "messages_delete": {"message_ids": "required list of 1–100 unique message IDs already observed in this exact chat/topic"},
    "files_metadata": {"message_id": "required positive media message ID in current chat/topic"},
    "files_download": {"message_id": "required positive media message ID; explicit MTProto stream to private profile cache; Telegram availability and free disk space apply"},
    "chat_info": {}, "bot_profile": {},
    "chat_topics": {"limit": "optional integer 1–100, default 50", "offset_topic": "optional topic cursor, default 0",
        "topic_ids": "optional list of 1–100 unique topic IDs; when given, returns exactly those topics instead of a page; cannot be combined with limit or offset_topic"},
    "pin_message": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "silent": "optional boolean, default true; pin without notifying the chat"},
    "unpin_message": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "silent": "optional boolean, default true",
        "pm_oneside": "optional boolean, default false; one-sided unpin, only for unpinning in a DM; rejected elsewhere"},
    "big_reaction": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "emoji": "required single emoji, e.g. 👍; a word or typo is rejected"},
    "ephemeral_send": {"content": "required message text; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "note": "DM only; the peer is the current chat, no arbitrary target; delivery is best-effort and Telegram may not confirm it"},
    "ephemeral_delete": {"message_id": "required positive ephemeral message ID already observed in this exact chat/topic; DM only"},
    "cross_chat_reply": {"message_id": "required positive message ID already observed in any chat/topic this profile has seen; the target peer comes from that observed message, never from params",
        "content": "required Markdown or native Rich HTML reply body; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "buttons": "optional list of up to 8 native one-way buttons, same shape as rich_message_send"},
    "send_message": {"recipient": "required numeric Telegram chat/user ID or @username; validated and resolved server-side to a peer, never a chat_id, peer, or destination field",
        "content": "required Markdown or native Rich HTML body; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto"},
    "user_info": {"user_id": "optional positive 64-bit user ID; defaults to current actor; other users must be observed senders in this exact scope"},
    "gateway_logs": {"log": "optional gateway or errors; reads only the active profile's gateway.log or errors.log", "limit": f"optional integer 1–{MAX_LOG_LINES}; default 50"},
    "chat_title": {"title": "required string, 1–128 characters; replaces current chat title"},
    "chat_about": {"about": "required string, at most 255 characters; replaces current chat description"},
    "chat_permissions": {"denied": "required object of booleans: send_messages, send_media, send_polls, invite_users, pin_messages, change_info, manage_topics. Replaces all default restrictions; omitted rights become unrestricted"},
    "user_ban": {"user_id": "required positive 64-bit user ID; current supergroup/channel only"},
    "user_unban": {"user_id": "required positive 64-bit user ID; current supergroup/channel only"},
    "bot_profile_update": {"name": "optional bot name, at most 64 characters", "about": "optional bot about, at most 120 characters",
        "description": "optional bot description, at most 512 characters; at least one field required"},
    "rich_message_send": {"content": "required Markdown or native Rich HTML body; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "buttons": "optional list of up to 8 native one-way buttons: {type: url|copy|profile, label, value, style}; copy value is at most 256 characters; profile value defaults to the current actor and other users must be observed senders in this exact chat/topic; style is optional primary, success, or danger; HTML bodies render buttons as in-body <tg-button> pills, Markdown/blocks bodies as PageButton rows"},
    "quote_reply": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "quote": "required excerpt of that message's text, copied verbatim, at most 4,096 characters",
        "content": "required Markdown or native Rich HTML reply body; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "buttons": "optional list of up to 8 native one-way buttons, same shape as rich_message_send"},
    "widget_send": {"html": "required widget HTML string; at most 32,768 bytes raw and 4,096 bytes zlib level 9",
        "caption": "optional caption text",
        "width": "optional fixed width 200–600 px, default 0 (auto)",
        "height": "optional fixed height 50–600 px, default 0 (auto)",
        "allow_queries": "optional boolean; the widget may ask the bot on tap",
        "shared_widget": "optional boolean; one shared object for all copies, forwards included",
        "public_data": "optional shared state string, at most 1,024 bytes UTF-8, no NUL",
        "chat_data": "optional per-message state string, at most 1,024 bytes UTF-8, no NUL",
        "reply_to": "optional message ID in the current chat/topic to reply to",
        "reply_markup": "optional classic inline keyboard rows: lists of [label, callback payload, style] buttons, style primary, success, danger, or null"},
    "widget_send_by_id": {"widget_id": "required positive widget ID of an existing shared widget",
        "chat_data": "optional per-message state string, at most 1,024 bytes UTF-8, no NUL",
        "caption": "optional caption text"},
    "widget_upload": {"html": "required widget HTML string; at most 32,768 bytes raw and 4,096 bytes zlib level 9",
        "width": "optional fixed width 200–600 px, default 0 (auto)",
        "height": "optional fixed height 50–600 px, default 0 (auto)",
        "allow_queries": "optional boolean; the widget may ask the bot on tap",
        "shared_widget": "optional boolean; one shared object for all copies, forwards included",
        "public_data": "optional shared state string, at most 1,024 bytes UTF-8, no NUL",
        "chat_data": "optional per-message state string, at most 1,024 bytes UTF-8, no NUL"},
    "widget_answer": {"query_id": "required widget tap query ID",
        "result": "optional answer string, at most 1,024 bytes UTF-8; omitted means the empty answer"},
    "widget_edit_message": {"message_id": "required positive message ID of the original widget message in the current chat",
        "public_data": "optional shared state string, at most 1,024 bytes UTF-8; at least one of public_data or chat_data is required",
        "chat_data": "optional per-message state string, at most 1,024 bytes UTF-8; at least one of public_data or chat_data is required"},
    "widget_edit_shared": {"widget_id": "required positive widget ID",
        "public_data": "required shared state string, at most 1,024 bytes UTF-8",
        "cache_time": "optional poll cadence in seconds; 0 resets it to 30 seconds"},
    "widget_edit_inline": {"inline_message_id": "required object {dc_id, id, access_hash} identifying a via-bot widget message",
        "public_data": "optional shared state string, at most 1,024 bytes UTF-8; at least one of public_data or chat_data is required",
        "chat_data": "optional per-message state string, at most 1,024 bytes UTF-8; at least one of public_data or chat_data is required"},
    "widget_get_public_data": {"message_id": "required positive message ID of a widget message in the current chat",
        "hash": "optional hash, default 0; a current hash returns not-modified instead of the data"},
    "request": {"method": "required Telethon TL function name, e.g. channels.GetFullChannel; only the allowlisted read-only methods may be called",
        "args": "optional object of JSON scalar fields only; the peer is always the current chat, never supplied in args"},
}


class _BlockedRequestError(ValueError):
    """A structured request for a TL method that is explicitly barred.

    Carries the error code the tool returns, so a blocked method is refused
    with REQUEST_BLOCKED instead of a generic invalid-argument error.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# Structured-request allowlist: read-only TL functions whose peer is forced
# to the current chat. The value is the set of args the method accepts; an
# empty set means the call takes no arguments at all. Extend only by explicit
# addition here — never by resolving a caller-supplied dotted path.
_REQUEST_ARG_FIELDS = {
    "channels.GetFullChannel": set(),
    "messages.GetFullChat": set(),
    "users.GetFullUser": set(),
    "messages.GetForumTopics": {"limit", "offset_topic"},
    "messages.GetForumTopicsByID": {"topics"},
}

# TL namespaces/verbs a structured request may never invoke, matched against
# the lowercased canonical method name: login/logout and 2FA, account
# changes, anything that sends/edits/deletes/forwards, exports/invites,
# admin grants, payments, bot management, and user-only enumeration the
# bot must not call (history, search, poll results).
_REQUEST_BLOCKED_PREFIXES = (
    "auth.", "account.", "payments.", "bots.", "phone.", "langpack.",
    "contacts.", "stickers.", "premium.", "stories.", "updates.", "upload.",
    "photos.", "messages.delete", "messages.send", "messages.edit",
    "messages.forward", "messages.pin", "messages.report", "messages.import",
    "messages.set", "messages.discard", "messages.gethistory",
    "messages.search", "messages.getpollresults", "messages.getmessagesviews",
    "channels.delete", "channels.edit", "channels.create", "channels.join",
    "channels.leave", "channels.invite", "channels.export", "channels.toggle",
    "channels.update", "channels.set", "channels.getparticipants",
    "channels.getchannels", "users.set", "users.getusers",
    "contacts.resolveusername",
)

# Keys that would smuggle routing or credentials into a structured request.
# The per-method arg allowlist already rejects them; this set names the
# refusal so the error says why instead of only listing allowed fields.
_REQUEST_ROUTING_KEYS = {
    "peer", "chat_id", "chat", "channel", "user_id", "user", "recipient",
    "destination", "profile", "api_id", "api_hash", "bot_token", "token",
    "password", "phone", "session", "path",
}
_REQUEST_METHOD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\.([A-Z][A-Za-z0-9]*?)(Request)?$")


class TelegramClientOperations:
    """Execute a bounded set of Telegram operations against the source's current chat."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._observed: OrderedDict[tuple[str, str, str], OrderedDict[int, Any]] = OrderedDict()
        self._download_slots = asyncio.Semaphore(PARALLEL_PARTS)
        self._download_connections = DownloadConnectionPool(PARALLEL_PARTS)

    def observe(self, source: Any, message: Any) -> None:
        """Remember a message already delivered to this admitted current-chat session.

        The parent gateway may call this after admission for inbound events and for
        adapter-owned outgoing messages. It never causes an MTProto history request.
        """
        if source is None or message is None:
            return
        message = getattr(message, "raw_message", None) or message
        msg_id = getattr(message, "id", None)
        if not msg_id:
            msg_id = getattr(message, "message_id", None)
            if msg_id:
                message = type("ObservedMessage", (), {
                    "id": int(msg_id), "message": str(getattr(message, "text", "") or ""),
                    "date": getattr(message, "timestamp", None), "sender_id": getattr(message, "user_id", None),
                    "media": None,
                })()
        if not msg_id:
            return
        try:
            key = (self._profile(source), str(source.chat_id), str(getattr(source, "thread_id", "") or ""))
        except Exception:
            return
        bucket = self._observed.setdefault(key, OrderedDict())
        bucket[int(msg_id)] = message
        bucket.move_to_end(int(msg_id))
        while len(bucket) > MAX_OBSERVED_MESSAGES:
            bucket.popitem(last=False)
        self._observed.move_to_end(key)
        while len(self._observed) > 256:
            self._observed.popitem(last=False)

    def observe_poll_update(self, source: Any, update: Any) -> bool:
        """Apply a raw UpdateMessagePoll only to an already observed message in this scope."""
        try:
            poll_id = int(update.poll_id)
        except (TypeError, ValueError, AttributeError):
            return False
        key = (self._profile(source), str(source.chat_id), str(getattr(source, "thread_id", "") or ""))
        message_id = getattr(update, "msg_id", None)
        bucket = self._observed.get(key, {})
        messages = [bucket.get(message_id)] if message_id is not None else list(bucket.values())
        changed = False
        from telethon import utils
        update_peer = getattr(update, "peer", None)
        if update_peer is not None and str(utils.get_peer_id(update_peer)) != str(source.chat_id):
            return False
        for message in messages:
            poll = getattr(getattr(message, "media", None), "poll", None) if message else None
            if poll is None or int(getattr(poll, "id", -1)) != poll_id:
                continue
            message.media.results = update.results
            if getattr(update, "poll", None) is not None:
                message.media.poll = update.poll
            changed = True
        return changed

    def _observed_messages(self, source: Any) -> list[Any]:
        key = (self._profile(source), str(source.chat_id), str(getattr(source, "thread_id", "") or ""))
        return list(self._observed.get(key, {}).values())

    def _all_messages_observed(self, source: Any, message_ids: list[int]) -> bool:
        observed_ids = {int(getattr(message, "id", 0) or 0) for message in self._observed_messages(source)}
        return bool(message_ids) and all(int(message_id) in observed_ids for message_id in message_ids)

    def _observed_message(self, source: Any, message_id: int) -> Any:
        return next((m for m in self._observed_messages(source)
                     if int(getattr(m, "id", 0) or 0) == int(message_id)), None)

    def _cross_chat_target(self, source: Any, message_id: int) -> tuple[tuple[str, str, str], Any]:
        """Find one message observed anywhere under this profile, by ID alone.

        The bucket key carries the chat/topic the message was seen in, so the
        target peer comes from observation, never from caller-supplied params.
        A message ID seen in more than one scope is ambiguous and refused:
        IDs are per-chat, and guessing the scope would reply in the wrong chat.
        """
        home = self._profile(source)
        hits = [(key, bucket[int(message_id)]) for key, bucket in self._observed.items()
                if key[0] == home and int(message_id) in bucket]
        if not hits:
            raise ValueError("The replied-to message has not been observed in any chat/topic this profile has seen; "
                             "read it there first — no chat ID may be supplied in params.")
        if len(hits) > 1:
            raise ValueError("That message ID was observed in more than one chat/topic; it cannot identify one reply target.")
        return hits[0]

    def _mutation_scope_error(self, source: Any, action: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Fail closed before confirmation/execution when a mutation's target is unobserved."""
        if action in {"pin_message", "unpin_message", "big_reaction", "ephemeral_delete"}:
            if not self._all_messages_observed(source, [params["message_id"]]):
                return self._error("MESSAGE_SCOPE_INVALID", "The message must have been observed in this exact chat/topic first.")
        if action == "cross_chat_reply":
            try:
                self._cross_chat_target(source, params["message_id"])
            except ValueError as exc:
                return self._error("MESSAGE_SCOPE_INVALID", str(exc))
        return None

    def _client(self):
        client = getattr(self.adapter, "_client", None)
        if client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        return client

    @staticmethod
    def _source_value(source: Any, name: str, default: Any = None) -> Any:
        return getattr(source, name, default)

    def _authorize(self, source: Any, *, owner: bool) -> tuple[bool, str]:
        if source is None or not str(self._source_value(source, "chat_id", "") or ""):
            return False, "A current Telegram chat is required."
        if self._source_value(source, "is_bot", False) or self._source_value(source, "is_guest", False):
            return False, "Telegram client operations are unavailable to bots and guest sessions."
        authorized = getattr(self.adapter, "_authorized", None)
        try:
            if not callable(authorized) or authorized(source) is not True:
                return False, "This Telegram account is not admitted in the current chat."
        except Exception:
            return False, "Telegram authorization could not be verified."
        if owner:
            check_owner = getattr(self.adapter, "_is_owner_user", None)
            try:
                if not callable(check_owner) or check_owner(self._source_value(source, "user_id"), source) is not True:
                    return False, "Only the configured Telegram owner can perform this operation."
            except Exception:
                return False, "Telegram owner authorization could not be verified."
        return True, ""

    def _profile(self, source: Any) -> str:
        home_fn = getattr(self.adapter, "_settings_home", None)
        if not callable(home_fn):
            raise RuntimeError("Telegram profile resolver is unavailable")
        home = Path(home_fn(source)).expanduser().resolve()
        return str(home)

    async def request(self, source: Any, action: str, params: dict[str, Any] | None = None,
                      *, progress_callback: Any = None, cancel_event: asyncio.Event | None = None) -> dict[str, Any]:
        """Execute a read/direct send or return a pending mutation for UI confirmation."""
        action = str(action or "").strip().lower()
        params = dict(params or {})
        if action not in ALL_ACTIONS:
            return self._error("UNSUPPORTED_ACTION", f"Unsupported Telegram operation: {action or '(empty)'}")
        allowed, reason = self._authorize(source, owner=action in MUTATION_ACTIONS | OWNER_READ_ACTIONS)
        if not allowed:
            return self._error("FORBIDDEN", reason)
        if action == "capabilities":
            owner_allowed, _ = self._authorize(source, owner=True)
            reads = READ_ACTIONS - {"capabilities"}
            parameters = {name: value for name, value in CLIENT_ACTION_PARAMETERS.items()
                          if name not in OWNER_READ_ACTIONS or owner_allowed}
            if owner_allowed:
                reads = reads | OWNER_READ_ACTIONS
            return {"ok": True, "status": "complete", "action": action, "data": {
                "reads": sorted(reads), "mutations": sorted(MUTATION_ACTIONS),
                "direct_actions": sorted(DIRECT_ACTIONS),
                "parameters": parameters,
                **({"gateway_log_access": "Configured owner only; current profile's gateway.log or errors.log, read-only, newest 1–100 lines, bounded to 64 KiB read and 16,000 output characters, with forced secret/token redaction; no arbitrary paths."}
                   if owner_allowed else {}),
                "message_access": "History/search use bounded observed messages. Explicit message-ID/file lookup may fetch accessible messages after exact chat/topic verification; user-only history/search RPCs are not used.",
                "poll_results": "Counts come from observed message media or UpdateMessagePoll; the bot-unsupported messages.getPollResults method is not called. Voter identities are not exposed.",
                "media_download_limit_bytes": None,
                "media_download_transport": f"Telethon/MTProto parallel document parts (up to {PARALLEL_PARTS}); native media fallback; streamed to disk, explicit only",
                "mutations_require_owner_confirmation": True,
            }}
        try:
            profile = self._profile(source)
        except Exception:
            return self._error("PROFILE_UNAVAILABLE", "The Telegram profile could not be resolved safely.")
        if action in DIRECT_ACTIONS:
            try:
                clean_params = self._validate_payload(action, params)
                if action == "quote_reply":
                    data = await self._send_quote_reply(source, clean_params, profile)
                elif action in WIDGET_ACTIONS:
                    data = await self._execute_widget(source, action, clean_params, profile)
                else:
                    data = await self._send_rich_message(source, clean_params, profile)
                return {"ok": True, "status": "complete", "action": action, "data": data}
            except (TypeError, ValueError) as exc:
                return self._error("INVALID_ARGUMENT", str(exc))
            except Exception as exc:
                return self._error(self._error_code(exc), self._safe_error(exc))
        if action in MUTATION_ACTIONS:
            try:
                clean_params = self._validate_payload(action, params)
            except (TypeError, ValueError) as exc:
                return self._error("INVALID_ARGUMENT", str(exc))
            if action == "messages_delete" and not self._all_messages_observed(source, clean_params["message_ids"]):
                return self._error("MESSAGE_SCOPE_INVALID", "Every message ID must have been observed in this exact chat/thread before deletion.")
            scope_error = self._mutation_scope_error(source, action, clean_params)
            if scope_error is not None:
                return scope_error
            return {"ok": True, "status": "confirmation_required", "action": action,
                    "params": clean_params, "chat_id": str(source.chat_id),
                    "thread_id": str(source.thread_id) if getattr(source, "thread_id", None) else None,
                    "profile": profile, "actor_id": str(source.user_id or "")}
        if action == "request":
            try:
                data = await self._execute_request(source, params)
            except _BlockedRequestError as exc:
                return self._error(exc.code, str(exc))
            except (TypeError, ValueError) as exc:
                return self._error("INVALID_ARGUMENT", str(exc))
            except Exception as exc:
                return self._error(self._error_code(exc), self._safe_error(exc))
            return {"ok": True, "status": "complete", "action": action, "data": data}
        try:
            data = await self._execute_read(source, action, params, profile,
                                            progress_callback=progress_callback, cancel_event=cancel_event)
            return {"ok": True, "status": "complete", "action": action, "data": data}
        except Exception as exc:
            return self._error(self._error_code(exc), self._safe_error(exc))

    async def execute_confirmed(self, source: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """Execute one already-confirmed mutation after rechecking owner/admission."""
        if action not in MUTATION_ACTIONS:
            return self._error("UNSUPPORTED_ACTION", "This operation is not a confirmable mutation.")
        allowed, reason = self._authorize(source, owner=True)
        if not allowed:
            return self._error("FORBIDDEN", reason)
        try:
            clean_params = self._validate_payload(action, params)
            if action == "messages_delete" and not self._all_messages_observed(source, clean_params["message_ids"]):
                return self._error("MESSAGE_SCOPE_INVALID", "Every message ID must have been observed in this exact chat/thread before deletion.")
            scope_error = self._mutation_scope_error(source, action, clean_params)
            if scope_error is not None:
                return scope_error
            result = await self._execute_mutation(source, action, clean_params)
            return {"ok": True, "status": "complete", "action": action, "data": result}
        except Exception as exc:
            return self._error(self._error_code(exc), self._safe_error(exc))

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "status": "error", "error": {"code": code, "message": message}}

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, IncompletePartError):
            return "DOWNLOAD_INCOMPLETE"
        if isinstance(exc, DownloadError):
            return exc.download_code
        if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EFBIG}:
            return "DOWNLOAD_STORAGE_ERROR"
        if isinstance(exc, TimeoutError):
            return "DOWNLOAD_TIMEOUT"
        name = type(exc).__name__.upper()
        if name in {"BOTMETHODINVALIDERROR", "BOT_METHOD_INVALID"}:
            return "BOT_METHOD_INVALID"
        rpc_code = getattr(exc, "code", None)
        if rpc_code:
            return str(getattr(exc, "message", "TELEGRAM_RPC_ERROR") or "TELEGRAM_RPC_ERROR")
        return name or "TELEGRAM_ERROR"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(str(exc)).replace("\x00", "")[:500]
        if "BotMethodInvalid" in type(exc).__name__:
            return "Telegram returned BOT_METHOD_INVALID: this bot account cannot perform this operation."
        return text or type(exc).__name__

    @staticmethod
    def _int(value: Any, field: str, *, minimum: int = 1, maximum: int = 2**31 - 1) -> int:
        if isinstance(value, (bool, float)):
            raise ValueError(f"{field} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"{field} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _validate_recipient(value: Any) -> str:
        """Normalize a send target: numeric Telegram ID or @username, nothing else.

        A numeric ID may be a user, group, supergroup, or channel ID. A username
        must be a valid Telegram username shape (5-32 characters, starts with a
        letter); anything else — URLs, deep links, paths, display names — is
        refused here, and resolution against Telegram happens at send time.
        """
        if isinstance(value, bool):
            raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
        if isinstance(value, int):
            if value == 0 or abs(value) > 2**63 - 1:
                raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            digits = text.removeprefix("-")
            if digits.isdecimal() and len(digits) <= 19:
                number = int(text)
                if number == 0 or abs(number) > 2**63 - 1:
                    raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")
                return str(number)
            if _RECIPIENT_USERNAME_RE.match(text):
                return "@" + text.lstrip("@")
        raise ValueError("recipient must be a numeric Telegram chat/user ID or a valid @username, e.g. @username or -1001234567890")

    @staticmethod
    def _validate_emoji(value: Any) -> str:
        """Accept one real emoji (a single character or sequence); reject words and typos."""
        if not isinstance(value, str):
            raise ValueError("emoji must be a single emoji, e.g. 👍")
        emoji = value.strip()
        if (not emoji or len(emoji) > 8 or any(char.isspace() or char.isalnum() for char in emoji)
                or all(ord(char) < 128 for char in emoji)):
            raise ValueError("emoji must be a single emoji, e.g. 👍; a word or typo is not a reaction")
        return emoji

    @classmethod
    def _validate_rich_body(cls, params: dict[str, Any]) -> tuple[str, str]:
        from plugins.platforms.telegram.mtproto_rich import MAX_RICH_MESSAGE_CHARS

        content = params.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_RICH_MESSAGE_CHARS:
            raise ValueError(f"content must be a non-empty string of at most {MAX_RICH_MESSAGE_CHARS} characters")
        rich_format = str(params.get("format", "auto") or "auto").strip().lower()
        if rich_format not in {"auto", "markdown", "html", "blocks"}:
            raise ValueError("format must be auto, markdown, html, or blocks")
        return content, rich_format

    @classmethod
    def _validate_rich_buttons(cls, raw_buttons: Any) -> list[dict[str, Any]]:
        from urllib.parse import urlsplit

        if not isinstance(raw_buttons, list) or len(raw_buttons) > 8:
            raise ValueError("buttons must be a list of at most 8 native one-way actions")
        buttons = []
        for item in raw_buttons:
            if not isinstance(item, dict) or set(item) - {"type", "label", "value", "style"}:
                raise ValueError("each button requires only type, label, optional value, and optional style")
            kind = str(item.get("type", "")).strip().lower()
            if kind not in {"url", "copy", "profile"}:
                raise ValueError("button type must be url, copy, or profile; callback buttons are not supported here")
            label = str(item.get("label") or {"url": "Open link", "copy": "Copy", "profile": "View profile"}[kind]).strip()
            if not label or len(label) > 64:
                raise ValueError("button label must contain 1–64 characters")
            style = item.get("style")
            if style is not None:
                style = str(style).strip().lower()
                if style not in {"primary", "success", "danger"}:
                    raise ValueError("button style must be primary, success, or danger; the link style is callback-only")
            value = item.get("value")
            if kind == "url":
                value = str(value or "").strip()
                parsed = urlsplit(value)
                if (not value or len(value) > 2048 or parsed.scheme.lower() not in {"http", "https"}
                        or not parsed.netloc or parsed.username or parsed.password
                        or any(ord(char) < 32 for char in value)):
                    raise ValueError("URL buttons require a valid http:// or https:// URL")
            elif kind == "copy":
                value = str(value or "")
                # Telegram rejects longer copy text with
                # BUTTON_COPY_TEXT_INVALID; CopyTextButton is 1-256
                # characters. Fail here with a clear message instead of
                # sending a request the server will refuse.
                if not value or len(value) > 256 or "\x00" in value:
                    raise ValueError("copy buttons require non-empty text of at most 256 characters")
            elif value not in (None, ""):
                value = str(cls._int(value, "profile user_id", maximum=2**63 - 1))
            else:
                value = None
            buttons.append({"type": kind, "label": label, "value": value, "style": style})
        return buttons

    @classmethod
    def _normalize_request_method(cls, method: str) -> str:
        """Map a caller-spelled TL name to its canonical allowlist spelling.

        Exactly one dot is allowed, so there is no dotted-path traversal into
        arbitrary modules, and the class part must start uppercase, as
        Telethon TL function classes do. A trailing ``Request`` is optional
        spelling. This is a structured descriptor, not code evaluation: the
        name only selects an entry of _REQUEST_ARG_FIELDS.
        """
        text = method.strip()
        match = _REQUEST_METHOD_RE.match(text)
        if not match:
            raise ValueError("method must be a Telethon TL function name, e.g. channels.GetFullChannel")
        normalized = f"{match.group(1).lower()}.{match.group(2)}"
        if any(normalized.lower().startswith(prefix) for prefix in _REQUEST_BLOCKED_PREFIXES):
            raise _BlockedRequestError(
                "REQUEST_BLOCKED", f"{normalized} is not available through the structured request action")
        if normalized not in _REQUEST_ARG_FIELDS:
            raise ValueError(f"{normalized} is not on the allowlisted read-only request set; "
                             "use action=capabilities to discover the supported operations")
        return normalized

    @classmethod
    def _validate_widget_options(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the options shared by widget_send and widget_upload."""
        from plugins.platforms.telegram.widget_messages import (
            validate_widget_dimensions, validate_widget_state,
        )

        width = cls._int(p.get("width", 0), "width", minimum=0, maximum=600)
        height = cls._int(p.get("height", 0), "height", minimum=0, maximum=600)
        width, height = validate_widget_dimensions(width, height)
        options: dict[str, Any] = {"width": width, "height": height}
        for key in ("allow_queries", "shared_widget"):
            if key in p:
                if type(p[key]) is not bool:
                    raise ValueError(f"{key} must be a boolean")
                options[key] = p[key]
        for key in ("public_data", "chat_data"):
            options[key] = validate_widget_state(p.get(key), field=key)
        return options

    @classmethod
    def _validate_widget_keyboard(cls, raw: Any) -> list[list[tuple[str, str, str | None]]]:
        """Validate classic inline-keyboard rows: [label, callback payload, style].

        Telethon enforces the payload (at most 64 bytes) and style values at
        build time; the label length is checked here, since Telethon accepts
        any label and the server does not.
        """
        if not isinstance(raw, list) or len(raw) > 8:
            raise ValueError("reply_markup must be a list of at most 8 rows of [label, callback payload, style] buttons")
        rows: list[list[tuple[str, str, str | None]]] = []
        for row in raw:
            if not isinstance(row, list) or not row or len(row) > 8:
                raise ValueError("each reply_markup row must be a list of 1–8 [label, callback payload, style] buttons")
            buttons: list[tuple[str, str, str | None]] = []
            for item in row:
                if not isinstance(item, (list, tuple)) or len(item) != 3:
                    raise ValueError("each reply_markup button must be [label, callback payload, style]")
                label, payload, style = item
                if not isinstance(label, str) or not label.strip() or len(label) > 64:
                    raise ValueError("reply_markup button label must contain 1–64 characters")
                if not isinstance(payload, str) or not payload or len(payload.encode("utf-8")) > 64:
                    raise ValueError("reply_markup callback payload must be a non-empty string of at most 64 bytes")
                if style is not None:
                    style = str(style).strip().lower()
                    if style not in {"primary", "success", "danger"}:
                        raise ValueError("reply_markup button style must be primary, success, danger, or null")
                buttons.append((label.strip(), payload, style))
            rows.append(buttons)
        return rows

    @classmethod
    def _validate_request_payload(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate a structured request descriptor: method + JSON scalar args.

        Args carry only JSON scalars (or lists of them) for the method's own
        fields; any peer/chat/user id, credential, or path key is refused,
        because the peer is forced to the current chat at execution time.
        """
        if set(p) - {"method", "args"}:
            raise ValueError("request accepts only method and args; the peer is always the current chat")
        method = p.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a Telethon TL function name, e.g. channels.GetFullChannel")
        normalized = cls._normalize_request_method(method)
        args = p.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("args must be an object of JSON scalar fields")
        smuggled = {key for key in args if str(key).strip().lower() in _REQUEST_ROUTING_KEYS}
        if smuggled:
            raise ValueError("request args must not carry routing or credentials; the peer is forced to the current chat")
        allowed = _REQUEST_ARG_FIELDS[normalized]
        if set(args) - allowed:
            raise ValueError(f"{normalized} accepts only {', '.join(sorted(allowed)) or 'no arguments'}")
        cleaned: dict[str, Any] = {}
        if "limit" in args:
            cleaned["limit"] = cls._int(args["limit"], "limit", minimum=1, maximum=100)
        if "offset_topic" in args:
            cleaned["offset_topic"] = cls._int(args["offset_topic"], "offset_topic", minimum=0)
        if "topics" in args:
            topics = args["topics"]
            if not isinstance(topics, list) or not 1 <= len(topics) <= 100:
                raise ValueError("topics must contain 1–100 topic IDs")
            cleaned_topics = [cls._int(x, "topic_id") for x in topics]
            if len(set(cleaned_topics)) != len(cleaned_topics):
                raise ValueError("topics must be unique")
            cleaned["topics"] = cleaned_topics
        return {"method": normalized, "args": cleaned}

    @classmethod
    def _validate_widget_state_edit(cls, p: dict[str, Any]) -> tuple[str | None, str | None]:
        """Validate the public_data/chat_data pair of a widget state edit."""
        from plugins.platforms.telegram.widget_messages import validate_widget_state

        public_data = validate_widget_state(p.get("public_data"), field="public_data")
        chat_data = validate_widget_state(p.get("chat_data"), field="chat_data")
        if public_data is None and chat_data is None:
            raise ValueError("A widget state edit needs public_data or chat_data")
        return public_data, chat_data

    @classmethod
    def _validate_widget_send(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_send action's params; routing stays on the current chat."""
        if set(p) - {"html", "caption", "width", "height", "allow_queries", "shared_widget",
                     "public_data", "chat_data", "reply_to", "reply_markup"}:
            raise ValueError("widget_send accepts only html, caption, width, height, allow_queries, "
                             "shared_widget, public_data, chat_data, reply_to, and reply_markup; "
                             "routing is fixed to the current chat")
        from plugins.platforms.telegram.widget_messages import validate_widget_html

        caption = p.get("caption", "")
        if not isinstance(caption, str):
            raise ValueError("caption must be a string")
        result = {"html": validate_widget_html(p.get("html")), "caption": caption,
                  **cls._validate_widget_options(p)}
        if "reply_to" in p:
            result["reply_to"] = cls._int(p["reply_to"], "reply_to")
        if p.get("reply_markup") is not None:
            result["reply_markup"] = cls._validate_widget_keyboard(p["reply_markup"])
        return result
    @classmethod
    def _validate_widget_send_by_id(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_send_by_id action's params; routing stays on the current chat."""
        if set(p) - {"widget_id", "chat_data", "caption"}:
            raise ValueError("widget_send_by_id accepts only widget_id, chat_data, and caption; "
                             "routing is fixed to the current chat")
        from plugins.platforms.telegram.widget_messages import validate_widget_state

        caption = p.get("caption", "")
        if not isinstance(caption, str):
            raise ValueError("caption must be a string")
        return {"widget_id": cls._int(p.get("widget_id"), "widget_id", maximum=2**63 - 1),
                "chat_data": validate_widget_state(p.get("chat_data"), field="chat_data"),
                "caption": caption}
    @classmethod
    def _validate_widget_upload(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_upload action's params; routing stays on the current chat."""
        if set(p) - {"html", "width", "height", "allow_queries", "shared_widget",
                     "public_data", "chat_data"}:
            raise ValueError("widget_upload accepts only html, width, height, allow_queries, "
                             "shared_widget, public_data, and chat_data; "
                             "routing is fixed to the current chat")
        from plugins.platforms.telegram.widget_messages import validate_widget_html

        return {"html": validate_widget_html(p.get("html")), **cls._validate_widget_options(p)}
    @classmethod
    def _validate_widget_answer(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_answer action's params; routing stays on the current chat."""
        if set(p) - {"query_id", "result"}:
            raise ValueError("widget_answer accepts only query_id and result")
        from plugins.platforms.telegram.widget_messages import validate_widget_result

        return {"query_id": cls._int(p.get("query_id"), "query_id", maximum=2**63 - 1),
                "result": validate_widget_result(p.get("result"))}
    @classmethod
    def _validate_widget_edit_message(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_edit_message action's params; routing stays on the current chat."""
        if set(p) - {"message_id", "public_data", "chat_data"}:
            raise ValueError("widget_edit_message accepts only message_id, public_data, and chat_data; "
                             "routing is fixed to the current chat")
        public_data, chat_data = cls._validate_widget_state_edit(p)
        return {"message_id": cls._int(p.get("message_id"), "message_id"),
                "public_data": public_data, "chat_data": chat_data}
    @classmethod
    def _validate_widget_edit_shared(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_edit_shared action's params; routing stays on the current chat."""
        if set(p) - {"widget_id", "public_data", "cache_time"}:
            raise ValueError("widget_edit_shared accepts only widget_id, public_data, and cache_time")
        if p.get("public_data") is None:
            raise ValueError("widget_edit_shared requires public_data")
        from plugins.platforms.telegram.widget_messages import validate_widget_state

        result = {"widget_id": cls._int(p.get("widget_id"), "widget_id", maximum=2**63 - 1),
                  "public_data": validate_widget_state(p.get("public_data"), field="public_data")}
        if p.get("cache_time") is not None:
            result["cache_time"] = cls._int(p["cache_time"], "cache_time", minimum=0)
        return result
    @classmethod
    def _validate_widget_edit_inline(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_edit_inline action's params; routing stays on the current chat."""
        if set(p) - {"inline_message_id", "public_data", "chat_data"}:
            raise ValueError("widget_edit_inline accepts only inline_message_id, public_data, and chat_data")
        inline_id = p.get("inline_message_id")
        if not isinstance(inline_id, dict) or set(inline_id) - {"dc_id", "id", "access_hash"}:
            raise ValueError("inline_message_id must be an object with dc_id, id, and access_hash")
        cleaned_id = {
            "dc_id": cls._int(inline_id.get("dc_id"), "inline_message_id.dc_id"),
            "id": cls._int(inline_id.get("id"), "inline_message_id.id",
                           minimum=-(2**63), maximum=2**63 - 1),
            "access_hash": cls._int(inline_id.get("access_hash"), "inline_message_id.access_hash",
                                    minimum=-(2**63), maximum=2**63 - 1),
        }
        public_data, chat_data = cls._validate_widget_state_edit(p)
        return {"inline_message_id": cleaned_id,
                "public_data": public_data, "chat_data": chat_data}
    @classmethod
    def _validate_widget_get_public_data(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_get_public_data action's params; routing stays on the current chat."""
        if set(p) - {"message_id", "hash"}:
            raise ValueError("widget_get_public_data accepts only message_id and hash; "
                             "routing is fixed to the current chat")
        return {"message_id": cls._int(p.get("message_id"), "message_id"),
                "hash": cls._int(p.get("hash", 0), "hash", minimum=0, maximum=2**63 - 1)}
    @classmethod
    def _validate_widget_payload(cls, action: str, p: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one widget action to its validator (dict, not a ladder)."""
        validators = {
            "widget_send": cls._validate_widget_send,
            "widget_send_by_id": cls._validate_widget_send_by_id,
            "widget_upload": cls._validate_widget_upload,
            "widget_answer": cls._validate_widget_answer,
            "widget_edit_message": cls._validate_widget_edit_message,
            "widget_edit_shared": cls._validate_widget_edit_shared,
            "widget_edit_inline": cls._validate_widget_edit_inline,
            "widget_get_public_data": cls._validate_widget_get_public_data,
        }
        validator = validators.get(action)
        if validator is None:
            raise ValueError(f"Unsupported widget operation: {action}")
        return validator(p)

    @classmethod
    def _validate_quote_excerpt(cls, p: dict[str, Any]) -> str:
        """Validate the verbatim quote excerpt of a quote_reply."""
        quote = p.get("quote")
        if not isinstance(quote, str) or not quote.strip() or len(quote) > 4096:
            raise ValueError("quote must be a non-empty excerpt of at most 4,096 characters, copied verbatim from the quoted message")
        return quote.strip()
    @classmethod
    def _validate_poll_create(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate poll_create params (tg-polls: 1–12 options, several correct answers)."""
        if "correct_option" in p:
            raise ValueError("correct_option was replaced by correct_answers (a list of indexes)")
        question = str(p.get("question", "")).strip()
        options = p.get("options")
        if not question or len(question) > 300 or not isinstance(options, list) or not 1 <= len(options) <= 12:
            raise ValueError("A poll requires a question and 1–12 answer options")
        options = [str(x).strip() for x in options]
        if any(not x or len(x) > 100 for x in options) or len(set(options)) != len(options):
            raise ValueError("Poll options must be unique non-empty strings of at most 100 characters")
        description = str(p.get("description", "") or "")
        if len(description) > 1024:
            raise ValueError("Poll description must be at most 1024 characters")
        booleans = {key: p.get(key, default) for key, default in (
            ("anonymous", True), ("multiple_choice", False), ("quiz", False),
            ("allow_adding_options", False), ("shuffle_options", False),
            ("hide_results_until_close", False), ("members_only", False))}
        if any(type(value) is not bool for value in booleans.values()):
            raise ValueError("anonymous, multiple_choice, quiz, allow_adding_options, "
                             "shuffle_options, hide_results_until_close and members_only must be booleans")
        if "revoting" in p and type(p["revoting"]) is not bool:
            raise ValueError("revoting must be a boolean")
        result = {"question": question, "options": options, "description": description, **booleans}
        result["revoting"] = p.get("revoting", not booleans["quiz"])
        if booleans["quiz"]:
            correct = p.get("correct_answers")
            if (not isinstance(correct, list) or not correct
                    or any(type(x) is not int or not 0 <= x < len(options) for x in correct)
                    or len(set(correct)) != len(correct)):
                raise ValueError("A quiz requires correct_answers: unique 0-based indexes into options")
            result["correct_answers"] = sorted(correct)
            solution = str(p.get("solution", ""))
            if len(solution) > 200 or solution.count("\n") > 2:
                raise ValueError("Quiz solution must be at most 200 characters and two line breaks")
            result["solution"] = solution
        elif any(key in p for key in ("correct_answers", "solution")):
            raise ValueError("correct_answers and solution are available only for a quiz")
        if booleans["allow_adding_options"] and (booleans["anonymous"] or booleans["quiz"]):
            raise ValueError("allow_adding_options is not available for anonymous polls or quizzes")
        open_period = p.get("open_period")
        close_date = p.get("close_date")
        if open_period is not None and close_date is not None:
            raise ValueError("open_period and close_date cannot be combined")
        if open_period is not None:
            if type(open_period) is not int or not 5 <= open_period <= 2628000:
                raise ValueError("open_period must be 5–2628000 seconds")
            result["open_period"] = open_period
        if close_date is not None:
            now = int(time.time())
            if type(close_date) is not int or not now + 5 <= close_date <= now + 2628000:
                raise ValueError("close_date must be 5–2628000 seconds in the future")
            result["close_date"] = close_date
        country_codes = p.get("country_codes")
        if country_codes is not None:
            if (not isinstance(country_codes, list) or len(country_codes) > 12
                    or any(not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code)
                           for code in country_codes)):
                raise ValueError("country_codes must be 0–12 ISO country codes")
            result["country_codes"] = list(country_codes)
        return result

    @classmethod
    def _validate_payload(cls, action: str, params: dict[str, Any]) -> dict[str, Any]:
        p = dict(params)
        if action == "request":
            return cls._validate_request_payload(p)
        if action == "rich_message_send":
            if set(p) - {"content", "format", "buttons"}:
                raise ValueError("rich_message_send accepts only content, format, and buttons; routing is fixed to the current chat/topic")
            content, rich_format = cls._validate_rich_body(p)
            return {"content": content, "format": rich_format, "buttons": cls._validate_rich_buttons(p.get("buttons", []))}
        if action == "quote_reply":
            if set(p) - {"message_id", "quote", "content", "format", "buttons"}:
                raise ValueError("quote_reply accepts only message_id, quote, content, format, and buttons; routing is fixed to the current chat/topic")
            quote = cls._validate_quote_excerpt(p)
            content, rich_format = cls._validate_rich_body(p)
            return {"message_id": cls._int(p.get("message_id"), "message_id"), "quote": quote,
                    "content": content, "format": rich_format, "buttons": cls._validate_rich_buttons(p.get("buttons", []))}
        if action in WIDGET_ACTIONS:
            return cls._validate_widget_payload(action, p)
        if action == "gateway_logs":
            if set(p) - {"log", "limit"}:
                raise ValueError("gateway_logs accepts only log and limit; file paths are not supported")
            log = str(p.get("log", "gateway") or "gateway").strip().lower()
            if log not in {"gateway", "errors"}:
                raise ValueError("log must be 'gateway' or 'errors'")
            return {"log": log, "limit": cls._int(
                p.get("limit", 50), "limit", minimum=1, maximum=MAX_LOG_LINES,
            )}
        if action == "poll_create":
            return cls._validate_poll_create(p)
        if action in {"chat_info", "bot_profile"}:
            return {}
        if action == "user_info":
            return {"user_id": str(cls._int(p["user_id"], "user_id", maximum=2**63 - 1))} if "user_id" in p else {}
        if action in {"poll_results", "poll_stop", "message_get", "files_metadata", "files_download"}:
            return {"message_id": cls._int(p.get("message_id"), "message_id")}
        if action == "messages_delete":
            ids = p.get("message_ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                raise ValueError("message_ids must contain 1–100 message IDs")
            cleaned = [cls._int(x, "message_id") for x in ids]
            if len(set(cleaned)) != len(cleaned):
                raise ValueError("message_ids must be unique")
            return {"message_ids": cleaned}
        if action == "messages_history":
            return {"limit": cls._int(p.get("limit", 20), "limit", minimum=1, maximum=MAX_PAGE),
                    "offset_id": cls._int(p.get("offset_id", 0), "offset_id", minimum=0)}
        if action == "messages_search":
            query = str(p.get("query", "")).strip()
            if not query or len(query) > 256:
                raise ValueError("query must contain 1–256 characters")
            return {"query": query, "limit": cls._int(p.get("limit", 20), "limit", minimum=1, maximum=MAX_PAGE),
                    "offset_id": cls._int(p.get("offset_id", 0), "offset_id", minimum=0)}
        if action == "chat_topics":
            if "topic_ids" in p:
                if "limit" in p or "offset_topic" in p:
                    raise ValueError("topic_ids cannot be combined with limit or offset_topic")
                ids = p["topic_ids"]
                if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                    raise ValueError("topic_ids must contain 1–100 topic IDs")
                cleaned = [cls._int(x, "topic_id") for x in ids]
                if len(set(cleaned)) != len(cleaned):
                    raise ValueError("topic_ids must be unique")
                return {"topic_ids": cleaned}
            return {"limit": cls._int(p.get("limit", 50), "limit", minimum=1, maximum=100),
                    "offset_topic": cls._int(p.get("offset_topic", 0), "offset_topic", minimum=0)}
        if action in {"pin_message", "unpin_message"}:
            allowed = {"message_id", "silent"} | ({"pm_oneside"} if action == "unpin_message" else set())
            if set(p) - allowed:
                raise ValueError(f"{action} accepts only message_id, silent"
                                 + (", and pm_oneside" if action == "unpin_message" else "")
                                 + "; routing is fixed to the current chat/topic")
            result = {"message_id": cls._int(p.get("message_id"), "message_id")}
            for key, default in (("silent", True), ("pm_oneside", False)):
                if key in allowed:
                    value = p.get(key, default)
                    if type(value) is not bool:
                        raise ValueError(f"{key} must be a boolean")
                    result[key] = value
            return result
        if action == "big_reaction":
            if set(p) - {"message_id", "emoji"}:
                raise ValueError("big_reaction accepts only message_id and emoji; routing is fixed to the current chat/topic")
            return {"message_id": cls._int(p.get("message_id"), "message_id"),
                    "emoji": cls._validate_emoji(p.get("emoji"))}
        if action == "ephemeral_send":
            if set(p) - {"content", "format"}:
                raise ValueError("ephemeral_send accepts only content and format; routing is fixed to the current chat")
            content, rich_format = cls._validate_rich_body(p)
            return {"content": content, "format": rich_format}
        if action == "ephemeral_delete":
            if set(p) - {"message_id"}:
                raise ValueError("ephemeral_delete accepts only message_id; routing is fixed to the current chat")
            return {"message_id": cls._int(p.get("message_id"), "message_id")}
        if action == "cross_chat_reply":
            if set(p) - {"message_id", "content", "format", "buttons"}:
                raise ValueError("cross_chat_reply accepts only message_id, content, format, and buttons; "
                                 "the target peer comes from the observed message, never from params")
            content, rich_format = cls._validate_rich_body(p)
            return {"message_id": cls._int(p.get("message_id"), "message_id"),
                    "content": content, "format": rich_format,
                    "buttons": cls._validate_rich_buttons(p.get("buttons", []))}
        if action == "send_message":
            if set(p) - {"recipient", "content", "format"}:
                raise ValueError("send_message accepts only recipient, content, and format; "
                                 "the target is a numeric ID or @username, never chat_id, peer, or destination")
            content, rich_format = cls._validate_rich_body(p)
            return {"recipient": cls._validate_recipient(p.get("recipient")),
                    "content": content, "format": rich_format}
        if action == "chat_title":
            value = str(p.get("title", "")).strip()
            if not value or len(value) > 128:
                raise ValueError("title must contain 1–128 characters")
            return {"title": value}
        if action == "chat_about":
            if "about" not in p:
                raise ValueError("about is required; use an explicit empty string to clear it")
            value = str(p.get("about", ""))
            if len(value) > 255:
                raise ValueError("about must be at most 255 characters")
            return {"about": value}
        if action == "user_ban":
            return {"user_id": str(cls._int(p.get("user_id"), "user_id", maximum=2**63 - 1))}
        if action == "user_unban":
            return {"user_id": str(cls._int(p.get("user_id"), "user_id", maximum=2**63 - 1))}
        if action == "chat_permissions":
            if "denied" not in p:
                raise ValueError("denied is required; an explicit empty object clears all default restrictions")
            rights = p["denied"]
            supported = {"send_messages", "send_media", "send_polls", "invite_users", "pin_messages", "change_info", "manage_topics"}
            if not isinstance(rights, dict) or set(rights) - supported:
                raise ValueError("denied must contain supported permission names mapped to booleans")
            if any(type(value) is not bool for value in rights.values()):
                raise ValueError("denied permission values must be booleans")
            return {"denied": dict(rights)}
        if action == "bot_profile_update":
            fields = {k: str(p[k]) for k in ("name", "about", "description") if k in p}
            limits = {"name": 64, "about": 120, "description": 512}
            if not fields or any(len(v) > limits[k] for k, v in fields.items()):
                raise ValueError("Supply bot name (64), about (120), or description (512 characters max)")
            return fields
        raise ValueError(f"No payload schema for {action}")

    def _check_profile_target(self, source: Any, target_id: int) -> None:
        """Profile buttons may target only the current actor or an observed sender."""
        observed_ids = {str(getattr(message, "sender_id", "") or "")
                        for message in self._observed_messages(source)}
        if target_id != int(source.user_id) and str(target_id) not in observed_ids:
            raise ValueError("profile buttons may target only the current actor or a sender observed in this exact chat/topic")

    def _html_button(self, source: Any, button: dict[str, Any]) -> str:
        """Render one one-way button as an in-body <tg-button> pill.

        HTML profile buttons need no entity resolution: the target is a
        ``tg://user?id=`` URL, but the observed-sender boundary still applies.
        """
        from plugins.platforms.telegram.mtproto_rich import rich_button

        kind, label, value, style = button["type"], button["label"], button["value"], button.get("style")
        if kind == "profile":
            target_id = int(value) if value is not None else int(source.user_id)
            self._check_profile_target(source, target_id)
            return rich_button(label, kind="profile", value=str(target_id), style=style)
        return rich_button(label, kind=kind, value=value, style=style)

    async def _send_quote_reply(self, source: Any, params: dict[str, Any], profile: str) -> dict[str, Any]:
        """Reply to one observed message, quoting a verbatim excerpt of its text.

        The target must already be observed in this exact chat/thread, and the
        quote must match its text exactly: Telegram renders the excerpt from
        the original message, so a paraphrase or invented excerpt would quote
        text the message never contained.
        """
        from .topic_delivery import topic_reply_header

        target_id = params["message_id"]
        observed = [message for message in self._observed_messages(source)
                    if int(getattr(message, "id", 0) or 0) == target_id]
        if not observed:
            raise ValueError(
                "The quoted message must have been observed in this exact chat/thread; "
                "read it with messages_history or message_get first.")
        target_text = str(getattr(observed[0], "message", "") or "")
        quote = params["quote"]
        offset = target_text.find(quote)
        if offset < 0:
            raise ValueError(
                "quote must match the quoted message's text exactly; read the message "
                "first and copy the excerpt verbatim.")
        # Telegram quote offsets are UTF-16 code units, not Python indices.
        quote_offset = len(target_text[:offset].encode("utf-16-le")) // 2
        thread_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
        header = topic_reply_header(target_id, thread_id, quote_text=quote, quote_offset=quote_offset)
        data = await self._send_rich_message(source, params, profile, reply_header=header)
        data.update({"reply_to_message_id": target_id, "quote": quote})
        return data

    async def _send_rich_message(self, source: Any, params: dict[str, Any], profile: str,
                                 *, reply_header: Any = None, scope_source: Any = None) -> dict[str, Any]:
        """Send one native RichMessage with only current-scope, one-way buttons.

        HTML bodies get the buttons as in-body ``<tg-button>`` pills in one
        trailing row; Markdown/blocks bodies keep the PageButton rows.
        ``scope_source`` overrides where the message lands and which scope's
        observed senders buttons may target (cross-chat replies); the acting
        source still owns the authorization and profile re-checks.
        """
        from telethon import functions, types, utils
        from plugins.platforms.telegram.mtproto_rich import (
            _HTML_RICH_START_RE, input_rich_message, rich_button_row,
        )
        from .topic_delivery import topic_reply_header

        scope = scope_source if scope_source is not None else source
        content = params["content"]
        buttons = params["buttons"]
        rich_format = params["format"]
        is_html = rich_format == "html" or (
            rich_format == "auto" and bool(_HTML_RICH_START_RE.match(content)))
        button_count = 0
        if buttons and is_html:
            content = content.rstrip() + "\n" + rich_button_row(
                [self._html_button(scope, button) for button in buttons])
            rich_format = "html"
            button_count = len(buttons)
        elif buttons and rich_format in {"auto", "markdown"}:
            rich_format = "blocks"
        rich = input_rich_message(content, rich_format=rich_format)
        if buttons and not is_html:
            if not isinstance(rich, types.InputRichMessage):
                raise ValueError("Native PageButtons require an InputRichMessage block body")
            native_buttons = []
            for button in buttons:
                kind, label, value = button["type"], button["label"], button["value"]
                if kind == "url":
                    button_type = types.InlineButtonTypeUrl(url=value)
                elif kind == "copy":
                    button_type = types.InlineButtonTypeCopy(copy_text=value)
                else:
                    target_id = int(value) if value is not None else int(scope.user_id)
                    self._check_profile_target(scope, target_id)
                    entity = await self._client().get_input_entity(types.PeerUser(target_id))
                    input_user = utils.get_input_user(entity)
                    if not isinstance(input_user, types.InputUser) or int(input_user.user_id) != target_id:
                        raise ValueError("Telegram could not resolve that observed user's profile")
                    button_type = types.InputInlineButtonTypeUserProfile(user_id=input_user)
                native_buttons.append(types.PageButton(
                    text=types.TextPlain(label), type=button_type,
                    style=types.RichButtonStyle(link=True),
                ))
            for index in range(0, len(native_buttons), 2):
                rich.blocks.append(types.PageBlockButtonRow(buttons=native_buttons[index:index + 2]))
            button_count = len(native_buttons)

        peer = await self._peer(scope)
        thread_id = int(scope.thread_id) if str(getattr(scope, "thread_id", "") or "").isdecimal() else None
        message_id = int(scope.message_id) if str(getattr(scope, "message_id", "") or "").isdecimal() else None
        request = functions.messages.SendMessageRequest(
            peer=peer, message=content, rich_message=rich,
            reply_to=reply_header if reply_header is not None else topic_reply_header(message_id, thread_id),
        )
        allowed, reason = self._authorize(source, owner=False)
        if not allowed or self._profile(source) != profile:
            raise ValueError(reason or "The Telegram route changed before the rich message could be sent")
        response = await self.adapter._mtproto_rich_request(request)
        sent_id = None
        receipt = getattr(self.adapter, "_sent_message_id", None)
        if callable(receipt):
            sent_id = receipt(response, random_id=request.random_id, peer=peer, topic_id=thread_id)
        if sent_id:
            self.observe(scope, type("ObservedRichMessage", (), {
                "id": int(sent_id), "message": content, "sender_id": source.user_id,
                "media": None,
            })())
        return {"message_id": sent_id, "chat_id": str(scope.chat_id),
                "thread_id": str(scope.thread_id) if thread_id else None,
                "button_count": button_count, "rich_message": True}

    @staticmethod
    def _widget_checked(result: Any) -> Any:
        """Return a widget SendResult, raising for a failed one.

        A local refusal (bad payload, broadcast channel) is an invalid
        argument; a transport failure is a runtime error.
        """
        if result.success:
            return result
        error = result.error or "Telegram widget operation failed"
        if error.startswith("Telegram widget refused"):
            raise ValueError(error)
        raise RuntimeError(error)

    @classmethod
    def _widget_send_data(cls, result: Any, chat_id: str, thread_id: Any) -> dict[str, Any]:
        checked = cls._widget_checked(result)
        data: dict[str, Any] = {"message_id": checked.message_id, "chat_id": chat_id,
                                "thread_id": str(thread_id) if thread_id else None}
        widget_id = (checked.raw_response or {}).get("widget_id") if checked.raw_response else None
        if widget_id is not None:
            data["widget_id"] = widget_id
        return data

    async def _execute_widget(self, source: Any, action: str, p: dict[str, Any], profile: str) -> dict[str, Any]:
        """Run one owner-only widget action against the current chat.

        The peer is always the current chat: no widget action takes a chat
        target. The route is re-checked right before the transport call,
        like _send_rich_message does — the adapter call is the send.
        """
        from plugins.platforms.telegram.mtproto_rich import inline_markup

        allowed, reason = self._authorize(source, owner=True)
        if not allowed or self._profile(source) != profile:
            raise ValueError(reason or "The Telegram route changed before the widget operation could run")
        chat_id = str(source.chat_id)
        thread_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
        if action == "widget_send":
            result = await self.adapter.send_widget(
                chat_id, p["html"], caption=p.get("caption", ""), width=p.get("width", 0),
                height=p.get("height", 0), allow_queries=p.get("allow_queries"),
                shared_widget=p.get("shared_widget"), public_data=p.get("public_data"),
                chat_data=p.get("chat_data"), reply_to=p.get("reply_to"),
                top_msg_id=thread_id,
                reply_markup=inline_markup(p.get("reply_markup")) if p.get("reply_markup") else None,
            )
            return self._widget_send_data(result, chat_id, thread_id)
        if action == "widget_send_by_id":
            result = await self.adapter.send_widget_by_id(
                chat_id, p["widget_id"], caption=p.get("caption", ""),
                chat_data=p.get("chat_data"), top_msg_id=thread_id,
            )
            return self._widget_send_data(result, chat_id, thread_id)
        if action == "widget_upload":
            result = await self.adapter.upload_widget(
                chat_id, p["html"], width=p.get("width", 0), height=p.get("height", 0),
                allow_queries=p.get("allow_queries"), shared_widget=p.get("shared_widget"),
                public_data=p.get("public_data"), chat_data=p.get("chat_data"),
            )
            checked = self._widget_checked(result)
            widget_id = (checked.raw_response or {}).get("widget_id") if checked.raw_response else None
            if widget_id is None:
                raise RuntimeError("Telegram did not return a widget_id for the upload")
            return {"widget_id": widget_id, "chat_id": chat_id}
        if action == "widget_answer":
            if await self.adapter.answer_widget_query(p["query_id"], p.get("result")) is not True:
                raise RuntimeError("Telegram widget query answer failed")
            return {"answered": True, "query_id": p["query_id"]}
        if action == "widget_edit_message":
            self._widget_checked(await self.adapter.edit_widget_message_data(
                chat_id, p["message_id"], public_data=p.get("public_data"),
                chat_data=p.get("chat_data")))
            return {"message_id": p["message_id"], "chat_id": chat_id}
        if action == "widget_edit_shared":
            self._widget_checked(await self.adapter.edit_shared_widget_data(
                p["widget_id"], p["public_data"], cache_time=p.get("cache_time")))
            return {"widget_id": p["widget_id"]}
        if action == "widget_edit_inline":
            from telethon import types

            inline_id = types.InputBotInlineMessageID(**p["inline_message_id"])
            self._widget_checked(await self.adapter.edit_inline_widget_data(
                inline_id, public_data=p.get("public_data"), chat_data=p.get("chat_data")))
            return {"inline_message_id": p["inline_message_id"]}
        if action == "widget_get_public_data":
            result = await self.adapter.get_widget_public_data(
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

    async def _peer(self, source: Any) -> Any:
        return await self.adapter._input_peer_for(str(source.chat_id))

    @staticmethod
    def _message_summary(message: Any) -> dict[str, Any]:
        if message is None:
            return {}
        media = getattr(message, "media", None)
        document = getattr(media, "document", None) if media else None
        photo = getattr(media, "photo", None) if media else None
        file_info = getattr(message, "file", None)
        attributes = getattr(document, "attributes", []) if document else []
        filename = next((getattr(a, "file_name", None) for a in attributes if getattr(a, "file_name", None)), None)
        poll = getattr(media, "poll", None) if media else None
        return {"id": int(getattr(message, "id", 0) or 0), "date": str(getattr(message, "date", "") or ""),
                "sender_id": getattr(message, "sender_id", None), "text": str(getattr(message, "message", "") or ""),
                "has_media": bool(media), "media_type": type(media).__name__ if media else None,
                "file_name": filename or getattr(file_info, "name", None),
                "file_size": getattr(document, "size", None) if document else getattr(file_info, "size", None),
                "mime_type": getattr(document, "mime_type", None) if document else getattr(file_info, "mime_type", None),
                "media_id": getattr(document or photo, "id", None),
                "poll": {"id": getattr(poll, "id", None), "question": str(getattr(getattr(poll, "question", ""), "text", getattr(poll, "question", "")) or "")}
                        if poll else None}

    async def _get_message(self, source: Any, message_id: int) -> Any:
        cached = next((m for m in self._observed_messages(source)
                       if int(getattr(m, "id", 0) or 0) == int(message_id)), None)
        if cached is not None:
            return cached
        # Non-channel IDs are account-wide: the requested peer alone is NOT a
        # security boundary. Bot-compatible ID fetches must verify returned scope.
        from telethon import utils
        result = await self._client().get_messages(await self._peer(source), ids=[int(message_id)])
        message = next(iter(result), None) if isinstance(result, (list, tuple)) else result
        if message is None or int(getattr(message, "id", 0) or 0) != int(message_id):
            return None
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None and getattr(message, "peer_id", None) is not None:
            chat_id = utils.get_peer_id(message.peer_id)
        if str(chat_id) != str(source.chat_id):
            raise ValueError("The message does not belong to the current Telegram chat. Reply to a file in this chat and use /tgfiles metadata or /tgfiles download; IDs from another chat cannot be used here.")
        actual_thread = self.adapter._message_thread_id(message)
        source_thread = str(getattr(source, "thread_id", "") or "")
        # A topic-less source (plain group/DM after the forum-only thread
        # derivation) has no topic to scope to; the chat check above already
        # passed. Enforcing the comparison there would refuse every file whose
        # reply header carries a chain root.
        if source_thread and str(actual_thread or "") != source_thread:
            raise ValueError("The message does not belong to the current Telegram topic. Reply to a file in this topic and use /tgfiles metadata or /tgfiles download.")
        self.observe(source, message)
        return message

    async def _download_media(self, source: Any, message: Any, summary: dict[str, Any], profile: str,
                              *, progress_callback: Any = None, cancel_event: asyncio.Event | None = None) -> dict[str, Any]:
        """Pipeline document parts, preserving private-file and scoped media handling.

        Large documents use bounded parallel MTProto requests, never a full bytes buffer.
        Other media retain Telethon's native writer and special-media handling.
        The idle watchdog, rather than a total duration limit, permits slow large files.
        """
        binding = (str(source.chat_id), str(getattr(source, "thread_id", "") or ""),
                   str(getattr(source, "user_id", "") or ""))
        scope_check_seconds = 0.0
        scope_check_count = 0

        def check_scope() -> None:
            nonlocal scope_check_seconds, scope_check_count
            checked_at = time.monotonic()
            try:
                allowed, _ = self._authorize(source, owner=False)
                current = (str(source.chat_id), str(getattr(source, "thread_id", "") or ""),
                           str(getattr(source, "user_id", "") or ""))
                if not allowed or current != binding or self._profile(source) != profile:
                    raise DownloadError("DOWNLOAD_SCOPE_CHANGED", "Download stopped: chat, actor, profile, or authorization changed.")
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadError("DOWNLOAD_CANCELLED", "Download cancelled; the partial file was removed.")
            finally:
                scope_check_seconds += time.monotonic() - checked_at
                scope_check_count += 1

        check_scope()
        raw_size = summary.get("file_size")
        size = int(raw_size) if raw_size is not None and int(raw_size) > 0 else None
        root = Path(profile) / "cache" / "telegram-client-operations"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)

        def check_space(required: int = 0) -> None:
            free = shutil.disk_usage(root).free
            if free < required + DOWNLOAD_DISK_RESERVE_BYTES:
                raise DownloadError("DOWNLOAD_STORAGE_ERROR", "Insufficient free disk space for the download (16 MiB safety reserve required).")

        check_space(size or 0)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(summary.get("file_name") or f"telegram-{summary['id']}.bin"))[:120]
        scope_digest = hashlib.sha256(":".join(binding).encode()).hexdigest()[:16]
        started_at = time.monotonic()
        last_progress = time.monotonic()
        retry_deadline = last_progress
        last_bytes = 0

        def retry_wait(delay: float) -> None:
            nonlocal retry_deadline
            check_scope()
            retry_deadline = max(retry_deadline, time.monotonic() + delay + DOWNLOAD_IDLE_TIMEOUT)

        async def progress(downloaded: int, total: int | None) -> None:
            nonlocal last_progress, last_bytes
            check_scope()
            if int(downloaded) > 0 and int(downloaded) != last_bytes:
                last_progress = time.monotonic()
                last_bytes = int(downloaded)
            check_space()
            if progress_callback is not None:
                await progress_callback(int(downloaded), total or size)

        async def refresh_document() -> Any:
            # Non-channel IDs are account-wide. Force a fresh fetch, but verify
            # chat/topic and document identity before accepting a new reference.
            from telethon import types, utils
            check_scope()
            result = await self._client().get_messages(await self._peer(source), ids=[summary["id"]])
            fresh = next(iter(result), None) if isinstance(result, (list, tuple)) else result
            chat_id = getattr(fresh, "chat_id", None)
            if chat_id is None and getattr(fresh, "peer_id", None) is not None:
                chat_id = utils.get_peer_id(fresh.peer_id)
            document = getattr(getattr(fresh, "media", None), "document", None)
            if (str(chat_id) != binding[0]
                    or (binding[1] and str(self.adapter._message_thread_id(fresh) or "") != binding[1])
                    or getattr(fresh, "id", None) != summary["id"]
                    or not isinstance(document, types.Document)
                    or document.id != summary["media_id"] or document.size != size):
                raise DownloadError("DOWNLOAD_SCOPE_CHANGED", "Media changed or is no longer accessible in this chat; reply to the file again.")
            check_scope()
            return document

        with tempfile.NamedTemporaryFile(prefix=".telegram-download-", dir=root, delete=False) as temp:
            temporary = Path(temp.name)
        # Unique per request: concurrent downloads cannot replace one another's result.
        target = root / f"{scope_digest}-{summary['id']}-{temporary.name.removeprefix('.telegram-download-')}-{safe_name}"
        task = None
        try:
            await progress(0, size)
            check_scope()
            client = self._client()
            if supports_parallel(message, size):
                transfer = download_document(client, message, str(temporary), size,
                                             progress_callback=progress, check_scope=check_scope,
                                             refresh_document=refresh_document, slots=self._download_slots,
                                             retry_callback=retry_wait,
                                             connections=self._download_connections)
            else:
                transfer = client.download_media(message, file=str(temporary), progress_callback=progress)
            task = asyncio.create_task(transfer)
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=min(1.0, DOWNLOAD_IDLE_TIMEOUT))
                check_scope()
                if (not done and time.monotonic() - last_progress >= DOWNLOAD_IDLE_TIMEOUT
                        and time.monotonic() >= retry_deadline):
                    raise DownloadError("DOWNLOAD_TIMEOUT", "Download timed out after 120 seconds without media progress; the partial file was removed.")
            transfer_stats = await task
            check_scope()
            actual = temporary.stat().st_size
            if not actual or (size is not None and actual != size):
                raise DownloadError("DOWNLOAD_INCOMPLETE", "Telegram did not return the complete media file; the partial file was removed.")
            check_space()
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            elapsed = max(time.monotonic() - started_at, 0.000001)
            from telethon.crypto import aes
            diagnostics = dict(transfer_stats) if isinstance(transfer_stats, dict) else {
                "strategy": "native_telethon", "sender_mode": "native_telethon"}
            diagnostics["crypto_backend"] = ("cryptg" if aes.cryptg else "libssl" if aes.libssl else "pyaes")
            diagnostics["scope_check_seconds_sum"] = scope_check_seconds
            diagnostics["scope_check_count"] = scope_check_count
            logger.info("Telegram media transfer complete: size_mb=%.3f elapsed_seconds=%.3f average_mb_per_second=%.3f bytes=%d diagnostics=%s",
                        actual / 1_000_000, elapsed, actual / elapsed / 1_000_000, actual, diagnostics)
            return {**summary, "cached_path": str(target), "downloaded_bytes": actual,
                    "elapsed_seconds": elapsed, "average_bytes_per_second": actual / elapsed,
                    "download_diagnostics": diagnostics,
                    "download_transport": "Telethon/MTProto"}
        finally:
            # CancelledError is a BaseException: cleanup must also run when the gateway
            # or caller cancels this coroutine, and must wait for Telethon to close its file.
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            temporary.unlink(missing_ok=True)

    async def _execute_read(self, source: Any, action: str, params: dict[str, Any], profile: str,
                            *, progress_callback: Any = None, cancel_event: asyncio.Event | None = None) -> Any:
        from telethon import functions, types
        p = self._validate_payload(action, params)
        if action == "gateway_logs":
            return read_gateway_logs(profile, **p)
        peer = await self._peer(source)
        thread_id = getattr(source, "thread_id", None)
        if action == "messages_history":
            observed = self._observed_messages(source)
            rows = [self._message_summary(m) for m in observed if int(getattr(m, "id", 0) or 0) < p["offset_id"] or not p["offset_id"]]
            rows.sort(key=lambda row: row["id"], reverse=True)
            available = len(rows)
            rows = rows[:p["limit"]]
            return {"messages": rows, "total": len(rows), "available_count": available,
                    "next_offset_id": rows[-1]["id"] if len(rows) < available else None}
        if action == "messages_search":
            query = p["query"].casefold()
            observed = self._observed_messages(source)
            rows = [self._message_summary(m) for m in observed
                    if query in str(getattr(m, "message", "") or "").casefold()
                    and (not p["offset_id"] or int(getattr(m, "id", 0) or 0) < p["offset_id"])]
            rows.sort(key=lambda row: row["id"], reverse=True)
            available = len(rows)
            rows = rows[:p["limit"]]
            return {"messages": rows, "total": len(rows), "available_count": available,
                    "next_offset_id": rows[-1]["id"] if len(rows) < available else None}
        if action == "message_get":
            msg = await self._get_message(source, p["message_id"])
            return self._message_summary(msg) if msg else None
        if action in {"files_metadata", "files_download"}:
            msg = await self._get_message(source, p["message_id"])
            if msg is None or not getattr(msg, "media", None):
                raise ValueError("That message has no accessible media in this chat/topic. DMs have message IDs too, but replying is easier: reply to the file and send /tgfiles metadata or /tgfiles download. Explicit IDs must come from this same chat/topic.")
            summary = self._message_summary(msg)
            if action == "files_metadata":
                return summary
            return await self._download_media(source, msg, summary, profile,
                                              progress_callback=progress_callback, cancel_event=cancel_event)
        if action == "chat_info":
            # Telegram bot accounts cannot use messages.getHistory; no arbitrary dialog
            # enumeration is attempted here. Full metadata requests are limited to this peer.
            if isinstance(peer, types.InputPeerChannel):
                channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
                full = await self.adapter._mtproto_rich_request(functions.channels.GetFullChannelRequest(channel=channel))
            elif isinstance(peer, types.InputPeerChat):
                full = await self.adapter._mtproto_rich_request(functions.messages.GetFullChatRequest(chat_id=peer.chat_id))
            else:
                user = await self.adapter._mtproto_rich_request(functions.users.GetFullUserRequest(id=peer))
                full = user
            chats = list(getattr(full, "chats", []) or [])
            users = list(getattr(full, "users", []) or [])
            entity = next((x for x in chats + users if str(getattr(x, "id", "")) == str(getattr(peer, "channel_id", getattr(peer, "chat_id", getattr(peer, "user_id", ""))))), None)
            full_chat = getattr(full, "full_chat", None)
            return {"id": str(source.chat_id), "title": getattr(entity, "title", None),
                    "username": getattr(entity, "username", None), "about": getattr(full_chat, "about", None),
                    "participants_count": getattr(full_chat, "participants_count", None),
                    "chat_type": str(getattr(source, "chat_type", "")), "thread_id": thread_id}
        if action == "chat_topics":
            if not isinstance(peer, types.InputPeerChannel):
                raise ValueError("Forum topics are available only for supergroups/channels")
            if p.get("topic_ids") is not None:
                result = await self.adapter._mtproto_rich_request(
                    functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=p["topic_ids"]))
            else:
                result = await self.adapter._mtproto_rich_request(functions.messages.GetForumTopicsRequest(
                    peer=peer, offset_date=None, offset_id=0, offset_topic=p["offset_topic"], limit=p["limit"]))
            items = getattr(result, "topics", []) or []
            return {"topics": [{"id": int(x.id), "title": str(getattr(x, "title", "")),
                                "closed": bool(getattr(x, "closed", False)), "pinned": bool(getattr(x, "pinned", False))}
                               for x in items], "total": len(items)}
        if action == "user_info":
            current_actor = str(getattr(source, "user_id", "") or "")
            actor = p.get("user_id", current_actor)
            if not actor.isdecimal():
                raise ValueError("The current sender identity is unavailable")
            visible_senders = {str(getattr(message, "sender_id", "") or "")
                               for message in self._observed_messages(source)}
            if actor != current_actor and actor not in visible_senders:
                raise ValueError("The target user has not been observed in this current chat/topic")
            full = await self.adapter._mtproto_rich_request(functions.users.GetFullUserRequest(
                id=await self._client().get_input_entity(int(actor))))
            user = getattr(full, "user", None) or next(iter(getattr(full, "users", []) or []), None)
            full_user = getattr(full, "full_user", None)
            return {"id": actor, "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None), "username": getattr(user, "username", None),
                    "about": getattr(full_user, "about", None),
                    "is_bot": bool(getattr(user, "bot", False))}
        if action == "bot_profile":
            result = await self.adapter._mtproto_rich_request(functions.bots.GetBotInfoRequest(lang_code=""))
            bot_info = getattr(result, "bot", result)
            return {"name": getattr(bot_info, "name", None), "about": getattr(bot_info, "about", None),
                    "description": getattr(bot_info, "description", None)}
        if action == "poll_results":
            msg = await self._get_message(source, p["message_id"])
            poll = getattr(getattr(msg, "media", None), "poll", None) if msg else None
            if poll is None:
                raise ValueError("That message is not an observed poll in this current chat/thread")
            results = getattr(getattr(msg.media, "results", None), "results", None) or []
            by_option = {bytes(getattr(r, "option", b"")): r for r in results}
            return {"message_id": p["message_id"], "question": str(getattr(getattr(poll, "question", ""), "text", getattr(poll, "question", ""))),
                    "closed": bool(getattr(poll, "closed", False)),
                    "total_voters": getattr(getattr(msg.media, "results", None), "total_voters", None),
                    "answers": [{"text": str(getattr(getattr(a, "text", None), "text", getattr(a, "text", ""))),
                                 "voters": getattr(by_option.get(bytes(getattr(a, "option", b""))), "voters", None),
                                 "chosen": bool(getattr(by_option.get(bytes(getattr(a, "option", b""))), "chosen", False))}
                                for a in getattr(poll, "answers", []) or []],
                    "results_source": "observed Telegram updates/message state"}
        raise ValueError(f"Unsupported read action: {action}")

    async def _execute_request(self, source: Any, params: dict[str, Any]) -> Any:
        """Execute one allowlisted read-only TL request against the current chat.

        The peer is built from _peer(source) for every method, never from
        params. The call goes through adapter._mtproto_rich_request — the
        same live client every other read uses — so an open control card's
        SendMessage/EditMessageRequest rewrite in _route_control_request
        still applies; these allowlisted Get* methods are never rewritten
        and pass through unchanged. No new client, session, or credential
        is created here, and a mutating TL call can never reach this path:
        it is refused by the allowlist before any transport call.
        """
        from telethon import functions, types
        p = self._validate_payload("request", params)
        peer = await self._peer(source)
        method = p["method"]
        args = p["args"]
        if method == "channels.GetFullChannel":
            if not isinstance(peer, types.InputPeerChannel):
                raise ValueError("channels.GetFullChannel is available only in a supergroup or channel")
            channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
            request = functions.channels.GetFullChannelRequest(channel=channel)
        elif method == "messages.GetFullChat":
            if not isinstance(peer, types.InputPeerChat):
                raise ValueError("messages.GetFullChat is available only in a basic group chat")
            request = functions.messages.GetFullChatRequest(chat_id=peer.chat_id)
        elif method == "users.GetFullUser":
            if not isinstance(peer, (types.InputPeerUser, types.InputPeerSelf)):
                raise ValueError("users.GetFullUser is available only in a direct message")
            request = functions.users.GetFullUserRequest(id=peer)
        elif method == "messages.GetForumTopics":
            if not isinstance(peer, types.InputPeerChannel):
                raise ValueError("Forum topics are available only for supergroups/channels")
            request = functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=None, offset_id=0,
                offset_topic=args.get("offset_topic", 0), limit=args.get("limit", 50))
        elif method == "messages.GetForumTopicsByID":
            if not isinstance(peer, types.InputPeerChannel):
                raise ValueError("Forum topics are available only for supergroups/channels")
            request = functions.messages.GetForumTopicsByIDRequest(peer=peer, topics=args["topics"])
        else:  # _validate_payload already refused anything else.
            raise ValueError(f"Unsupported request method: {method}")
        result = await self.adapter._mtproto_rich_request(request)
        return {"method": method, "result": self._bounded_request_result(result)}

    @classmethod
    def _bounded_request_result(cls, result: Any) -> Any:
        """Serialise one TL result to bounded JSON-safe data.

        TL results are raw Telethon objects, so to_dict() is the serialiser
        and the tool layer's json.dumps(..., default=str) is only the last
        resort. Lists are capped, long strings truncated, and depth limited,
        so one huge channel-full dump cannot blow up the turn.
        """
        if hasattr(result, "to_dict"):
            data = result.to_dict()
        elif isinstance(result, dict):
            data = result
        else:
            data = {"type": type(result).__name__, "value": str(result)[:4096]}
        return cls._bound_value(data, depth=0)

    @classmethod
    def _bound_value(cls, value: Any, *, depth: int) -> Any:
        if isinstance(value, dict):
            if depth >= 6:
                return {"_truncated": True}
            return {str(key)[:128]: cls._bound_value(item, depth=depth + 1)
                    for key, item in list(value.items())[:100]}
        if isinstance(value, (list, tuple)):
            items = [cls._bound_value(item, depth=depth + 1) for item in value[:50]]
            if len(value) > 50:
                items.append({"_truncated_items": len(value) - 50})
            return items
        if isinstance(value, str):
            return value if len(value) <= 4096 else value[:4096] + "…(truncated)"
        if isinstance(value, bytes):
            return value.hex()[:4096]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:4096]

    async def _execute_poll_create(self, source: Any, p: dict[str, Any],
                                   peer: Any, thread_id: Any) -> Any:
        from telethon import functions, types
        # Option bytes are the skill's form (bytes([i])); older polls used
        # bytes([65 + i]) — the bytes are opaque per-poll identifiers.
        answers = [types.PollAnswer(text=types.TextWithEntities(text=option, entities=[]), option=bytes([i]))
                   for i, option in enumerate(p["options"])]
        if (p.get("country_codes") or p.get("members_only")) and not isinstance(peer, types.InputPeerChannel):
            raise ValueError("country_codes and members_only are available only in channels")
        close_date = (datetime.datetime.fromtimestamp(p["close_date"], tz=datetime.UTC)
                      if p.get("close_date") else None)
        poll = types.Poll(id=int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1),
                          question=types.TextWithEntities(text=p["question"], entities=[]),
                          answers=answers, hash=0, public_voters=not p["anonymous"],
                          multiple_choice=p["multiple_choice"], quiz=p["quiz"],
                          open_answers=p["allow_adding_options"],
                          revoting_disabled=not p["revoting"],
                          shuffle_answers=p["shuffle_options"],
                          hide_results_until_close=p["hide_results_until_close"],
                          subscribers_only=p["members_only"],
                          close_period=p.get("open_period"),
                          close_date=close_date,
                          countries_iso2=p.get("country_codes"))
        media = types.InputMediaPoll(poll=poll,
            correct_answers=list(p["correct_answers"]) if p["quiz"] else None,
            solution=p.get("solution") or None,
            solution_entities=[] if p.get("solution") else None)
        request = functions.messages.SendMediaRequest(peer=peer, media=media, message=p.get("description") or "",
                                                      reply_to=self._reply_header(thread_id))
        result = await self.adapter._mtproto_rich_request(request)
        from .topic_delivery import sent_message_id
        message_id = sent_message_id(result, random_id=request.random_id, peer=peer,
                                     topic_id=int(thread_id) if thread_id else None)
        for update in getattr(result, "updates", []) or []:
            message = getattr(update, "message", None)
            if message is not None and str(getattr(message, "id", "")) == str(message_id):
                from telethon.utils import get_peer_id
                if (str(get_peer_id(message.peer_id)) == str(source.chat_id)
                        and str(self.adapter._message_thread_id(message) or "") == str(thread_id or "")):
                    self.observe(source, message)
        return {"message_id": int(message_id) if message_id else None,
                "question": p["question"], "options": p["options"]}

    async def _execute_poll_stop(self, source: Any, p: dict[str, Any], peer: Any) -> Any:
        from telethon import functions, types
        msg = await self._get_message(source, p["message_id"])
        old_poll = getattr(getattr(msg, "media", None), "poll", None) if msg else None
        if old_poll is None:
            raise ValueError("That message is not a poll")
        # Closing edits the poll media in place: every flag the poll was
        # created with is copied, so stopping never silently drops settings.
        closed = types.Poll(id=old_poll.id, question=old_poll.question, answers=old_poll.answers,
                            hash=getattr(old_poll, "hash", 0), closed=True,
                            public_voters=getattr(old_poll, "public_voters", None),
                            multiple_choice=getattr(old_poll, "multiple_choice", None),
                            quiz=getattr(old_poll, "quiz", None),
                            open_answers=getattr(old_poll, "open_answers", None),
                            revoting_disabled=getattr(old_poll, "revoting_disabled", None),
                            shuffle_answers=getattr(old_poll, "shuffle_answers", None),
                            hide_results_until_close=getattr(old_poll, "hide_results_until_close", None),
                            subscribers_only=getattr(old_poll, "subscribers_only", None),
                            close_period=getattr(old_poll, "close_period", None),
                            close_date=getattr(old_poll, "close_date", None),
                            countries_iso2=getattr(old_poll, "countries_iso2", None))
        request = functions.messages.EditMessageRequest(peer=peer, id=p["message_id"],
                                                        media=types.InputMediaPoll(poll=closed))
        await self.adapter._mtproto_rich_request(request)
        old_poll.closed = True
        return {"message_id": p["message_id"], "closed": True}

    async def _execute_mutation(self, source: Any, action: str, p: dict[str, Any]) -> Any:
        from telethon import functions, types
        peer = await self._peer(source)
        client = self._client()
        thread_id = getattr(source, "thread_id", None)
        if action == "poll_create":
            return await self._execute_poll_create(source, p, peer, thread_id)
        if action == "poll_stop":
            return await self._execute_poll_stop(source, p, peer)
        if action == "chat_title":
            if isinstance(peer, types.InputPeerChannel):
                channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
                await self.adapter._mtproto_rich_request(functions.channels.EditTitleRequest(channel=channel, title=p["title"]))
            elif isinstance(peer, types.InputPeerChat):
                await self.adapter._mtproto_rich_request(functions.messages.EditChatTitleRequest(chat_id=peer.chat_id, title=p["title"]))
            else:
                raise ValueError("This Telegram chat type does not support a title")
            return {"title": p["title"]}
        if action == "chat_about":
            await self.adapter._mtproto_rich_request(functions.messages.EditChatAboutRequest(peer=peer, about=p["about"]))
            return {"about": p["about"]}
        if action == "chat_permissions":
            denied = p["denied"]
            kwargs = {name: bool(value) for name, value in denied.items()}
            rights = types.ChatBannedRights(until_date=None, **kwargs)
            await self.adapter._mtproto_rich_request(functions.messages.EditChatDefaultBannedRightsRequest(peer=peer, banned_rights=rights))
            return {"denied": denied}
        if action in {"user_ban", "user_unban"}:
            if not isinstance(peer, types.InputPeerChannel):
                raise ValueError("Ban/unban is supported here only for supergroups and channels")
            target = await client.get_input_entity(int(p["user_id"]))
            if not isinstance(target, types.InputPeerUser):
                raise ValueError("Telegram target must resolve to a user")
            channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
            rights = types.ChatBannedRights(until_date=None, view_messages=action == "user_ban")
            await self.adapter._mtproto_rich_request(functions.channels.EditBannedRequest(channel=channel, participant=target, banned_rights=rights))
            return {"user_id": p["user_id"], "banned": action == "user_ban"}
        if action == "messages_delete":
            ids = p["message_ids"]
            if isinstance(peer, types.InputPeerChannel):
                channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
                await self.adapter._mtproto_rich_request(functions.channels.DeleteMessagesRequest(channel=channel, id=ids))
            else:
                await self.adapter._mtproto_rich_request(functions.messages.DeleteMessagesRequest(id=ids, revoke=True))
            return {"deleted_message_ids": ids}
        if action == "bot_profile_update":
            await self.adapter._mtproto_rich_request(functions.bots.SetBotInfoRequest(lang_code="", **p))
            return {"updated_fields": sorted(p)}
        if action in {"pin_message", "unpin_message"}:
            if self._observed_message(source, p["message_id"]) is None:
                raise ValueError("The message must have been observed in this exact chat/topic before pinning.")
            if p.get("pm_oneside") and (action != "unpin_message" or not isinstance(peer, types.InputPeerUser)):
                raise ValueError("pm_oneside applies only to a one-sided unpin in a DM")
            await self.adapter._mtproto_rich_request(functions.messages.UpdatePinnedMessageRequest(
                peer=peer, id=p["message_id"], silent=p["silent"], unpin=action == "unpin_message",
                pm_oneside=bool(p.get("pm_oneside")) if action == "unpin_message" else None))
            return {"message_id": p["message_id"], "pinned": action == "pin_message"}
        if action == "big_reaction":
            if self._observed_message(source, p["message_id"]) is None:
                raise ValueError("The message must have been observed in this exact chat/topic before reacting.")
            await self.adapter._mtproto_rich_request(functions.messages.SendReactionRequest(
                peer=peer, msg_id=p["message_id"], big=True, add_to_recent=True,
                reaction=[types.ReactionEmoji(emoticon=p["emoji"])]))
            return {"message_id": p["message_id"], "emoji": p["emoji"], "big": True}
        if action == "ephemeral_send":
            if not isinstance(peer, types.InputPeerUser):
                raise ValueError("Ephemeral messages are available only in a direct message")
            from plugins.platforms.telegram.mtproto_rich import input_rich_message
            rich = input_rich_message(p["content"], rich_format=p["format"])
            receiver = types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
            request = functions.ephemeral.SendMessageRequest(
                receiver_id=receiver, message=p["content"], peer=peer, rich_message=rich,
                reply_to=self._reply_header(thread_id))
            result = await self.adapter._mtproto_rich_request(request)
            from .topic_delivery import sent_message_id
            message_id = sent_message_id(result, random_id=getattr(request, "random_id", None),
                                         peer=peer, topic_id=None)
            return {"message_id": int(message_id) if message_id else None, "ephemeral": True,
                    "delivery": "best-effort; Telegram may not confirm an ephemeral send"}
        if action == "ephemeral_delete":
            if self._observed_message(source, p["message_id"]) is None:
                raise ValueError("The message must have been observed in this exact chat/topic before deletion.")
            if not isinstance(peer, types.InputPeerUser):
                raise ValueError("Ephemeral messages are available only in a direct message")
            receiver = types.InputUser(user_id=peer.user_id, access_hash=peer.access_hash)
            await self.adapter._mtproto_rich_request(functions.ephemeral.DeleteMessageRequest(
                receiver_id=receiver, id=p["message_id"], peer=peer))
            return {"deleted_message_id": p["message_id"], "ephemeral": True}
        if action == "cross_chat_reply":
            key, _target = self._cross_chat_target(source, p["message_id"])
            target_chat, target_thread = key[1], key[2]
            from dataclasses import replace
            scope_source = replace(source, chat_id=target_chat,
                                   thread_id=target_thread or None, message_id=None)
            target_peer = await self._peer(scope_source)
            header = types.InputReplyToMessage(
                reply_to_msg_id=p["message_id"],
                top_msg_id=int(target_thread) if str(target_thread or "").isdecimal() and target_thread != "1" else None,
                reply_to_peer_id=target_peer)
            data = await self._send_rich_message(source, p, self._profile(source),
                                                 reply_header=header, scope_source=scope_source)
            data.update({"reply_to_message_id": p["message_id"], "reply_to_chat_id": target_chat})
            return data
        if action == "send_message":
            # The recipient was shape-validated at request time; resolve it to a
            # peer here, at execution time, so an unresolvable target is refused
            # with a clear error before any send is attempted. The send itself
            # reuses the rich-message path: same content/format handling and
            # the same messages.SendMessageRequest as rich_message_send.
            from dataclasses import replace
            scope_source = replace(source, chat_id=p["recipient"], thread_id=None, message_id=None)
            try:
                await self._peer(scope_source)
            except Exception as exc:
                raise ValueError(f"Telegram could not resolve recipient {p['recipient']}; "
                                 "use a numeric chat/user ID or a valid @username the bot can reach") from exc
            data = await self._send_rich_message(
                source, {"content": p["content"], "format": p["format"], "buttons": []},
                self._profile(source), scope_source=scope_source)
            data.update({"recipient": p["recipient"]})
            return data
        raise ValueError(f"Unsupported mutation: {action}")

    @staticmethod
    def _reply_header(thread_id: Any) -> Any:
        if not thread_id:
            return None
        from plugins.platforms.telegram.topic_delivery import topic_reply_header
        return topic_reply_header(int(thread_id), int(thread_id))
