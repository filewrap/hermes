"""Telegram native forum-topic management UI.

Telegram is the source of truth for topic metadata and permissions. Bot sessions
cannot enumerate topics (messages.getForumTopics is user-only), so known topic
IDs are refreshed with the bot-supported messages.getForumTopicsByID method.
"""
from __future__ import annotations

import contextlib
import logging
import math
import re
import secrets
import time
from html import escape
from pathlib import Path
from typing import Any, Optional

from .mtproto_rich import edit_request, inline_markup

_LOG = logging.getLogger(__name__)

_STATE_TTL = 900
_PAGE_SIZE = 8
_NUMERIC_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_CHAT_ID = re.compile(r"^(?:[1-9][0-9]{0,19}|-100[1-9][0-9]{0,19})$")


def _page_footer(page: int, pages: int, total: int) -> str:
    return f"Page {int(page) + 1} of {int(pages)} • total {int(total)}"


def _html_code(value: Any) -> str:
    """Render dynamic command text safely inside Telegram HTML code markup."""
    return f"<code>{escape(str(value))}</code>"


def _command_help_html() -> str:
    commands = (
        "/thread create <title>", "/thread list [page]", "/thread open <topic-id>",
        "/thread select <topic-id>", "/thread rename <topic-id> <title>",
        "/thread close <topic-id>", "/thread reopen <topic-id>",
        "/thread archive 1", "/thread unarchive 1", "/thread delete <topic-id>",
    )
    return "<h1>Thread commands</h1><hr><ul>" + "".join(
        f"<li>{_html_code(command)}</li>" for command in commands
    ) + "</ul>"


def _topic_id(topic: Any) -> Optional[int]:
    try:
        value = int(getattr(topic, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _response_message_id(response: Any) -> Optional[str]:
    """Extract the card's message id from a single MTProto send response."""
    if response is None:
        return None
    receipt = getattr(response, "message_id", None)
    if receipt not in (None, ""):
        return str(receipt)
    direct = getattr(response, "id", None)
    if direct is not None and type(response).__name__ != "UpdateMessageID":
        return str(direct)
    pending = [response]
    seen: set[int] = set()
    while pending:
        item = pending.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        message = getattr(item, "message", None)
        if message is not None and not isinstance(message, str):
            if getattr(message, "id", None) is not None and getattr(message, "out", False):
                return str(message.id)
        pending.extend(getattr(item, "updates", ()) or ())
        nested = getattr(item, "update", None)
        if nested is not None:
            pending.append(nested)
    return None


class TelegramThreadUI:
    """Buttoned Telegram forum-topic manager using the adapter's live Telethon client."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._states: dict[str, dict[str, Any]] = {}
        self._confirmations: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _allowed_chat(source: Any) -> bool:
        return (
            str(getattr(source, "chat_type", "")) in {"group", "forum", "dm", "private", "direct"}
            and bool(_CHAT_ID.fullmatch(str(getattr(source, "chat_id", "") or "")))
            and bool(_NUMERIC_ID.fullmatch(str(getattr(source, "user_id", "") or "")))
            and not bool(getattr(source, "is_bot", False))
        )

    def _authorized_owner(self, source: Any) -> bool:
        if not self._allowed_chat(source):
            return False
        owner_check = getattr(self.adapter, "_is_owner_user", None)
        authorized = getattr(self.adapter, "_authorized", None)
        try:
            self._home(source)
            # Fail closed if either gateway authorization or the profile owner cannot be
            # established. In multiplex deployments these methods resolve the source's home.
            return bool(callable(owner_check) and owner_check(source.user_id, source)
                        and callable(authorized) and authorized(source))
        except Exception:
            return False

    def _home(self, source: Any) -> str:
        runner = getattr(self.adapter, "gateway_runner", None)
        route = getattr(runner, "_resolve_profile_home_for_source", None)
        if callable(route):
            # A missing routed home must not silently fall back to the launch profile.
            home = route(source)
        else:
            resolver = getattr(self.adapter, "_settings_home", None)
            home = resolver(source) if callable(resolver) else None
        if home is None:
            raise RuntimeError("Telegram profile routing is unavailable")
        return str(Path(home).expanduser().resolve())

    @contextlib.contextmanager
    def _profile_scope(self, home: str):
        if not home:
            raise RuntimeError("The owning Hermes profile could not be resolved")
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(Path(home)):
            yield

    def _prune(self) -> None:
        cutoff = time.monotonic() - _STATE_TTL
        for key, state in list(self._states.items()):
            if state["created_at"] < cutoff:
                self._states.pop(key, None)
        for key, state in list(self._confirmations.items()):
            if state["created_at"] < cutoff:
                self._confirmations.pop(key, None)

    def _new_state(self, source: Any, home: str, *, page: int = 0) -> dict[str, Any]:
        state_id = secrets.token_hex(8)
        state = {
            "id": state_id,
            "chat_id": str(source.chat_id),
            "user_id": str(source.user_id),
            "source_profile": getattr(source, "profile", None),
            "source_thread_id": str(getattr(source, "thread_id", "") or "") or None,
            "chat_type": str(source.chat_type),
            "profile_home": home,
            "source": source,
            "message_id": None,
            "created_at": time.monotonic(),
            "page": max(0, int(page)),
            "page_topics": [],
            "total": 0,
            "notice": "",
            "view": "list",
            "pending_topic": None,
            "confirmation": None,
        }
        self._states[state_id] = state
        return state

    def _callback(self, state: dict, action: str, value: Any = "") -> str:
        payload = f"hm:thread:{state['id']}:{action}"
        if value not in (None, ""):
            payload += f":{value}"
        if len(payload.encode("utf-8")) > 64:
            raise ValueError("Telegram thread callback payload is too long")
        return payload

    @staticmethod
    def _is_general(topic: Any) -> bool:
        return bool(getattr(topic, "is_general", False)) or _topic_id(topic) == 1

    def _render(self, state: dict) -> tuple[str, Any]:
        topic = state.get("pending_topic")
        if state["view"] == "confirm-delete" and topic is not None:
            label = escape(str(getattr(topic, "title", "") or "Untitled topic")[:100])
            text = (
                f"<h1>Delete topic</h1><p><b>{label}</b> and all of its Telegram topic history will be permanently deleted.</p>"
                "<hr>"
                "<p>This cannot be undone. Confirming here authorizes one deletion only.</p>"
                f"<p><i>{_page_footer(0, 1, 1)}</i></p>"
            )
            markup = inline_markup([[
                ("Delete permanently", self._callback(state, "delete-confirm", state["confirmation"]), "danger"),
                ("Cancel", self._callback(state, "delete-cancel"), None),
            ]])
            return text, markup

        if state["view"] == "error":
            return (
                "<h1>Topic action status unknown</h1><hr>"
                "<p>Check Telegram before retrying this action.</p>"
                f"<p><i>{_page_footer(0, 1, 1)}</i></p>",
                None,
            )

        topics = list(state.get("page_topics") or [])
        total = max(0, int(state.get("total", 0)))
        pages = max(1, math.ceil(total / _PAGE_SIZE))
        page = max(0, min(int(state.get("page", 0)), pages - 1))
        state["page"] = page
        blocks = [
            "<h1>Telegram topics</h1>",
            "<p>Known topics, refreshed directly from Telegram. Telegram does not let bots enumerate all topics. "
            "For a missing topic, send a message in it or use <code>/thread open &lt;topic-id&gt;</code>.</p>",
        ]
        buttons = []
        for item in topics:
            item_id = _topic_id(item)
            if item_id is None:
                continue
            title = escape(str(getattr(item, "title", "") or "Untitled topic")[:120])
            status = "Hidden" if bool(getattr(item, "hidden", False)) else (
                "Closed" if bool(getattr(item, "closed", False)) else "Open"
            )
            unread = max(0, int(getattr(item, "unread_count", 0) or 0))
            root = int(getattr(item, "top_message", 0) or 0)
            detail = f"{status} · Topic {item_id}"
            if unread:
                detail += f" · {unread} unread"
            blocks.append(f"<hr><h2>{title}</h2><p>{escape(detail)}</p>")
            buttons.append([("Open", self._callback(state, "select", item_id), "primary"),
                            ("Rename", self._callback(state, "rename-help", item_id), None)])
            if not self._is_general(item):
                action = "reopen" if bool(getattr(item, "closed", False)) else "close"
                label = "Reopen" if action == "reopen" else "Close"
                buttons.append([(label, self._callback(state, action, item_id), "success" if action == "reopen" else None),
                                ("Delete…", self._callback(state, "delete", item_id), "danger")])
            else:
                hidden = bool(getattr(item, "hidden", False))
                action = "unarchive" if hidden else "archive"
                buttons.append([("Unarchive" if hidden else "Archive",
                                 self._callback(state, action, item_id), None)])
            if root > 0 and state["chat_type"] in {"group", "forum"}:
                raw_chat = str(state["chat_id"])
                if raw_chat.startswith("-100") and raw_chat[4:].isdigit():
                    blocks.append(f'<p><a href="https://t.me/c/{raw_chat[4:]}/{item_id}">Open in Telegram</a></p>')
        if not topics:
            blocks.append("<p>No known topics were returned for this chat.</p>")
        if state.get("notice"):
            blocks.append(f"<blockquote>{escape(str(state['notice'])[:240])}</blockquote>")
        blocks.append(f"<p><i>{_page_footer(page, pages, total)}</i></p>")
        nav = []
        if page > 0:
            nav.append(("Previous", self._callback(state, "page", page - 1), None))
        if page + 1 < pages:
            nav.append(("Next", self._callback(state, "page", page + 1), None))
        nav.append(("Refresh", self._callback(state, "refresh"), "primary"))
        buttons.append(nav)
        return "\n".join(blocks), inline_markup(buttons)

    def _known_topic_ids(self, state: dict) -> list[int]:
        from .topic_catalog import known_topic_ids
        return known_topic_ids(state["profile_home"], state["chat_id"], state["user_id"],
                               getattr(self.adapter, "_bot_id", None))

    def remember_source_topic(self, source: Any, topic_id: Any = None) -> None:
        """Record only an admitted owner's topic ID, never cached metadata or authority."""
        topic_id = topic_id or getattr(source, "thread_id", None)
        if not _NUMERIC_ID.fullmatch(str(topic_id or "")) or not self._authorized_owner(source):
            return
        from .topic_catalog import remember_topic
        try:
            remember_topic(self._home(source), str(source.chat_id), str(source.user_id),
                           getattr(self.adapter, "_bot_id", None), int(topic_id))
        except OSError:
            _LOG.warning("Could not persist an observed Telegram topic ID")

    async def _fetch_page(self, state: dict) -> None:
        if self.adapter._client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        from telethon import functions
        peer = self.adapter._peer_for(state["chat_id"])
        known_ids = sorted(topic_id for topic_id in set(self._known_topic_ids(state))
                           if self.adapter._authorized(self._topic_context(state["source"], topic_id)))
        topics = {}
        # IDs come only from this owner's scoped observations, never
        # numeric probing. A successful by-ID response, not the catalog, proves existence.
        for offset in range(0, len(known_ids), 100):
            batch = known_ids[offset:offset + 100]
            result = await self.adapter._client(functions.messages.GetForumTopicsByIDRequest(
                peer=peer, topics=batch,
            ))
            for topic in getattr(result, "topics", ()) or ():
                topic_id = _topic_id(topic)
                if topic_id in batch and getattr(topic, "title", None) is not None:
                    topics[topic_id] = topic
        rows = [topics[topic_id] for topic_id in sorted(topics)]
        state["total"] = len(rows)
        state["page"] = min(max(0, int(state["page"])), max(0, math.ceil(len(rows) / _PAGE_SIZE) - 1))
        start = state["page"] * _PAGE_SIZE
        state["page_topics"] = rows[start:start + _PAGE_SIZE]

    async def _send_or_edit(self, state: dict, source: Any, *, event: Any = None,
                            reply_to: Any = None, fresh: bool = False) -> bool:
        text, markup = self._render(state)
        if state.get("message_id") and not fresh:
            edit = getattr(self.adapter, "_edit_rich_ui_message", None)
            if callable(edit):
                await edit(event, source, text, markup)
            else:
                await self.adapter._mtproto_rich_request(edit_request(
                    self.adapter._peer_for(state["chat_id"]), int(state["message_id"]), text,
                    reply_markup=markup,
                ))
            return True
        response = await self.adapter._send_rich_ui(source, text, markup, reply_to=reply_to)
        state["message_id"] = _response_message_id(response)
        return bool(state["message_id"])

    async def _open_list(self, source: Any, *, reply_to: Any = None, page: int = 0,
                         notice: str = "") -> None:
        home = self._home(source)
        state = self._new_state(source, home, page=page)
        state["notice"] = str(notice or "")
        try:
            with self._profile_scope(home):
                await self._fetch_page(state)
                sent = await self._send_or_edit(state, source, reply_to=reply_to)
            if not sent:
                self._states.pop(state["id"], None)
        except Exception as exc:
            self._states.pop(state["id"], None)
            _LOG.warning("Telegram messages.getForumTopicsByID topic refresh failed (%s)", type(exc).__name__)
            await self._send_message(source, f"Could not refresh the topic list ({type(exc).__name__}); check Telegram before retrying.", reply_to=reply_to)

    async def _send_message(self, source: Any, text: str, *, reply_to: Any = None,
                            html: bool = False) -> None:
        body = str(text)[:2000] if html else escape(str(text)[:500])
        await self.adapter._send_rich_ui(source, f"<p>{body}</p>" if not html else body,
                                         None, reply_to=reply_to)

    async def _load_topic(self, source: Any, topic_id: int) -> Any:
        if self.adapter._client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        from telethon import functions
        if not self.adapter._authorized(self._topic_context(source, topic_id)):
            raise ValueError("That topic is not authorized for this Telegram conversation")
        peer = self.adapter._peer_for(source.chat_id)
        result = await self.adapter._client(functions.messages.GetForumTopicsByIDRequest(
            peer=peer, topics=[int(topic_id)],
        ))
        for topic in getattr(result, "topics", ()) or ():
            if _topic_id(topic) == topic_id and getattr(topic, "title", None) is not None:
                self.remember_source_topic(source, topic_id)
                return topic
        raise ValueError("That topic was not found in the current Telegram chat")

    @staticmethod
    def _topic_context(source: Any, topic_id: int) -> Any:
        try:
            from gateway.session_identity import replace_source
            return replace_source(source, thread_id=str(topic_id))
        except (TypeError, ValueError, AttributeError):
            from types import SimpleNamespace
            values = dict(vars(source)) if hasattr(source, "__dict__") else {}
            values["chat_id"] = getattr(source, "chat_id", None)
            values["chat_type"] = getattr(source, "chat_type", None)
            values["user_id"] = getattr(source, "user_id", None)
            values["thread_id"] = str(topic_id)
            return SimpleNamespace(**values)

    async def _open_topic(self, source: Any, topic: Any, *, reply_to: Any = None) -> None:
        topic_id = _topic_id(topic)
        if not topic_id:
            raise ValueError("Telegram did not return a usable topic root")
        title = escape(str(getattr(topic, "title", "Untitled topic"))[:120])
        text = f"<h1>{title}</h1><p>Topic selected. Continue the conversation in this Telegram topic.</p>"
        context = self._topic_context(source, topic_id)
        await self.adapter._send_rich_ui(context, text, None, reply_to=topic_id)
        if reply_to is not None:
            await self._send_message(source, f"Opened topic {topic_id} in this chat.", reply_to=reply_to)

    async def _mutate(self, source: Any, action: str, topic: Any, value: str = "") -> None:
        from telethon import functions
        topic_id = _topic_id(topic)
        if topic_id is None:
            raise ValueError("Invalid Telegram topic id")
        peer = self.adapter._peer_for(source.chat_id)
        async def rename() -> None:
            title = " ".join(str(value or "").split()).strip(" `\"'")
            if not title:
                raise ValueError("Provide a non-empty topic title")
            if len(title.encode("utf-8")) > 128:
                raise ValueError("Telegram topic titles must be at most 128 UTF-8 bytes")
            await self.adapter._client(functions.messages.EditForumTopicRequest(
                peer=peer, topic_id=topic_id, title=title,
            ))

        async def set_closed(closed: bool) -> None:
            if self._is_general(topic):
                raise ValueError("Telegram does not support closing the General topic")
            await self.adapter._client(functions.messages.EditForumTopicRequest(
                peer=peer, topic_id=topic_id, closed=closed,
            ))

        async def set_hidden(hidden: bool) -> None:
            if not self._is_general(topic):
                raise ValueError("Telegram supports hiding only the General topic; use Close for other topics")
            await self.adapter._client(functions.messages.EditForumTopicRequest(
                peer=peer, topic_id=topic_id, hidden=hidden,
            ))

        async def delete() -> None:
            if self._is_general(topic):
                raise ValueError("The General topic cannot be deleted from this UI")
            # DeleteTopicHistoryRequest is Telegram's native irreversible topic removal API.
            # This method is called only after a fresh, separate one-shot confirmation.
            await self.adapter._client(functions.messages.DeleteTopicHistoryRequest(
                peer=peer, top_msg_id=topic_id,
            ))
            from .topic_catalog import forget_topic
            try:
                forget_topic(self._home(source), str(source.chat_id), str(source.user_id),
                             getattr(self.adapter, "_bot_id", None), topic_id)
            except OSError:
                _LOG.warning("Could not remove a deleted Telegram topic ID from the catalog")

        handlers = {
            "rename": rename,
            "close": lambda: set_closed(True),
            "reopen": lambda: set_closed(False),
            "archive": lambda: set_hidden(True),
            "unarchive": lambda: set_hidden(False),
            "delete": delete,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ValueError("Unsupported Telegram topic action")
        await handler()

    def _fresh_callback_source(self, source: Any, state: dict) -> bool:
        if (str(getattr(source, "chat_id", "")) != state["chat_id"]
                or str(getattr(source, "user_id", "") or "") != state["user_id"]
                or str(getattr(source, "chat_type", "")) != state["chat_type"]
                or getattr(source, "profile", None) != state.get("source_profile")):
            return False
        actual_thread = str(getattr(source, "thread_id", "") or "")
        if actual_thread and actual_thread != (state["source_thread_id"] or ""):
            return False
        try:
            bound_source = self._restore_callback_source(source, state)
            if self._home(bound_source) != state["profile_home"]:
                return False
        except Exception:
            return False
        return self._authorized_owner(bound_source)

    def _consume_callback_state(self, state_id: str, state: dict) -> None:
        """Invalidate this card's callback token before the handler's first await."""
        self._states.pop(state_id, None)
        replacement = secrets.token_hex(8)
        while replacement in self._states or replacement == state_id:
            replacement = secrets.token_hex(8)
        state["id"] = replacement
        self._states[replacement] = state
        confirmation = state.get("confirmation")
        if confirmation:
            pending = self._confirmations.get(confirmation)
            if pending is not None and pending.get("state_id") == state_id:
                pending["state_id"] = replacement

    def _restore_callback_source(self, source: Any, state: dict) -> Any:
        """Restore the originating conversation thread when callback metadata omitted it."""
        current_thread = str(getattr(source, "thread_id", "") or "")
        originating = state.get("source_thread_id")
        if not current_thread and originating:
            return self._topic_context(source, int(originating))
        return source

    async def handle_command(self, source: Any, arguments: str, reply_to: Any = None) -> None:
        """Handle `/thread` list/open/rename/archive/close/reopen/delete commands."""
        self._prune()
        if not self._authorized_owner(source):
            await self._send_message(source, "Thread management is restricted to the authorized profile owner.", reply_to=reply_to)
            return
        self.remember_source_topic(source)
        raw = str(arguments or "").strip()
        action, _, rest = raw.partition(" ")
        action = action.casefold()
        if action == "create":
            title = rest.strip()
            if not title:
                await self._send_message(source, f"Usage: {_html_code('/thread create <title>')}",
                                         reply_to=reply_to, html=True)
                return
            creator = getattr(self.adapter, "_create_topic_from_message", None)
            if not callable(creator):
                await self._send_message(source, "Native Telegram topic creation is unavailable.", reply_to=reply_to)
                return
            await creator(source, title, reply_to=reply_to,
                          welcome_text="Topic created. Continue in this thread.")
            return
        if action in {"", "list", "topics"}:
            page = 0
            if rest.strip():
                try:
                    page = max(0, int(rest.strip()) - 1)
                except ValueError:
                    await self._send_message(source, _command_help_html(), reply_to=reply_to, html=True)
                    return
                if page > 99:
                    await self._send_message(source, "Use page buttons to browse beyond page 100.", reply_to=reply_to)
                    return
            await self._open_list(source, reply_to=reply_to, page=page)
            return
        if action == "help":
            await self._send_message(source, _command_help_html(), reply_to=reply_to, html=True)
            return
        first, _, tail = rest.strip().partition(" ")
        if not _NUMERIC_ID.fullmatch(first):
            await self._send_message(source, "A positive numeric Telegram topic ID is required.", reply_to=reply_to)
            return
        topic_id = int(first)
        try:
            home = self._home(source)
            with self._profile_scope(home):
                topic = await self._load_topic(source, topic_id)
                handlers = {
                    "open": lambda: self._open_topic(source, topic, reply_to=reply_to),
                    "select": lambda: self._open_topic(source, topic, reply_to=reply_to),
                    "delete": lambda: self._show_delete_confirmation(source, topic, home, reply_to=reply_to),
                    "rename": lambda: self._mutate(source, "rename", topic, tail),
                    "close": lambda: self._mutate(source, "close", topic),
                    "reopen": lambda: self._mutate(source, "reopen", topic),
                    "archive": lambda: self._mutate(source, "archive", topic),
                    "unarchive": lambda: self._mutate(source, "unarchive", topic),
                }
                handler = handlers.get(action)
                if handler is None:
                    await self._send_message(source, "Use `/thread list` to browse topics or `/thread help` for supported operations.", reply_to=reply_to)
                    return
                await handler()
                if action in {"open", "select", "delete"}:
                    return
            final_state = {"rename": "renamed", "close": "closed", "reopen": "reopened",
                           "archive": "archived", "unarchive": "unarchived"}.get(action, action)
            await self._open_list(source, reply_to=reply_to, notice=f"Topic {topic_id} {final_state} successfully.")
        except Exception:
            await self._send_message(source, "Action status is unknown; check Telegram before retrying.", reply_to=reply_to)

    async def _show_delete_confirmation(self, source: Any, topic: Any, home: str, *, reply_to: Any = None,
                                        state: Optional[dict] = None, event: Any = None) -> None:
        topic_id = _topic_id(topic)
        if topic_id is None:
            raise ValueError("Invalid Telegram topic id")
        if state is None:
            state = self._new_state(source, home)
        confirmation = secrets.token_hex(8)
        self._confirmations[confirmation] = {
            "created_at": time.monotonic(), "state_id": state["id"],
            "chat_id": str(source.chat_id), "user_id": str(source.user_id),
            "profile_home": home, "topic_id": topic_id,
        }
        state.update(view="confirm-delete", pending_topic=topic, confirmation=confirmation,
                     notice="", page_topics=[])
        sent = await self._send_or_edit(state, source, event=event, reply_to=reply_to,
                                        fresh=not bool(state.get("message_id")))
        if not sent:
            self._confirmations.pop(confirmation, None)
            self._states.pop(state["id"], None)

    async def _callback_card_update(self, state: dict, source: Any, event: Any,
                                    *, notice: str = "") -> None:
        state["notice"] = notice
        state["view"] = "list"
        state["pending_topic"] = None
        state["confirmation"] = None
        await self._fetch_page(state)
        await self._send_or_edit(state, source, event=event)

    async def _callback_page(self, event: Any, source: Any, state: dict, value: str) -> None:
        page = int(value)
        pages = max(1, math.ceil(max(0, int(state.get("total", 0))) / _PAGE_SIZE))
        if page < 0 or page >= pages:
            raise ValueError("That topic page is not available")
        state["page"] = page
        await self._callback_card_update(state, source, event)

    async def _callback_topic_action(self, event: Any, source: Any, state: dict,
                                     action: str, value: str) -> None:
        if not _NUMERIC_ID.fullmatch(value):
            raise ValueError("Invalid topic ID")
        progress = {"close": "Closing topic…", "reopen": "Reopening topic…",
                    "archive": "Archiving General…", "unarchive": "Unarchiving General…"}.get(action, "Opening topic…")
        state["notice"] = progress
        await self._send_or_edit(state, source, event=event)
        topic = await self._load_topic(source, int(value))
        if action == "select":
            await self._open_topic(source, topic)
            await self._callback_card_update(state, source, event, notice=f"Opened topic {value}.")
        else:
            await self._mutate(source, action, topic)
            final_state = {"close": "closed", "reopen": "reopened", "archive": "archived",
                           "unarchive": "unarchived"}.get(action, "updated")
            await self._callback_card_update(state, source, event, notice=f"Topic {value} {final_state}.")

    async def _callback_rename_help(self, event: Any, source: Any, state: dict, value: str) -> None:
        command = _html_code(f"/thread rename {value} <new title>")
        await self._send_message(source, f"Rename this topic with {command}.",
                                 reply_to=getattr(event, "message_id", None), html=True)
        await self._callback_card_update(state, source, event,
                                         notice=f"Rename with /thread rename {value} <new title>.")

    async def _callback_delete_ask(self, event: Any, source: Any, state: dict,
                                   home: str, value: str) -> None:
        if not _NUMERIC_ID.fullmatch(value):
            raise ValueError("Invalid topic ID")
        topic = await self._load_topic(source, int(value))
        await self._show_delete_confirmation(source, topic, home, state=state, event=event)

    async def _callback_delete_cancel(self, event: Any, source: Any, state: dict) -> None:
        confirmation = state.get("confirmation")
        if confirmation:
            self._confirmations.pop(confirmation, None)
        await self._callback_card_update(state, source, event,
                                         notice="Deletion cancelled; no topic data was changed.")

    async def _callback_delete_confirm(self, event: Any, source: Any, state: dict,
                                       value: str, home: str) -> None:
        confirmation = self._confirmations.get(value)
        if (not confirmation or confirmation.get("state_id") != state["id"]
                or confirmation.get("chat_id") != state["chat_id"]
                or confirmation.get("user_id") != state["user_id"]
                or confirmation.get("profile_home") != home
                or not state.get("pending_topic")
                or _topic_id(state["pending_topic"]) != confirmation.get("topic_id")
                or state.get("confirmation") != value):
            raise ValueError("This deletion confirmation is no longer valid")
        # Consume consent before any irreversible operation: a replay or concurrent duplicate
        # callback cannot cause a second delete, even if the native API fails.
        self._confirmations.pop(value, None)
        state.update(view="list", pending_topic=None, confirmation=None,
                     notice="Deleting topic history…")
        await self._fetch_page(state)
        await self._send_or_edit(state, source, event=event)
        topic = await self._load_topic(source, int(confirmation["topic_id"]))
        if _topic_id(topic) != confirmation["topic_id"]:
            raise ValueError("The topic changed before confirmation")
        await self._mutate(source, "delete", topic)
        await self._callback_card_update(
            state, source, event,
            notice=f"Topic {confirmation['topic_id']} and its Telegram history were deleted.",
        )

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        """Dispatch owner-bound `hm:thread:` callbacks; return False for other payloads."""
        if not str(data or "").startswith("hm:thread:"):
            return False
        self._prune()
        parts = str(data).split(":", 5)
        if len(parts) < 4:
            await event.answer("This thread action is invalid or expired.", alert=True)
            return True
        state_id, action = parts[2], parts[3]
        value = parts[4] if len(parts) > 4 else ""
        state = self._states.get(state_id)
        if (state is None or not state.get("message_id")
                or str(getattr(event, "message_id", "") or "") != str(state.get("message_id"))
                or not self._fresh_callback_source(source, state)):
            await event.answer("This thread card expired or belongs to another account or profile.", alert=True)
            return True
        # This mutation is intentionally synchronous and precedes the first await. A replay or
        # parallel double-tap still carries the retired token and cannot enter an action handler.
        self._consume_callback_state(state_id, state)
        home = state["profile_home"]
        try:
            source = self._restore_callback_source(source, state)
            await event.answer({
                "page": "Loading topic page…", "refresh": "Refreshing topics…", "open": "Opening topic…",
                "select": "Opening topic…",
                "close": "Closing topic…", "reopen": "Reopening topic…", "archive": "Archiving General…",
                "unarchive": "Unarchiving General…",
                "rename-help": "Preparing rename instructions…", "delete": "Preparing deletion confirmation…",
                "delete-confirm": "Deleting topic history…", "delete-cancel": "Deletion cancelled.",
            }.get(action, "Working…"))
            with self._profile_scope(home):
                handlers = {
                    "page": lambda: self._callback_page(event, source, state, value),
                    "refresh": lambda: self._callback_card_update(state, source, event),
                    "rename-help": lambda: self._callback_rename_help(event, source, state, value),
                    "open": lambda: self._callback_topic_action(event, source, state, "select", value),
                    "select": lambda: self._callback_topic_action(event, source, state, "select", value),
                    "close": lambda: self._callback_topic_action(event, source, state, "close", value),
                    "reopen": lambda: self._callback_topic_action(event, source, state, "reopen", value),
                    "archive": lambda: self._callback_topic_action(event, source, state, "archive", value),
                    "unarchive": lambda: self._callback_topic_action(event, source, state, "unarchive", value),
                    "delete": lambda: self._callback_delete_ask(event, source, state, home, value),
                    "delete-cancel": lambda: self._callback_delete_cancel(event, source, state),
                    "delete-confirm": lambda: self._callback_delete_confirm(event, source, state, value, home),
                }
                handler = handlers.get(action)
                if handler is None:
                    raise ValueError("Unsupported thread action")
                await handler()
        except Exception:
            # Never claim an RPC failed safely: a request may have committed before timing out.
            # Replace the exact originating card with a neutral, controls-free status if possible.
            try:
                if state.get("message_id"):
                    state["view"] = "error"
                    state["pending_topic"] = None
                    state["confirmation"] = None
                    await self._send_or_edit(state, source, event=event)
                else:
                    await self._send_message(source, "Action status is unknown; check Telegram before retrying.")
            except Exception:
                _LOG.exception("Could not replace the Telegram thread card after an action error")
                raise
        return True
