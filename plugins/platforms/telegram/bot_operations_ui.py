"""Owner-operated managed-bot and bot-to-bot Telegram RichMessage UI.

Two standalone menus share this UI and its card/confirmation machinery:

* ``/managedbots`` — bots created through this manager bot: a roster home with
  Create and Manage, a create page, a per-bot manage menu, and access settings.
* ``/b2b`` — bot-to-bot communication: approved bot peers, the owner inbox, and
  adding peers. ``/botpeers`` opens the same page.

Copy stays plain: no function or method names, no hype. Bot details use
indented lines with copy pills; hints and page indexation are italic.
"""
from __future__ import annotations

import html
import logging
import math
import secrets
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from .bot_operations import (
    TelegramBotOperations,
    _generate_username_slug,
    _redact_external_text,
)
from .mtproto_rich import edit_request, inline_markup, rich_button, rich_button_row

logger = logging.getLogger(__name__)

_PAGE_SIZE = 6
_CONFIRM_TTL = 300
_CARD_TTL = 900
# Views re-rendered when Telegram reports a new or updated managed child.
_MANAGED_VIEWS = frozenset({"managed", "manage", "create"})


def _code(value: Any) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _footer(page: int, pages: int, total: int) -> str:
    return f"Page {page + 1} of {pages} • total {total}"


def _indent(text: str) -> str:
    return f"&nbsp;&nbsp;&nbsp;&nbsp;{text}"


def _copy_pill(label: str, value: str) -> str:
    return rich_button(label, kind="copy", value=value, style="success")


def _observed(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%d %b %Y")
    except (TypeError, ValueError, OverflowError):
        return "unknown"


class TelegramBotOperationsUI:
    """The manager bot's explicit owner UI and request/source API."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self.operations = TelegramBotOperations(adapter)
        self._cards: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}

    def _authorized(self, source: Any) -> bool:
        return self.operations._is_owner(source)

    def _prune(self) -> None:
        now = time.monotonic()
        for key, state in tuple(self._cards.items()):
            if now - state["created_at"] > _CARD_TTL:
                self._cards.pop(key, None)
        for key, item in tuple(self._tokens.items()):
            if now > item["expires_at"] or item["card_id"] not in self._cards:
                self._tokens.pop(key, None)
        while len(self._cards) >= 128:
            self._cards.pop(next(iter(self._cards)))
        while len(self._tokens) >= 2048:
            self._tokens.pop(next(iter(self._tokens)))

    def _new_card(self, source: Any, view: str, *, page: int = 0) -> dict[str, Any]:
        self._prune()
        card_id = secrets.token_hex(8)
        state = {
            "id": card_id,
            "view": view,
            "page": max(0, int(page)),
            "user_id": str(source.user_id),
            "chat_id": str(source.chat_id),
            "chat_type": str(source.chat_type),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "profile": getattr(source, "profile", None),
            "profile_home": self.operations.profile_home(source),
            "message_id": None,
            "created_at": time.monotonic(),
            "pending": None,
            "notice": "",
            "return_view": None,
            "create_username": None,
            "create_name": None,
        }
        self._cards[card_id] = state
        return state

    def _token(self, state: dict[str, Any], action: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]
        self._tokens[token] = {"card_id": state["id"], "action": action,
                               "expires_at": time.monotonic() + _CONFIRM_TTL}
        return f"tgb:{token}"

    def _button(self, state: dict[str, Any], label: str, action: dict[str, Any], style: str | None = None):
        return (label, self._token(state, action), style)

    def _nav_button(self, state: dict[str, Any], label: str, view: str, *, page: int = 0):
        return self._button(state, label, {"kind": "view", "view": view, "page": page})

    async def _send_card(self, source: Any, state: dict[str, Any], text: str, markup: Any,
                         reply_to: Any = None) -> None:
        if state.get("message_id") is None:
            receipt = await self.adapter._send_rich_ui(source, text, markup, reply_to=reply_to)
            message_id = getattr(receipt, "id", None)
            if message_id is None:
                raise RuntimeError("Telegram did not return a message ID for the operation card")
            state["message_id"] = str(message_id)
            return
        if self.adapter._client is None:
            raise RuntimeError("Telegram MTProto client is disconnected")
        peer = await self.adapter._input_peer_for(source.chat_id)
        await self.adapter._mtproto_rich_request(edit_request(
            peer, int(state["message_id"]), text, reply_markup=markup, rich_format="html"))

    async def _display_result(self, source: Any, title: str, result: dict[str, Any],
                              *, reply_to: Any = None) -> None:
        state = self._new_card(source, "result")
        if result.get("ok"):
            body = html.escape(str(result.get("message", "Operation completed.")))
            if result.get("confirmation_required"):
                body = html.escape(str(result.get("message", "This exact change needs your confirmation.")))
                state["pending"] = {key: value for key, value in result.items()
                                    if key not in {"ok", "confirmation_required", "message"}}
                state["confirm_id"] = secrets.token_hex(10)
                state["confirm_expires"] = time.monotonic() + _CONFIRM_TTL
                if state["pending"].get("operation") == "peer.send":
                    body += f"<pre>{html.escape(str(state['pending'].get('text', '')))}</pre>"
                rows = [[self._button(state, "Confirm this change", {
                            "kind": "confirm", "confirm_id": state["confirm_id"]}, "success"),
                         self._button(state, "Cancel", {
                            "kind": "cancel", "confirm_id": state["confirm_id"]}, "danger")]]
            else:
                payload = result.get("messages", result.get("bots", result.get("peers", result)))
                body = f"<pre>{html.escape(str(payload)[:3000])}</pre>"
                rows = []
        else:
            body = f"<pre><code>{html.escape(str(result.get('error', 'The request failed.'))[:1200])}</code></pre>"
            rows = []
        rows.append([self._nav_button(state, "Managed bots", "managed"),
                     self._nav_button(state, "Bot-to-bot", "b2b")])
        text = f"<h1>{html.escape(title)}</h1><hr><p>{body}</p><hr><p><i>{_footer(0, 1, 1)}</i></p>"
        await self._send_card(source, state, text, inline_markup(rows), reply_to=reply_to)

    async def request(self, source: Any, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Owner source API for parent tool actions; every mutation stages a UI confirmation."""
        if not self._authorized(source):
            return {"ok": False, "error": "Owner authorization is required"}
        normalized = str(action).strip()
        aliases = {"bot.managed.list": "managed.list", "bot.managed.access.get": "managed.access.get",
                   "bot.managed.access.set": "managed.access.set", "bot.peers.list": "peers.list",
                   "bot.inbox.read": "inbox.read", "bot.peer.add": "peer.add",
                   "bot.peer.remove": "peer.remove", "bot.peer.send": "peer.send",
                   "bot.peer.set_permissions": "peer.set_permissions"}
        normalized = aliases.get(normalized, normalized)
        result = await self.operations.request(source, normalized, **dict(params or {}))
        if result.get("confirmation_required"):
            result["message"] = self._confirmation_summary(result)
            state = self._new_card(source, "confirm")
            state["pending"] = {key: value for key, value in result.items()
                                if key not in {"ok", "confirmation_required", "message"}}
            state["confirm_id"] = secrets.token_hex(10)
            state["confirm_expires"] = time.monotonic() + _CONFIRM_TTL
            await self._present_confirmation(source, state)
        elif result.get("operation") == "managed.create_link":
            state = self._new_card(source, "create")
            state["create_username"] = str(params.get("username")) if params and params.get("username") else None
            state["create_name"] = str(params["name"]) if params and params.get("name") else None
            await self._present_view(source, state)
        return result

    @staticmethod
    def _confirmation_summary(result: dict[str, Any]) -> str:
        operation = result.get("operation")
        if operation == "peer.add":
            bot = result["bot"]
            scopes = []
            if result.get("group_enabled"):
                scopes.append(f"this group only ({result.get('group_chat_id')})")
            if result.get("dm_enabled"):
                scopes.append("direct messages")
            return f"Add @{bot.get('username') or bot['id']} (verified ID {bot['id']}) for {', '.join(scopes)}?"
        if operation == "peer.remove":
            return f"Remove bot {result.get('bot_id')} from this profile's peer allowlist?"
        if operation == "peer.set_permissions":
            if result.get("group_chat_id") is None:
                return f"Leave all group grants unchanged and set DM permission to {result.get('dm_enabled')}?"
            return (f"Set group {result.get('group_chat_id')} permission to "
                    f"{result.get('group_enabled')} and DM permission to {result.get('dm_enabled')}?")
        if operation == "peer.send":
            return (f"Send one exact message as this manager bot to peer {result.get('bot_id')} "
                    f"in chat {result.get('chat_id')}, topic {result.get('thread_id') or 'none'}? "
                    "The full final message text is shown below.")
        if operation == "managed.access.set":
            return (f"Set managed bot {result.get('bot_id')} restricted={result.get('restricted')} "
                    f"with explicit additional user IDs {result.get('add_users')}?")
        return "Confirm this exact operation?"

    # -- Views: each returns (blocks, keyboard rows, total, pages) ------------

    async def _view_managed(self, source: Any, state: dict[str, Any]):
        result = await self.operations.request(source, "managed.list")
        bots = result.get("bots", []) if result.get("ok") else []
        blocks = ["<h1>Managed bots</h1>",
                  "<p>Bots created through this manager bot. Telegram reports each new or "
                  "updated child here; the list below is what this profile has seen.</p>"]
        page = max(0, int(state.get("page", 0)))
        total = len(bots)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for bot in bots[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            username = str(bot.get("username") or "")
            label = html.escape(str(bot.get("name") or username or bot.get("id")))
            blocks.append(f"<p>• <b>{label}</b></p>")
            if username:
                blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
        if not bots:
            blocks.append("<p>No managed bots in this profile yet.</p>")
            blocks.append("<p><i>Create one below. When Telegram confirms it, it appears here on its own.</i></p>")
        rows = [[self._nav_button(state, "Create", "create"),
                 self._nav_button(state, "Manage", "manage")]]
        return blocks, rows, total, pages

    async def _view_create(self, source: Any, state: dict[str, Any]):
        if not state.get("create_username"):
            state["create_username"] = f"hermes_{_generate_username_slug()}_bot"
        username = str(state["create_username"])
        name = state.get("create_name")
        result = await self.operations.request(source, "managed.list")
        bots = result.get("bots", []) if result.get("ok") else []
        created = next((bot for bot in bots
                        if str(bot.get("username") or "").lower() == username.lower()), None)
        blocks = ["<h1>Create a managed bot</h1>"]
        if created is not None:
            label = html.escape(str(created.get("name") or username or created.get("id")))
            blocks.append("<p>This bot is now in the roster.</p>")
            blocks.append(f"<p>• <b>{label}</b></p>")
            blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
            blocks.append(f"<p>{_indent('id: ' + _copy_pill(str(created.get('id')), str(created.get('id'))))}</p>")
            rows = [[self._nav_button(state, "Manage", "manage"),
                     self._nav_button(state, "Managed bots", "managed")]]
            return blocks, rows, 1, 1
        blocks.append("<p>Creation happens in Telegram. The link below opens it for this "
                      "manager bot with a suggested username.</p>")
        link = await self.operations.request(source, "managed.create_link",
                                             username=username, name=name)
        if not link.get("ok"):
            blocks.append(f"<p>{html.escape(str(link.get('error', 'The creation link is unavailable.')))}</p>")
        else:
            blocks.append(rich_button("Open Telegram bot creation", kind="url", value=str(link["url"])))
            blocks.append(f"<p>{_indent('suggested username: ' + _copy_pill('@' + username, '@' + username))}</p>")
            if name:
                blocks.append(f"<p>{_indent('suggested name: ' + html.escape(str(name)))}</p>")
            blocks.append("<p><i>When Telegram confirms the new bot, this manager bot is "
                          "notified and the list updates on its own. Nothing else to do here.</i></p>")
            blocks.append("<p><i>To choose the username yourself: /managedbots create "
                          "&lt;username&gt; [name]</i></p>")
        rows = [[self._nav_button(state, "Managed bots", "managed")]]
        return blocks, rows, 1, 1

    async def _view_manage(self, source: Any, state: dict[str, Any]):
        result = await self.operations.request(source, "managed.list")
        bots = result.get("bots", []) if result.get("ok") else []
        blocks = ["<h1>Manage managed bots</h1>",
                  "<p>Details for each bot in this profile's roster.</p>"]
        page = max(0, int(state.get("page", 0)))
        total = len(bots)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for bot in bots[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            bot_id = str(bot.get("id"))
            username = str(bot.get("username") or "")
            label = html.escape(str(bot.get("name") or username or bot_id))
            blocks.append(f"<p>• <b>{label}</b></p>")
            if username:
                blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
            blocks.append(f"<p>{_indent('id: ' + _copy_pill(bot_id, bot_id))}</p>")
            blocks.append(f"<p>{_indent('observed: ' + html.escape(_observed(bot.get('observed_at'))))}</p>")
            seen = await self.operations.read_inbox(source, bot_id=bot_id, limit=50)
            seen_count = len(seen.get("messages", [])) if seen.get("ok") else 0
            blocks.append(f"<p>{_indent('messages: ' + str(seen_count))}</p>")
            actions = [rich_button("Access settings", kind="callback",
                                   value=self._token(state, {"kind": "access", "bot_id": bot_id}),
                                   style="primary")]
            if username:
                actions.append(rich_button("Open in Telegram", kind="url",
                                           value=f"https://t.me/{username}"))
            actions.append(rich_button("Messages", kind="callback",
                                       value=self._token(state, {"kind": "view",
                                                               "view": f"inbox:{bot_id}", "page": 0})))
            blocks.append(rich_button_row(actions))
        if not bots:
            blocks.append("<p>No managed bots in this profile yet.</p>")
            blocks.append("<p><i>Create one first; management appears here once Telegram reports it.</i></p>")
        rows = [[self._nav_button(state, "Managed bots", "managed"),
                 self._nav_button(state, "Refresh", "manage")]]
        return blocks, rows, total, pages

    async def _view_access(self, source: Any, state: dict[str, Any], bot_id: str):
        result = await self.operations.request(source, "managed.access.get", bot_id=bot_id)
        if not result.get("ok"):
            blocks = ["<h1>Access settings</h1>",
                      f"<p>{html.escape(str(result.get('error')))}</p>"]
            rows = [[self._nav_button(state, "Manage", "manage")]]
            return blocks, rows, 0, 1
        roster = await self.operations.request(source, "managed.list")
        bots = roster.get("bots", []) if roster.get("ok") else []
        entry = next((bot for bot in bots if str(bot.get("id")) == str(bot_id)), {})
        username = str(entry.get("username") or "")
        label = html.escape(str(entry.get("name") or username or bot_id))
        users = result.get("add_users", [])
        blocks = ["<h1>Access settings</h1>",
                  f"<p>• <b>{label}</b></p>"]
        if username:
            blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
        blocks.append(f"<p>{_indent('id: ' + _copy_pill(str(bot_id), str(bot_id)))}</p>")
        blocks.append(f"<p>{_indent('restricted: <b>' + str(bool(result.get('restricted'))).lower() + '</b>')}</p>")
        if users:
            pills = " ".join(_copy_pill(str(user["id"]), str(user["id"])) for user in users)
            blocks.append(f"<p>{_indent('additional users: ' + pills)}</p>")
        else:
            blocks.append(f"<p>{_indent('additional users: none')}</p>")
        blocks.append("<p><i>Changes apply after confirmation. At most 10 additional users.</i></p>")
        rows = [[self._button(state, "Disable restriction" if result.get("restricted") else "Restrict access",
                    {"kind": "access_toggle", "bot_id": bot_id, "restricted": not result.get("restricted"),
                     "add_users": [] if result.get("restricted") else [int(user["id"]) for user in users]},
                    "danger" if not result.get("restricted") else "success")],
                [self._nav_button(state, "Manage", "manage")]]
        return blocks, rows, 1 + len(users), 1

    async def _view_b2b(self, source: Any, state: dict[str, Any]):
        peers = await self.operations.request(source, "peers.list")
        peer_list = peers.get("peers", []) if peers.get("ok") else []
        inbox = await self.operations.request(source, "inbox.read", limit=50)
        messages = inbox.get("messages", []) if inbox.get("ok") else []
        blocks = ["<h1>Bot-to-bot</h1>",
                  "<p>Bots this profile talks to, and what they send. Messages from approved "
                  "bots are kept in the inbox for review. They never start a reply on their own.</p>",
                  f"<p>• approved peers: <b>{len(peer_list)}</b></p>",
                  f"<p>• inbox messages: <b>{len(messages)}</b></p>"]
        rows = [[self._nav_button(state, "Peers", "peers"),
                 self._nav_button(state, "Inbox", "inbox"),
                 self._nav_button(state, "Add peer", "add_peer")]]
        return blocks, rows, 1, 1

    async def _view_peers(self, source: Any, state: dict[str, Any]):
        result = await self.operations.request(source, "peers.list")
        peers = result.get("peers", []) if result.get("ok") else []
        blocks = ["<h1>Approved bot peers</h1>",
                  "<p>Each peer is allowed only in the chats listed for it. Group access covers "
                  "one named group; it never enables direct messages.</p>"]
        page = max(0, int(state.get("page", 0)))
        total = len(peers)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for peer in peers[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            peer_id = str(peer.get("id"))
            username = str(peer.get("username") or "")
            label = html.escape(str(peer.get("name") or username or peer_id))
            groups = peer.get("group_chat_ids", [])
            blocks.append(f"<p>• <b>{label}</b></p>")
            if username:
                blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
            blocks.append(f"<p>{_indent('id: ' + _copy_pill(peer_id, peer_id))}</p>")
            blocks.append(f"<p>{_indent('direct messages: <b>' + str(bool(peer.get('dm_enabled'))).lower() + '</b>')}</p>")
            if groups:
                pills = " ".join(_copy_pill(str(item), str(item)) for item in groups)
                blocks.append(f"<p>{_indent('groups: ' + pills)}</p>")
            else:
                blocks.append(f"<p>{_indent('groups: none')}</p>")
            current_group = str(source.chat_id) if str(source.chat_type) in {"group", "forum"} else None
            dm_params = {"bot_id": peer_id,
                         "group_enabled": current_group in set(map(str, groups)) if current_group else False,
                         "dm_enabled": not bool(peer.get("dm_enabled"))}
            if current_group:
                dm_params["group_chat_id"] = current_group
            actions = [rich_button("Disable DM" if peer.get("dm_enabled") else "Enable DM",
                                   kind="callback",
                                   value=self._token(state, {"kind": "request", "action": "peer.set_permissions",
                                                           "params": dm_params}),
                                   style="danger" if peer.get("dm_enabled") else "success"),
                       rich_button("Send", kind="callback",
                                   value=self._token(state, {"kind": "view", "view": f"send:{peer_id}", "page": 0})),
                       rich_button("Remove", kind="callback",
                                   value=self._token(state, {"kind": "request", "action": "peer.remove",
                                                           "params": {"bot_id": peer_id}}),
                                   style="danger")]
            blocks.append(rich_button_row(actions))
            if str(getattr(source, "chat_type", "")) in {"group", "forum"}:
                current_chat = str(source.chat_id)
                enabled = current_chat in set(map(str, groups))
                blocks.append(rich_button_row([rich_button(
                    "Disable group here" if enabled else "Enable group here",
                    kind="callback",
                    value=self._token(state, {"kind": "request", "action": "peer.set_permissions",
                                             "params": {"bot_id": peer_id, "group_enabled": not enabled,
                                                        "dm_enabled": bool(peer.get("dm_enabled")),
                                                        "group_chat_id": current_chat}}),
                    style="danger" if enabled else "success")]))
        if not peers:
            blocks.append("<p>No bot peers are approved in this profile.</p>")
            blocks.append("<p><i>Add one below. Approval needs your confirmation.</i></p>")
        rows = [[self._nav_button(state, "Bot-to-bot", "b2b"),
                 self._nav_button(state, "Add peer", "add_peer")]]
        return blocks, rows, total, pages

    async def _view_inbox(self, source: Any, state: dict[str, Any], bot_id: str | None = None):
        params: dict[str, Any] = {"limit": 50}
        if bot_id is not None:
            params["bot_id"] = bot_id
        result = await self.operations.request(source, "inbox.read", **params)
        messages = result.get("messages", []) if result.get("ok") else []
        blocks = ["<h1>Bot inbox</h1>",
                  "<p>Approved bot messages are stored for owner review only. They never start a model turn.</p>"]
        page = max(0, int(state.get("page", 0)))
        total = len(messages)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for message in messages[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            bot = message.get("bot_username") or str(message.get("bot_id"))
            label = html.escape(str(message.get("bot_name") or bot))
            blocks.append(f"<p>• <b>{label}</b></p>")
            if message.get("bot_username"):
                blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + str(bot), '@' + str(bot)))}</p>")
            blocks.append(f"<p>{_indent('chat: ' + _copy_pill(str(message.get('chat_id')), str(message.get('chat_id'))))}</p>")
            blocks.append(f"<p>{_indent('message: ' + _copy_pill(str(message.get('message_id')), str(message.get('message_id'))))}</p>")
            blocks.append(f"<pre>{html.escape(_redact_external_text(message.get('text', ''))[:1200])}</pre>")
            blocks.append(rich_button_row([rich_button(
                "Send", kind="callback",
                value=self._token(state, {"kind": "view", "view": f"send:{message.get('bot_id')}", "page": 0}))]))
        if not messages:
            blocks.append("<p>The inbox is empty.</p>")
        rows = [[self._nav_button(state, "Bot-to-bot", "b2b")]]
        if bot_id is not None:
            rows[0].append(self._nav_button(state, "Manage", "manage"))
        return blocks, rows, total, pages

    async def _view_add_peer(self, source: Any, state: dict[str, Any]):
        blocks = ["<h1>Add a bot peer</h1>",
                  "<p>An approved peer may message this profile in the chats you allow. "
                  "Its messages go to the inbox for review; they never start a reply on their own.</p>",
                  "<p>• bot: the @username or numeric id of the bot</p>",
                  "<p>• scope: group, dm, or both — group covers only the group this command runs in</p>",
                  rich_button("Copy the add command", kind="copy",
                              value="/b2b add <bot> <group|dm|both>", style="success"),
                  "<p><i>Example: /b2b add @example_bot both</i></p>",
                  "<p><i>Adding needs your confirmation on the next card.</i></p>"]
        rows = [[self._nav_button(state, "Bot-to-bot", "b2b"),
                 self._nav_button(state, "Peers", "peers")]]
        return blocks, rows, 1, 1

    async def _view_send(self, source: Any, state: dict[str, Any], bot_id: str):
        result = await self.operations.request(source, "peers.list")
        peers = result.get("peers", []) if result.get("ok") else []
        entry = next((peer for peer in peers if str(peer.get("id")) == str(bot_id)), None)
        if entry is None:
            blocks = ["<h1>Send to a bot peer</h1>",
                      "<p>This bot is not on this profile's allowlist.</p>"]
            rows = [[self._nav_button(state, "Bot-to-bot", "b2b"),
                     self._nav_button(state, "Peers", "peers")]]
            return blocks, rows, 0, 1
        username = str(entry.get("username") or "")
        label = html.escape(str(entry.get("name") or username or bot_id))
        blocks = ["<h1>Send to a bot peer</h1>",
                  f"<p>• <b>{label}</b></p>"]
        if username:
            blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
        blocks.append(f"<p>{_indent('id: ' + _copy_pill(str(bot_id), str(bot_id)))}</p>")
        blocks.append("<p>• scope: dm, or group for a group this peer is approved in</p>")
        blocks.append(rich_button("Copy the send command", kind="copy",
                                  value=f"/b2b send {bot_id} dm ", style="success"))
        blocks.append("<p><i>The full message is shown for confirmation before anything is sent.</i></p>")
        rows = [[self._nav_button(state, "Bot-to-bot", "b2b"),
                 self._nav_button(state, "Peers", "peers")]]
        return blocks, rows, 1, 1

    async def _present_view(self, source: Any, state: dict[str, Any], *, reply_to: Any = None) -> None:
        view = state["view"]
        page = max(0, int(state.get("page", 0)))
        if view == "managed":
            blocks, rows, total, pages = await self._view_managed(source, state)
        elif view == "create":
            blocks, rows, total, pages = await self._view_create(source, state)
        elif view == "manage":
            blocks, rows, total, pages = await self._view_manage(source, state)
        elif view.startswith("access:"):
            blocks, rows, total, pages = await self._view_access(source, state, view.split(":", 1)[1])
        elif view == "b2b":
            blocks, rows, total, pages = await self._view_b2b(source, state)
        elif view == "peers":
            blocks, rows, total, pages = await self._view_peers(source, state)
        elif view == "inbox":
            blocks, rows, total, pages = await self._view_inbox(source, state)
        elif view.startswith("inbox:"):
            blocks, rows, total, pages = await self._view_inbox(source, state, view.split(":", 1)[1])
        elif view == "add_peer":
            blocks, rows, total, pages = await self._view_add_peer(source, state)
        elif view.startswith("send:"):
            blocks, rows, total, pages = await self._view_send(source, state, view.split(":", 1)[1])
        else:
            blocks, rows, total, pages = await self._view_managed(source, state)
        pages = max(1, pages)
        total = max(0, total)
        page = max(0, min(page, pages - 1))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(self._button(state, f"Previous page {page}", {"kind": "page", "page": page - 1}))
            if page + 1 < pages:
                nav.append(self._button(state, f"Next page {page + 2}", {"kind": "page", "page": page + 1}))
            if nav:
                rows.append(nav)
        if state.get("notice"):
            notice = html.escape(str(state["notice"]))
            blocks.append(f"<pre><code>{notice}</code></pre>" if str(state["notice"]).startswith("Error:")
                          else f"<p>{notice}</p>")
        text = "".join(blocks) + f"<hr><p><i>{_footer(page, pages, total)}</i></p>"
        state["page"] = page
        await self._send_card(source, state, text, inline_markup(rows), reply_to=reply_to)

    async def open(self, source: Any, view: str, *, page: int = 0, reply_to: Any = None,
                   create_username: str | None = None, create_name: str | None = None) -> bool:
        if not self._authorized(source):
            await self.adapter._send_rich_ui(source,
                "<h1>Owner only</h1><hr><p>These bot-management controls are available only to the profile owner.</p>",
                None, reply_to=reply_to)
            return True
        state = self._new_card(source, view, page=page)
        state["create_username"] = create_username
        state["create_name"] = create_name
        await self._present_view(source, state, reply_to=reply_to)
        return True

    async def _stage(self, source: Any, action: str, params: dict[str, Any], *, state: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.operations.request(source, action, **params)
        if not result.get("ok"):
            if state is not None:
                state["notice"] = f"Error: {html.escape(str(result.get('error', 'request failed'))[:600])}"
                await self._present_view(source, state)
            else:
                await self._display_result(source, "Bot operation failed", result)
            return result
        if result.get("confirmation_required"):
            card = state or self._new_card(source, "confirm")
            card["pending"] = {key: value for key, value in result.items()
                               if key not in {"ok", "confirmation_required"}}
            card["view"] = "confirm"
            card["return_view"] = (state.get("view") if state is not None else None) or "managed"
            card["confirm_id"] = secrets.token_hex(10)
            card["confirm_expires"] = time.monotonic() + _CONFIRM_TTL
            await self._present_confirmation(source, card)
        elif state is not None:
            state["notice"] = "Operation complete."
            await self._present_view(source, state)
        else:
            await self._display_result(source, "Bot operation", result)
        return result

    async def _present_confirmation(self, source: Any, state: dict[str, Any]) -> None:
        pending = state.get("pending") or {}
        summary = html.escape(self._confirmation_summary(pending))
        detail = (f"<pre>{html.escape(str(pending.get('text', '')))}</pre>"
                  if pending.get("operation") == "peer.send" else "")
        buttons = [[self._button(state, "Confirm one change", {"kind": "confirm", "confirm_id": state.get("confirm_id")}, "success"),
                    self._button(state, "Cancel", {"kind": "cancel", "confirm_id": state.get("confirm_id")}, "danger")]]
        text = (f"<h1>Owner confirmation required</h1><hr><p>{summary}</p>{detail}"
                f"<p>Owner {_code(state['user_id'])} · chat {_code(state['chat_id'])} · "
                f"topic {_code(state['thread_id'] or 'none')} · profile {_code(state['profile_home'])}</p>"
                f"<p>This request expires in five minutes and is bound to this profile and chat.</p>"
                f"<hr><p><i>{_footer(0, 1, 1)}</i></p>")
        await self._send_card(source, state, text, inline_markup(buttons))

    async def refresh_managed_cards(self, profile_home: str, bot_id: int | None = None) -> int:
        """Re-render open managed cards for one profile after Telegram reports a child.

        The observer has no turn source, so each card rebuilds one from its stored
        owner/chat/profile and re-resolves the profile home; a card whose home no
        longer matches is skipped, never edited into the wrong profile.
        """
        refreshed = 0
        for state in list(self._cards.values()):
            if state.get("message_id") is None or state.get("view") not in _MANAGED_VIEWS:
                continue
            if str(state.get("profile_home") or "") != str(profile_home):
                continue
            source = SimpleNamespace(
                home=state["profile_home"], user_id=state["user_id"], chat_id=state["chat_id"],
                chat_type=state["chat_type"], thread_id=state["thread_id"] or None,
                is_bot=False, profile=state.get("profile"), username="", display_name="")
            try:
                if str(self.operations.profile_home(source)) != str(state["profile_home"]):
                    continue
            except Exception:
                continue
            try:
                await self._present_view(source, state)
                refreshed += 1
            except Exception:
                logger.warning("Managed-bot card refresh failed for card %s", state.get("id"))
        return refreshed

    async def handle_callback(self, event: Any, source: Any, data: Any) -> bool:
        """Handle only a fresh owner callback bound to the exact card, chat and profile."""
        raw = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data or "")
        if not raw.startswith("tgb:"):
            return False
        self._prune()
        token = raw.split(":", 1)[-1]
        entry = self._tokens.pop(token, None)
        state = self._cards.get(entry["card_id"]) if entry else None
        query = getattr(event, "query", None)
        message_id = getattr(query, "msg_id", None) or getattr(event, "message_id", None)
        if (state and not getattr(source, "thread_id", None) and state["thread_id"]
                and str(message_id) == str(state.get("message_id"))
                and str(source.user_id) == state["user_id"] and str(source.chat_id) == state["chat_id"]):
            # Callback updates commonly omit the topic; only the exact native card
            # may restore it, before re-resolving owner/profile authorization.
            import dataclasses
            if dataclasses.is_dataclass(source):
                from gateway.session_identity import replace_source
                source = replace_source(source, thread_id=state["thread_id"])
            else:
                import copy
                source = copy.copy(source)
                source.thread_id = state["thread_id"]
        if not self._authorized(source):
            await event.answer("Owner authorization required", alert=True)
            return True
        message_id = message_id or getattr(getattr(query, "message", None), "id", None)
        actor_id = (getattr(query, "user_id", None) or getattr(event, "sender_id", None)
                    or getattr(getattr(query, "sender", None), "id", None))
        try:
            current_profile = self.operations.profile_home(source)
        except Exception:
            current_profile = None
        if (not state or state["user_id"] != str(source.user_id)
                or state["chat_id"] != str(source.chat_id)
                or state["thread_id"] != str(getattr(source, "thread_id", "") or "")
                or state["profile_home"] != current_profile
                or (actor_id is not None and str(actor_id) != str(source.user_id))
                or message_id is None or state.get("message_id") is None
                or str(message_id) != str(state["message_id"])):
            await event.answer("This control expired or belongs to another chat", alert=True)
            return True
        action = entry["action"]
        kind = action.get("kind")
        try:
            if kind == "page":
                state["page"] = action["page"]
                state["notice"] = ""
                await self._present_view(source, state)
            elif kind == "view":
                target = action["view"]
                if target == "create" and state["view"] != "create":
                    # Fresh visit, not a re-render: suggest a new username.
                    state["create_username"] = None
                    state["create_name"] = None
                state["view"], state["page"], state["pending"] = target, action.get("page", 0), None
                state["notice"] = ""
                await self._present_view(source, state)
            elif kind == "access":
                state["view"], state["page"] = f"access:{action['bot_id']}", 0
                await self._present_view(source, state)
            elif kind == "access_toggle":
                await self._stage(source, "managed.access.set", {
                    "bot_id": action["bot_id"], "restricted": action["restricted"],
                    "add_users": action["add_users"]}, state=state)
            elif kind == "request":
                await self._stage(source, action["action"], action.get("params", {}), state=state)
            elif kind == "confirm":
                pending = state.get("pending")
                if not pending or action.get("confirm_id") != state.get("confirm_id"):
                    await event.answer("No pending action", alert=True)
                    return True
                expires = state.get("confirm_expires", 0)
                if time.monotonic() > expires:
                    state["pending"] = None
                    state["notice"] = "Confirmation expired; nothing was changed."
                    await self._present_view(source, state)
                else:
                    operation = pending.get("operation")
                    payload = {key: value for key, value in pending.items() if key not in {"operation", "message"}}
                    state["pending"] = None
                    state["confirm_id"] = None  # consume before the first network await
                    result = await self.operations.confirm(source, operation, **payload)
                    state["view"] = state.get("return_view") or "managed"
                    state["notice"] = ("Change applied." if result.get("ok") else
                                        "Error: " + html.escape(str(result.get("error", "request failed"))[:500]))
                    await self._present_view(source, state)
            elif kind == "cancel":
                if action.get("confirm_id") != state.get("confirm_id"):
                    await event.answer("This confirmation is no longer current", alert=True)
                    return True
                state["pending"] = None
                state["confirm_id"] = None
                state["view"] = state.get("return_view") or "managed"
                state["notice"] = "Cancelled; no change was made."
                await self._present_view(source, state)
            else:
                await event.answer("Unknown bot control", alert=True)
                return True
        except Exception:
            state["notice"] = "Error: Telegram operation failed; check Telegram before retrying."
            state["pending"] = None
            await self._present_view(source, state)
        await event.answer("Updated")
        return True

    async def handle_command(self, message: Any, source: Any, command: str,
                             arguments: str = "", reply_to: Any = None) -> bool:
        """Route /managedbots, /b2b and /botpeers native commands, returning whether handled."""
        name = str(command or "").lstrip("/").split("@", 1)[0].lower()
        args = str(arguments or "").split()
        if name == "managedbots":
            if not args:
                return await self.open(source, "managed", reply_to=reply_to)
            if args[0].lower() == "create" and len(args) >= 2:
                return await self.open(source, "create", reply_to=reply_to,
                                       create_username=args[1],
                                       create_name=" ".join(args[2:]) if len(args) >= 3 else None)
            if args[0].lower() == "access" and len(args) == 2:
                result = await self.operations.request(source, "managed.access.get", bot_id=args[1])
                if result.get("ok"):
                    return await self.open(source, f"access:{args[1]}", reply_to=reply_to)
                await self._display_result(source, "Managed-bot access", result, reply_to=reply_to)
                return True
            if args[0].lower() in {"restrict", "unrestrict"} and len(args) >= 2:
                current = await self.operations.request(source, "managed.access.get", bot_id=args[1])
                if not current.get("ok"):
                    await self._display_result(source, "Managed-bot access", current, reply_to=reply_to)
                    return True
                users = [int(user["id"]) for user in current.get("add_users", [])]
                await self._stage(source, "managed.access.set", {"bot_id": args[1],
                    "restricted": args[0].lower() == "restrict",
                    "add_users": users if args[0].lower() == "restrict" else []})
                return True
            await self._display_result(source, "Managed-bot commands", {"ok": False,
                "error": "Use /managedbots, /managedbots create <username> [name], /managedbots access <bot-id>, /managedbots restrict <bot-id>, or /managedbots unrestrict <bot-id>."}, reply_to=reply_to)
            return True
        if name not in {"b2b", "botpeers"}:
            return False
        if not args:
            return await self.open(source, "b2b", reply_to=reply_to)
        sub = args[0].lower()
        if sub == "peers":
            return await self.open(source, "peers", reply_to=reply_to)
        if sub == "inbox":
            return await self.open(source, "inbox", reply_to=reply_to)
        if sub == "add" and len(args) == 3:
            scope = args[2].lower()
            if scope not in {"group", "dm", "both"}:
                error = {"ok": False, "error": "Use group, dm, or both; group is limited to the current group chat."}
                await self._display_result(source, "Add bot peer", error, reply_to=reply_to)
                return True
            group = scope in {"group", "both"}
            dm = scope in {"dm", "both"}
            if group and str(getattr(source, "chat_type", "")) not in {"group", "forum"}:
                await self._display_result(source, "Add bot peer", {"ok": False,
                    "error": "Run this command in the exact group whose bot access you want to allow."}, reply_to=reply_to)
                return True
            await self._stage(source, "peer.add", {"bot_id": args[1], "group_enabled": group, "dm_enabled": dm})
            return True
        if sub == "remove" and len(args) == 2:
            await self._stage(source, "peer.remove", {"bot_id": args[1]})
            return True
        if sub == "send" and len(args) >= 4:
            bot_id, scope = args[1], args[2].lower()
            if scope == "dm":
                text = " ".join(args[3:])
            elif scope == "group" and len(args) >= 5:
                group_chat_id, text = args[3], " ".join(args[4:])
            else:
                await self._display_result(source, "Send to bot", {"ok": False,
                    "error": "Use /b2b send <bot-id> dm <text> or /b2b send <bot-id> group <chat-id> <text>."}, reply_to=reply_to)
                return True
            params = {"bot_id": bot_id, "scope": scope, "text": text}
            if scope == "group":
                params["group_chat_id"] = group_chat_id
            await self._stage(source, "peer.send", params)
            return True
        await self._display_result(source, "Bot-to-bot commands", {"ok": False,
            "error": "Use /b2b, /b2b peers, /b2b inbox, /b2b add <bot-id> <group|dm|both>, /b2b remove <bot-id>, or /b2b send <bot-id> <dm|group> ..."}, reply_to=reply_to)
        return True

    async def handle_managed_update(self, update: Any, profile_home: str | None = None) -> dict[str, Any]:
        return await self.operations.handle_managed_update(update, profile_home=profile_home)

    async def handle_incoming(self, event: Any, source: Any) -> bool:
        return await self.operations.handle_incoming(event, source)
