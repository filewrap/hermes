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

from .client_operations_purge import (
    capabilities_listing as _purge_capabilities_listing,
)
from .client_operations_purge import execute_purge as _execute_purge
from .client_operations_purge import prepare_purge_request_params as _prepare_purge_request_params
from .client_operations_purge import validate_purge_payload as _validate_purge_payload
from .client_operations_validation import validate_emoji, validate_recipient, validate_send_message_payload
from .client_operations_widgets import execute_direct
from .download_connections import DownloadConnectionPool
from .gateway_logs import MAX_LOG_LINES, read_gateway_logs
from .incoming_reactions import execute_agent_reaction as _execute_agent_reaction
from .parallel_download import PARALLEL_PARTS, IncompletePartError, download_document, supports_parallel

logger = logging.getLogger(__name__)

READ_ACTIONS = {
    "poll_results", "messages_history", "messages_search", "message_get",
    "files_metadata", "files_download", "chat_info", "chat_topics", "user_info", "bot_profile", "capabilities",
    "gateway_logs", "list_chats",
}
# Structured TL requests are neither reads nor mutations: one `request`
# call may read or write. They are listed separately in capabilities so
# no caller concludes from the reads list that `request` is read-only.
REQUEST_ACTIONS = {"request"}
MUTATION_ACTIONS = {
    "poll_create", "poll_stop", "chat_title", "chat_about", "chat_permissions",
    "user_ban", "user_unban", "purge", "bot_profile_update",
    "pin_message", "unpin_message", "reaction", "leave",
    "ephemeral_send", "ephemeral_delete", "cross_chat_reply", "send_message",
}
# Widget actions: direct sends/edits/reads against the current
# chat, executed immediately like rich_message_send. They are
# deliberately NOT expressible through the `request` action: widget
# calls are handled by their own send/edit paths, not by a single TL
# function call.
WIDGET_ACTIONS = {
    "widget_send", "widget_send_by_id", "widget_upload", "widget_answer",
    "widget_edit_message", "widget_edit_shared", "widget_edit_inline",
    "widget_get_public_data",
}
DIRECT_ACTIONS = {"rich_message_send", "quote_reply", "widget_show"} | WIDGET_ACTIONS
ALL_ACTIONS = READ_ACTIONS | MUTATION_ACTIONS | DIRECT_ACTIONS | REQUEST_ACTIONS
MAX_PAGE = 50
DOWNLOAD_IDLE_TIMEOUT = 120.0
DOWNLOAD_DISK_RESERVE_BYTES = 16 * 1024 * 1024
MAX_OBSERVED_MESSAGES = 500


class DownloadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.download_code = code

CLIENT_ACTION_PARAMETERS = {
    "poll_create": {"question": "required string, 1–300 characters", "options": "required list of 1–12 unique strings, each 1–100 characters",
        "description": "optional post text above the poll, 0–1024 characters (the SendMedia caption)",
        "reply_to": "optional message id the poll replies to",
        "mention": "optional {user_id, name} tagging the intended user at the start of the description",
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
    "purge": {"message_ids": "optional list of 1–100 unique message IDs already observed in this exact chat/topic; exactly one of message_ids, last_n, minutes, hours, all — owner-only, your own (bot-sent) messages only",
        "last_n": "optional integer 1–500: purge your newest N observed messages in this chat/topic",
        "minutes": "optional integer 1–43200: purge your observed messages from the last N minutes",
        "hours": "optional integer 1–720: purge your observed messages from the last N hours; cannot combine with minutes",
        "all": "optional boolean true: purge all your observed messages in this chat/topic; resolved from messages observed in this chat/topic in this process (at most 500 per chat/topic), never from a history fetch"},
    "files_metadata": {"message_id": "required positive media message ID in current chat/topic"},
    "files_download": {"message_id": "required positive media message ID; explicit MTProto stream to private profile cache; Telegram availability and free disk space apply"},
    "chat_info": {}, "bot_profile": {}, "leave": {},
    "chat_topics": {"limit": "optional integer 1–100, default 50", "offset_topic": "optional topic cursor, default 0",
        "topic_ids": "optional list of 1–100 unique topic IDs; when given, returns exactly those topics instead of a page; cannot be combined with limit or offset_topic"},
    "list_chats": {"limit": "optional integer 1–50, default 20; dialogs this bot account participates in, newest first"},
    "pin_message": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "silent": "optional boolean, default true; pin without notifying the chat"},
    "unpin_message": {"message_id": "required positive message ID already observed in this exact chat/topic",
        "silent": "optional boolean, default true",
        "pm_oneside": "optional boolean, default false; one-sided unpin, only for unpinning in a DM; rejected elsewhere"},
    "reaction": {"message_id": "optional positive message ID already observed in this exact chat/topic; omit to react to the triggering message",
        "emoji": "required single real emoji, e.g. 👍; must be one of Telegram's standard reaction emojis — a word, a typo, or an emoji Telegram does not accept as a reaction is rejected",
        "big": "optional boolean, default false; true sends a big reaction"},
    "ephemeral_send": {"content": "required message text; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "note": "DM only; the peer is the current chat, no arbitrary target; delivery is best-effort and Telegram may not confirm it"},
    "ephemeral_delete": {"message_id": "required positive ephemeral message ID already observed in this exact chat/topic; DM only"},
    "cross_chat_reply": {"message_id": "required positive message ID already observed in any chat/topic this profile has seen; the target peer comes from that observed message, never from params",
        "content": "required Markdown or native Rich HTML reply body; max 32,768 characters",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "buttons": "optional list of up to 8 native one-way buttons, same shape as rich_message_send"},
    "send_message": {"recipient": "required numeric Telegram chat/user ID or @username; validated and resolved server-side to a peer",
        "content": "required Markdown or native Rich HTML body; max 32,768 characters; optional when media or location is sent, when it becomes the media caption",
        "format": "optional auto, markdown, html, or blocks; default auto",
        "reply_to": "optional positive message ID in the recipient chat to reply to",
        "thread_id": "optional positive forum topic ID in the recipient chat",
        "silent": "optional boolean, default false; send without notifying the recipient",
        "buttons": "optional list of up to 8 native one-way buttons, same shape as rich_message_send",
        "quote": "optional verbatim excerpt of the replied-to message's text, at most 4,096 characters; needs reply_to",
        "effect": "optional message effect id",
        "pin": "optional boolean, default false; pin the sent message after sending",
        "pin_silent": "optional boolean, default true; pin without notifying the chat",
        "media": "optional http(s) URL or base64/data-URL attachment; sent with content as its caption",
        "filename": "optional file name for the attachment",
        "content_type": "optional MIME type for the attachment",
        "as_voice": "optional boolean, default false; send an audio attachment as a voice note",
        "video_note": "optional boolean, default false; send one video attachment as a round video note",
        "force_document": "optional boolean, default false; send media as a document, no compression (as_document is an alias)",
        "location": "optional {latitude, longitude, accuracy, name, address}; a standalone pin or venue, never combined with text or media"},
    "user_info": {"user_id": "optional positive 64-bit user ID; defaults to current actor"},
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
        "buttons": "optional list of up to 8 native one-way buttons: {type: url|copy|profile, label, value, style}; copy value is at most 256 characters; profile value defaults to the current actor; style is optional primary, success, or danger; HTML bodies render buttons as in-body <tg-button> pills, Markdown/blocks bodies as PageButton rows"},
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
    "widget_show": {"template": "required one of metric, progress, checklist, flow, chart, status: a pre-made precise widget, built by the widget kit",
        "title": "required short title, 1–128 characters; host metadata, shown as the widget heading",
        "data": "required object for the template: metric {label, value, delta?, note?}; progress {label, done, total, steps? [{label, done}]}; checklist {label, items [{label, done}]}; flow {mermaid: flowchart TD|LR source}; chart {label, kind bar|line, labels, values, unit?}; status {label, rows [{name, state, detail?}]}",
        "companion": "optional Rich HTML message sent with the widget in a DM, carrying the buttons; ignored on the rich surface, which renders the template itself",
        "buttons": "optional list of up to 8 native one-way buttons, same shape as rich_message_send"},
    "request": {"method": "required Telethon TL function name, e.g. channels.GetFullChannel; any TL method may be called, read or write",
        "args": "optional object of JSON scalar fields only; the peer defaults to the current chat, never supplied in args",
        "kwargs": "optional object of nested TL objects, keyed by the method parameter name; each value is a descriptor {\"_type\": \"<Telethon TL type>\", ...fields} whose fields are JSON scalars, flat lists, or nested descriptors — e.g. messages.SetTyping with action {\"_type\": \"SendMessageTextDraftAction\", \"random_id\": 123, \"text\": {\"_type\": \"TextWithEntities\", \"text\": \"...\", \"entities\": []}}"},
}


# Structured requests accept any Telethon TL function name: there is no
# method allowlist. The name is a structured descriptor (one dotted name,
# class part uppercase, optional trailing ``Request``), never a dotted-path
# lookup or code evaluation — _normalize_request_method enforces the shape
# and _execute_request resolves it against telethon.tl.functions only.
#
# Keys that would smuggle routing or credentials into a structured request.
# The peer defaults to the current chat at execution time; this set names
# the refusal so the error says why instead of only listing allowed fields.
_REQUEST_ROUTING_KEYS = {
    "peer", "chat_id", "chat", "channel", "user_id", "user", "recipient",
    "destination", "profile", "api_id", "api_hash", "bot_token", "token",
    "password", "phone", "session", "path",
}
_REQUEST_METHOD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\.([A-Z][A-Za-z0-9]*?)(Request)?$")
# Bounds for nested TL-object descriptors in `request` kwargs: deeper
# nesting is a malformed descriptor, not a real TL value.
_TL_DESCRIPTOR_MAX_DEPTH = 4
_TL_DESCRIPTOR_MAX_LIST = 100


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
        if action in {"pin_message", "unpin_message", "reaction", "ephemeral_delete"}:
            if not self._all_messages_observed(source, [params["message_id"]]):
                return self._error("MESSAGE_SCOPE_INVALID", "The message must have been observed in this exact chat/topic first.")
        if action == "cross_chat_reply":
            try:
                self._cross_chat_target(source, params["message_id"])
            except ValueError as exc:
                return self._error("MESSAGE_SCOPE_INVALID", str(exc))
        return None

    def _resolve_reaction_target(self, source: Any, action: str, clean_params: dict[str, Any]) -> None:
        """Fill an omitted reaction message_id from the triggering message.

        The adapter pins the inbound message id on the source, so a bare
        reaction lands on the message the turn is answering. An unknown
        trigger fails loudly instead of guessing a target.
        """
        if action != "reaction" or clean_params.get("message_id") is not None:
            return
        try:
            clean_params["message_id"] = self._int(
                self._source_value(source, "message_id", None), "message_id")
        except (TypeError, ValueError):
            raise ValueError("reaction needs a message_id when the triggering message is unknown; "
                             "pass message_id of an already-observed message") from None

    def _client(self):
        client = getattr(self.adapter, "_client", None)
        if client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        return client

    @staticmethod
    def _source_value(source: Any, name: str, default: Any = None) -> Any:
        return getattr(source, name, default)

    def _authorize(self, source: Any, *, owner: bool = False) -> tuple[bool, str]:
        """Admission check: a current chat, a non-bot/non-guest actor, admitted in the chat.

        ``owner`` is accepted for call-site compatibility and no longer gates
        anything: every action is available to any admitted user.
        """
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
        return True, ""

    def _profile(self, source: Any) -> str:
        home_fn = getattr(self.adapter, "_settings_home", None)
        if not callable(home_fn):
            raise RuntimeError("Telegram profile resolver is unavailable")
        home = Path(home_fn(source)).expanduser().resolve()
        return str(home)

    async def request(self, source: Any, action: str, params: dict[str, Any] | None = None,
                      *, progress_callback: Any = None, cancel_event: asyncio.Event | None = None) -> dict[str, Any]:
        """Execute a read, direct send, or mutation immediately for any admitted user."""
        action = str(action or "").strip().lower()
        params = dict(params or {})
        if action not in ALL_ACTIONS:
            return self._error("UNSUPPORTED_ACTION", f"Unsupported Telegram operation: {action or '(empty)'}")
        allowed, reason = self._authorize(source)
        if not allowed:
            return self._error("FORBIDDEN", reason)
        if action == "capabilities":
            reads = READ_ACTIONS - {"capabilities"}
            mutations, parameters = _purge_capabilities_listing(self, source)
            return {"ok": True, "status": "complete", "action": action, "data": {
                "reads": sorted(reads), "mutations": mutations,
                "direct_actions": sorted(DIRECT_ACTIONS),
                "structured_requests": sorted(REQUEST_ACTIONS),
                "structured_request_help": "Any Telethon TL method, read or write: method plus args (JSON scalars) and kwargs (nested TL objects); the peer defaults to the current chat.",
                "parameters": parameters,
                "gateway_log_access": "Current profile's gateway.log or errors.log, read-only, newest 1–100 lines, bounded to 64 KiB read and 16,000 output characters, with forced secret/token redaction; no arbitrary paths.",
                "message_access": "History/search use bounded observed messages. Explicit message-ID/file lookup may fetch accessible messages after exact chat/topic verification; user-only history/search RPCs are not used. Purge deletes only your own observed messages in this chat/topic (owner-only; at most 500 observed per chat/topic).",
                "poll_results": "Counts come from observed message media or UpdateMessagePoll; the bot-unsupported messages.getPollResults method is not called. Voter identities are not exposed.",
                "media_download_limit_bytes": None,
                "media_download_transport": f"Telethon/MTProto parallel document parts (up to {PARALLEL_PARTS}); native media fallback; streamed to disk, explicit only",
                "mutations_require_confirmation": False,
            }}
        try:
            profile = self._profile(source)
        except Exception:
            return self._error("PROFILE_UNAVAILABLE", "The Telegram profile could not be resolved safely.")
        if action in DIRECT_ACTIONS:
            try:
                clean_params = self._validate_payload(action, params)
                data = await execute_direct(self, source, action, clean_params, profile)
                return {"ok": True, "status": "complete", "action": action, "data": data}
            except (TypeError, ValueError) as exc:
                return self._error("INVALID_ARGUMENT", str(exc))
            except Exception as exc:
                return self._error(self._error_code(exc), self._safe_error(exc))
        if action in MUTATION_ACTIONS:
            try:
                clean_params = self._validate_payload(action, params)
                self._resolve_reaction_target(source, action, clean_params)
            except (TypeError, ValueError) as exc:
                return self._error("INVALID_ARGUMENT", str(exc))
            if action == "purge":
                clean_params, early = _prepare_purge_request_params(
                    self, source, clean_params, MAX_OBSERVED_MESSAGES)
                if early is not None:
                    return early
            scope_error = self._mutation_scope_error(source, action, clean_params)
            if scope_error is not None:
                return scope_error
            try:
                result = await self._execute_mutation(source, action, clean_params)
            except (TypeError, ValueError) as exc:
                # Execution-time argument/scope errors (unobserved target,
                # wrong chat type, server-rejected reaction) are argument
                # errors, not opaque RPC failures.
                return self._error("INVALID_ARGUMENT", str(exc))
            except Exception as exc:
                return self._error(self._error_code(exc), self._safe_error(exc))
            return {"ok": True, "status": "complete", "action": action, "data": result}
        if action == "request":
            try:
                data = await self._execute_request(source, params)
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
        """Execute one already-staged mutation after rechecking admission.

        Kept for confirmation cards staged before this change: new mutations
        execute immediately in request() and never stage a confirmation.
        """
        if action not in MUTATION_ACTIONS:
            return self._error("UNSUPPORTED_ACTION", "This operation is not a confirmable mutation.")
        allowed, reason = self._authorize(source)
        if not allowed:
            return self._error("FORBIDDEN", reason)
        try:
            clean_params = self._validate_payload(action, params)
            self._resolve_reaction_target(source, action, clean_params)
            if action == "purge":
                clean_params, early = _prepare_purge_request_params(
                    self, source, clean_params, MAX_OBSERVED_MESSAGES)
                if early is not None:
                    return early
            scope_error = self._mutation_scope_error(source, action, clean_params)
            if scope_error is not None:
                return scope_error
            result = await self._execute_mutation(source, action, clean_params)
            return {"ok": True, "status": "complete", "action": action, "data": result}
        except (TypeError, ValueError) as exc:
            return self._error("INVALID_ARGUMENT", str(exc))
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
        """Map a caller-spelled TL name to its canonical spelling.

        Exactly one dot is allowed, so there is no dotted-path traversal into
        arbitrary modules, and the class part must start uppercase, as
        Telethon TL function classes do. A trailing ``Request`` is optional
        spelling. This is a structured descriptor, not code evaluation: the
        name only selects a Telethon TL function class, and any method may
        be called.
        """
        text = method.strip()
        match = _REQUEST_METHOD_RE.match(text)
        if not match:
            raise ValueError("method must be a Telethon TL function name, e.g. channels.GetFullChannel")
        return f"{match.group(1).lower()}.{match.group(2)}"

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
        """Validate a structured request descriptor: method + args + kwargs.

        Any Telethon TL method may be called, read or write. Args carry
        only JSON scalars for the method's own fields; kwargs carries
        nested TL objects keyed by parameter name, each a descriptor
        {"_type": "<TL type>", ...}. Peer/chat/user ids, credentials,
        and path keys are refused — the peer defaults to the current chat.
        """
        if set(p) - {"method", "args", "kwargs"}:
            raise ValueError("request accepts only method, args, and kwargs; the peer defaults to the current chat")
        method = p.get("method")
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a Telethon TL function name, e.g. channels.GetFullChannel")
        normalized = cls._normalize_request_method(method)
        args = p.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("args must be an object of JSON scalar fields")
        smuggled = {key for key in args if str(key).strip().lower() in _REQUEST_ROUTING_KEYS}
        if smuggled:
            raise ValueError("request args must not carry routing or credentials; the peer defaults to the current chat")
        cleaned: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, list):
                if any(isinstance(item, (dict, list)) for item in value):
                    raise ValueError("args must contain JSON scalars or flat lists of them only; "
                                     "use kwargs for nested TL objects")
                cleaned[key] = list(value)
            elif isinstance(value, (str, int, float, bool)) or value is None:
                cleaned[key] = value
            else:
                raise ValueError("args must contain JSON scalars or flat lists of them only; "
                                 "use kwargs for nested TL objects")
        kwargs = p.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise ValueError("kwargs must be an object keyed by the method parameter name")
        smuggled_kwargs = {key for key in kwargs if str(key).strip().lower() in _REQUEST_ROUTING_KEYS}
        if smuggled_kwargs:
            raise ValueError("request kwargs must not carry routing or credentials; the peer defaults to the current chat")
        cleaned_kwargs: dict[str, Any] = {}
        for key, value in kwargs.items():
            cls._validate_tl_value(value)
            cleaned_kwargs[key] = value
        if normalized == "messages.GetForumTopics":
            if "limit" in cleaned:
                cleaned["limit"] = cls._int(cleaned["limit"], "limit", minimum=1, maximum=100)
            if "offset_topic" in cleaned:
                cleaned["offset_topic"] = cls._int(cleaned["offset_topic"], "offset_topic", minimum=0)
        if normalized == "messages.GetForumTopicsByID" and "topics" in cleaned:
            topics = cleaned["topics"]
            if not isinstance(topics, list) or not 1 <= len(topics) <= 100:
                raise ValueError("topics must contain 1–100 topic IDs")
            cleaned_topics = [cls._int(x, "topic_id") for x in topics]
            if len(set(cleaned_topics)) != len(cleaned_topics):
                raise ValueError("topics must be unique")
            cleaned["topics"] = cleaned_topics
        return {"method": normalized, "args": cleaned, "kwargs": cleaned_kwargs}

    @classmethod
    def _validate_tl_value(cls, value: Any, depth: int = 0) -> None:
        """Shape-check one kwargs value: scalar, list, or TL descriptor.

        A dict must carry a "_type" naming a Telethon TL type; its
        fields recurse the same rule. Shape only — construction happens
        in _resolve_tl_value.
        """
        if isinstance(value, list):
            if len(value) > _TL_DESCRIPTOR_MAX_LIST:
                raise ValueError("kwargs lists hold at most 100 items")
            for item in value:
                cls._validate_tl_value(item, depth)
            return
        if isinstance(value, dict):
            if depth >= _TL_DESCRIPTOR_MAX_DEPTH:
                raise ValueError("kwargs nesting is at most 4 levels deep")
            if not isinstance(value.get("_type"), str) or not str(value.get("_type") or "").strip():
                raise ValueError("a kwargs object needs a \"_type\" naming a Telethon TL type")
            for field in value:
                if field != "_type":
                    cls._validate_tl_value(value[field], depth + 1)
            return
        if isinstance(value, (str, int, float, bool)) or value is None:
            return
        raise ValueError("kwargs values must be JSON scalars, lists, or TL-object descriptors")

    @classmethod
    def _resolve_tl_value(cls, value: Any, depth: int = 0) -> Any:
        """Resolve one kwargs value into a scalar, list, or built TL object.

        A dict is a nested TL-object descriptor: {"_type": "<Telethon TL
        type>", ...fields}, resolved against telethon.tl.types only
        (types, never functions). Every required constructor parameter
        must be present; nothing is filled from the current chat inside
        a nested object.
        """
        import inspect

        if isinstance(value, list):
            return [cls._resolve_tl_value(item, depth) for item in value]
        if isinstance(value, dict):
            if depth >= _TL_DESCRIPTOR_MAX_DEPTH:
                raise ValueError("kwargs nesting is at most 4 levels deep")
            type_name = str(value.get("_type") or "").strip()
            if not type_name:
                raise ValueError("a kwargs object needs a \"_type\" naming a Telethon TL type")
            from telethon.tl import types as tl_types

            type_cls = getattr(tl_types, type_name, None)
            if type_cls is None or not isinstance(type_cls, type):
                raise ValueError(f"{type_name} is not a Telethon TL type")
            try:
                signature = inspect.signature(type_cls.__init__)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{type_name} cannot be constructed: {exc}") from exc
            fields: dict[str, Any] = {}
            for param_name, param in signature.parameters.items():
                if param_name == "self" or param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue
                if param_name in value:
                    fields[param_name] = cls._resolve_tl_value(value[param_name], depth + 1)
                elif param.default is inspect.Parameter.empty:
                    raise ValueError(f"{type_name} needs {param_name}, which was not supplied")
            unknown = set(value) - {"_type"} - set(fields)
            if unknown:
                raise ValueError(f"{type_name} has no fields {sorted(unknown)}")
            try:
                return type_cls(**fields)
            except TypeError as exc:
                raise ValueError(f"{type_name} could not be built: {exc}") from exc
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise ValueError("kwargs values must be JSON scalars, lists, or TL-object descriptors")

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
    def _validate_widget_show(cls, p: dict[str, Any]) -> dict[str, Any]:
        """Validate the widget_show action's params; routing stays on the current chat.

        Both surfaces are built here, once: the widget HTML (which refuses
        oversize output through the kit) and the Rich HTML equivalent, so
        execution only picks the surface the resolved peer allows.
        """
        if set(p) - {"template", "title", "data", "companion", "buttons"}:
            raise ValueError("widget_show accepts only template, title, data, companion, "
                             "and buttons; routing is fixed to the current chat")
        title = str(p.get("title") or "").strip()
        if not title or len(title) > 128:
            raise ValueError("widget_show requires a title of 1–128 characters")
        from plugins.platforms.telegram.widget_kit import (
            build_rich_html, build_widget_html, validate_template_data,
        )

        data = validate_template_data(p.get("template"), p.get("data"))
        template = str(p.get("template") or "").strip().lower()
        companion = None
        if p.get("companion"):
            companion, _ = cls._validate_rich_body(
                {"content": p.get("companion"), "format": "html"})
        return {"template": template, "title": title, "data": data,
                "html": build_widget_html(template, title, data),
                "rich_html": build_rich_html(template, title, data),
                "companion": companion,
                "buttons": cls._validate_rich_buttons(p.get("buttons", []))}

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
    @staticmethod
    def _validate_poll_anchor(p: dict[str, Any], result: dict[str, Any]) -> None:
        """Clarify-poll anchor passthrough: the message the poll replies to, and
        the {user_id, name} the caption tags when there is none."""
        reply_to = p.get("reply_to")
        if reply_to is not None:
            if type(reply_to) is not int or reply_to <= 0:
                raise ValueError("reply_to must be a positive message id")
            result["reply_to"] = reply_to
        mention = p.get("mention")
        if mention is not None:
            if (not isinstance(mention, dict) or type(mention.get("user_id")) is not int
                    or not isinstance(mention.get("name"), str) or not mention["name"].strip()
                    or len(mention["name"]) > 100):
                raise ValueError("mention must be {user_id: int, name: non-empty string <= 100 chars}")
            result["mention"] = {"user_id": mention["user_id"], "name": mention["name"]}

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
        # Clarify polls reply to the triggering message and, when there is none,
        # tag the intended user in the caption: both ride through validation.
        cls._validate_poll_anchor(p, result)
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
        if action == "widget_show":
            return cls._validate_widget_show(p)
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
        if action == "leave":
            if p:
                raise ValueError("leave accepts no parameters; it leaves the current chat")
            return {}
        if action == "user_info":
            return {"user_id": str(cls._int(p["user_id"], "user_id", maximum=2**63 - 1))} if "user_id" in p else {}
        if action in {"poll_results", "poll_stop", "message_get", "files_metadata", "files_download"}:
            return {"message_id": cls._int(p.get("message_id"), "message_id")}
        if action == "purge":
            return _validate_purge_payload(cls._int, p, MAX_OBSERVED_MESSAGES)
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
        if action == "reaction":
            if set(p) - {"message_id", "emoji", "big"}:
                raise ValueError("reaction accepts only message_id, emoji, and big; routing is fixed to the current chat/topic")
            message_id = p.get("message_id")
            big = p.get("big", False)
            if type(big) is not bool:
                raise ValueError("big must be a boolean")
            return {"message_id": cls._int(message_id, "message_id") if message_id is not None else None,
                    "emoji": validate_emoji(p.get("emoji")), "big": big}
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
            return validate_send_message_payload(
                p, rich_body=cls._validate_rich_body, rich_buttons=cls._validate_rich_buttons,
                int_value=cls._int, quote_excerpt=cls._validate_quote_excerpt)
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
        if action in {"user_ban", "user_unban"}:
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

    def _html_button(self, source: Any, button: dict[str, Any]) -> str:
        """Render one one-way button as an in-body <tg-button> pill.

        HTML profile buttons need no entity resolution: the target is a
        ``tg://user?id=`` URL for any user ID.
        """
        from plugins.platforms.telegram.mtproto_rich import rich_button

        kind, label, value, style = button["type"], button["label"], button["value"], button.get("style")
        if kind == "profile":
            target_id = int(value) if value is not None else int(source.user_id)
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
                                 *, reply_header: Any = None, scope_source: Any = None,
                                 silent: bool = False, effect: int | None = None) -> dict[str, Any]:
        """Send one native RichMessage with one-way buttons.

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
                    entity = await self._client().get_input_entity(types.PeerUser(target_id))
                    input_user = utils.get_input_user(entity)
                    if not isinstance(input_user, types.InputUser) or int(input_user.user_id) != target_id:
                        raise ValueError("Telegram could not resolve that user's profile")
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
            silent=silent, effect=effect,
        )
        allowed, reason = self._authorize(source)
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
                allowed, _ = self._authorize(source)
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
        if action == "list_chats":  # peer-less read; the sibling validates the limit
            from plugins.platforms.telegram.client_operations_list_chats import list_chats
            return await list_chats(self, params)
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
        """Execute one structured TL request, with the peer defaulting to the current chat.

        Any Telethon TL method may be called: the name is resolved against
        telethon.tl.functions only, never by evaluating caller text. The peer
        is built from _peer(source) for every method, never from params. The
        call goes through adapter._mtproto_rich_request — the same live
        client every other read uses — so an open control card's
        SendMessage/EditMessageRequest rewrite in _route_control_request
        still applies. No new client, session, or credential is created here.
        """
        from telethon import functions, types
        p = self._validate_payload("request", params)
        peer = await self._peer(source)
        method = p["method"]
        args = p["args"]
        kwargs = {key: self._resolve_tl_value(value) for key, value in p.get("kwargs", {}).items()}
        if kwargs and method in {"channels.GetFullChannel", "messages.GetFullChat", "users.GetFullUser",
                                "messages.GetForumTopics", "messages.GetForumTopicsByID"}:
            raise ValueError(f"kwargs is not supported for {method}; its parameters are scalars only")
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
        else:
            request = self._build_generic_request(method, args, peer, kwargs)
        result = await self.adapter._mtproto_rich_request(request)
        return {"method": method, "result": self._bounded_request_result(result)}

    @staticmethod
    def _build_generic_request(method: str, args: dict[str, Any], peer: Any,
                               kwargs: dict[str, Any] | None = None) -> Any:
        """Build any Telethon TL request from its dotted name, scalar args, and nested kwargs.

        The request class is resolved against telethon.tl.functions only.
        Peer-like parameters the caller did not supply are filled from the
        current chat's peer; kwargs holds already-resolved nested TL
        objects, merged before that fill. Every other required parameter
        must be supplied in args (scalar) or kwargs (nested TL object).
        A name with no matching TL function, or unfillable required
        parameters, fails before any transport call.
        """
        import inspect

        from telethon import functions, types
        namespace, name = method.split(".", 1)
        module = getattr(functions, namespace, None)
        request_cls = getattr(module, f"{name}Request", None) if module is not None else None
        if request_cls is None:
            raise ValueError(f"{method} is not a Telethon TL function")
        filled = dict(args)
        for key, value in (kwargs or {}).items():
            filled[key] = value
        try:
            signature = inspect.signature(request_cls.__init__)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{method} cannot be constructed: {exc}") from exc
        for param_name, param in signature.parameters.items():
            if param_name == "self" or param_name in filled:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param.default is not inspect.Parameter.empty:
                continue
            annotation = str(param.annotation or "")
            if param_name == "peer" or "InputPeer" in annotation:
                filled[param_name] = peer
            elif param_name == "channel" or "InputChannel" in annotation:
                if not isinstance(peer, types.InputPeerChannel):
                    raise ValueError(f"{method} needs a channel peer for {param_name}; the current chat is not a supergroup or channel")
                filled[param_name] = types.InputChannel(
                    channel_id=peer.channel_id, access_hash=peer.access_hash)
            elif param_name == "chat_id":
                if not isinstance(peer, types.InputPeerChat):
                    raise ValueError(f"{method} needs a basic group chat for chat_id")
                filled[param_name] = peer.chat_id
            elif "InputUser" in annotation:
                if not isinstance(peer, (types.InputPeerUser, types.InputPeerSelf)):
                    raise ValueError(f"{method} needs a user peer for {param_name}; the current chat is not a direct message")
                filled[param_name] = peer
            else:
                raise ValueError(f"{method} requires {param_name}, which was not supplied")
        try:
            return request_cls(**filled)
        except TypeError as exc:
            raise ValueError(f"{method} could not be built from the supplied args: {exc}") from exc

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
        # A mention tags the intended user in the caption (offset 0: the tag line
        # starts with their name). Telegram entity offsets are UTF-16 units.
        description = p.get("description") or ""
        entities = None
        mention = p.get("mention") or {}
        if (isinstance(mention, dict) and mention.get("name")
                and description.startswith(str(mention["name"]))):
            name = str(mention["name"])
            entities = [types.MessageEntityMentionName(
                offset=0, length=len(name.encode("utf-16-le")) // 2,
                user_id=int(mention["user_id"]))]
        request = functions.messages.SendMediaRequest(peer=peer, media=media, message=description,
                                                      reply_to=self._reply_header(thread_id, p.get("reply_to")),
                                                      entities=entities)
        result = await self.adapter._mtproto_rich_request(request)
        from .topic_delivery import sent_message_id
        message_id = sent_message_id(result, random_id=request.random_id, peer=peer,
                                     topic_id=int(thread_id) if thread_id else None)
        poll_id = poll.id  # replaced below by the id the server echoes in the sent message
        for update in getattr(result, "updates", []) or []:
            message = getattr(update, "message", None)
            if message is not None and str(getattr(message, "id", "")) == str(message_id):
                from telethon.utils import get_peer_id
                if (str(get_peer_id(message.peer_id)) == str(source.chat_id)
                        and str(self.adapter._message_thread_id(message) or "") == str(thread_id or "")):
                    self.observe(source, message)
                    poll_id = getattr(getattr(getattr(message, "media", None), "poll", None), "id", None) or poll_id
        return {"message_id": int(message_id) if message_id else None,
                "poll_id": poll_id, "question": p["question"], "options": p["options"]}

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
        if action == "purge":
            return await _execute_purge(self, peer, p, MAX_OBSERVED_MESSAGES)
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
        if action == "reaction":
            return await _execute_agent_reaction(self, source, peer, p)
        if action == "leave":
            # Current chat only, no chat_id param (same routing discipline as
            # reaction): a supergroup/channel leaves via LeaveChannel, a
            # basic group removes the bot itself via DeleteChatUser, and a
            # DM has no chat to leave.
            if isinstance(peer, types.InputPeerChannel):
                channel = types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)
                await self.adapter._mtproto_rich_request(functions.channels.LeaveChannelRequest(channel=channel))
                return {"left": True, "chat_type": "supergroup_or_channel"}
            if isinstance(peer, types.InputPeerChat):
                await self.adapter._mtproto_rich_request(functions.messages.DeleteChatUserRequest(
                    chat_id=peer.chat_id, user_id=types.InputUserSelf()))
                return {"left": True, "chat_type": "group"}
            raise ValueError("There is no group to leave in a direct message")
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
            from .client_operations_send import execute_send_message

            return await execute_send_message(self, source, p)
        raise ValueError(f"Unsupported mutation: {action}")

    @staticmethod
    def _reply_header(thread_id: Any, reply_to: Any = None) -> Any:
        """Reply header for a poll send: to ``reply_to`` when given (the clarify
        poll answering the triggering message), else the thread root."""
        if not thread_id and not reply_to:
            return None
        from plugins.platforms.telegram.topic_delivery import topic_reply_header
        target = int(reply_to) if reply_to else int(thread_id)
        return topic_reply_header(target, int(thread_id) if thread_id else None)
