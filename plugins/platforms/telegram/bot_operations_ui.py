"""Owner-operated managed-bot and approved-bot-peer Telegram RichMessage UI."""
from __future__ import annotations

import html
import math
import secrets
import time
from typing import Any

from .bot_operations import TelegramBotOperations, _redact_external_text
from .mtproto_rich import edit_request, inline_markup

_PAGE_SIZE = 6
_CONFIRM_TTL = 300
_CARD_TTL = 900


def _code(value: Any) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _footer(page: int, pages: int, total: int) -> str:
    return f"Page {page + 1} of {pages} • total {total}"


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
            "profile_home": self.operations.profile_home(source),
            "message_id": None,
            "created_at": time.monotonic(),
            "pending": None,
            "notice": "",
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
            if result.get("operation") == "managed.create_link":
                url = html.escape(str(result.get("url", "")), quote=True)
                body += f'<p><a href="{url}">Open Telegram bot creation</a></p>'
                body += f"<p>Suggested username: {_code(result.get('username', ''))}</p>"
                rows = []
            elif result.get("confirmation_required"):
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
        rows.append([self._button(state, "Managed bots", {"kind": "view", "view": "managed", "page": 0}),
                     self._button(state, "Bot peers", {"kind": "view", "view": "peers", "page": 0})])
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
            await self._display_result(source, "Create managed bot", result)
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

    async def _present_view(self, source: Any, state: dict[str, Any], *, reply_to: Any = None) -> None:
        view = state["view"]
        page = max(0, int(state.get("page", 0)))
        rows: list[list[tuple[str, str, str | None]]] = []
        blocks: list[str] = []
        if view == "managed":
            result = await self.operations.request(source, "managed.list")
            if not result.get("ok"):
                blocks.append(f"<pre>{html.escape(str(result.get('error')))}</pre>")
                bots = []
            else:
                bots = result.get("bots", [])
            total = len(bots)
            pages = max(1, math.ceil(total / _PAGE_SIZE))
            page = min(page, pages - 1)
            shown = bots[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
            blocks.extend(["<h1>Managed bots</h1>",
                "<p>Telegram sends the manager bot an update when a managed child is created or updated. "
                "This profile roster contains only those verified update events; <code>getAdminedBots</code> "
                "is user-session-only and is not called.</p>"])
            for bot in shown:
                username = str(bot.get("username") or "")
                label = html.escape(str(bot.get("name") or username or bot.get("id")))
                blocks.append(f"<hr><h2>{label}</h2><p>{_code('@' + username if username else 'ID ' + str(bot.get('id')))}</p>")
                rows.append([self._button(state, "Access settings", {"kind": "access", "bot_id": str(bot["id"])}, "primary")])
            if not bots:
                cap = result.get("capability", {})
                blocks.append("<p>No managed child-bot update has been observed in this profile yet.</p>")
                blocks.append(f"<p><i>{html.escape(str(cap.get('reason', 'Roster is unavailable.')))}</i></p>")
            total = len(bots)
        elif view.startswith("access:"):
            bot_id = view.split(":", 1)[1]
            result = await self.operations.request(source, "managed.access.get", bot_id=bot_id)
            if not result.get("ok"):
                blocks.extend(["<h1>Managed-bot access</h1>", f"<pre>{html.escape(str(result.get('error')))}</pre>"])
                total = 0
            else:
                users = result.get("add_users", [])
                blocks.extend(["<h1>Managed-bot access settings</h1>",
                    f"<p>Bot {_code(bot_id)} · restricted: <b>{str(result.get('restricted')).lower()}</b></p>",
                    "<p>Additional access users (max 10): " + (", ".join(_code(user["id"]) for user in users) or "none") + "</p>",
                    "<p>Changes are applied through bots.editAccessSettings only after confirmation.</p>"])
                rows.append([self._button(state, "Disable restriction" if result.get("restricted") else "Restrict access",
                    {"kind": "access_toggle", "bot_id": bot_id, "restricted": not result.get("restricted"),
                     "add_users": [] if result.get("restricted") else [int(user["id"]) for user in users]}, "danger" if not result.get("restricted") else "success")])
                total = 1 + len(users)
            pages = 1
        elif view == "inbox":
            result = await self.operations.request(source, "inbox.read", limit=50)
            messages = result.get("messages", []) if result.get("ok") else []
            total = len(messages)
            pages = max(1, math.ceil(total / _PAGE_SIZE))
            page = min(page, pages - 1)
            blocks.extend(["<h1>Bot inbox</h1>",
                "<p>Approved bot messages are stored for owner review only. They never start a model turn.</p>"])
            for message in messages[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
                bot = message.get("bot_username") or str(message.get("bot_id"))
                blocks.append(f"<hr><h2>{_code('@' + str(bot))}</h2>"
                    f"<p>Chat {_code(message.get('chat_id'))} · message {_code(message.get('message_id'))}</p>"
                    f"<pre>{html.escape(_redact_external_text(message.get('text', ''))[:1200])}</pre>")
            if not messages:
                blocks.append("<p>The inbox is empty.</p>")
        else:
            result = await self.operations.request(source, "peers.list")
            peers = result.get("peers", []) if result.get("ok") else []
            total = len(peers)
            pages = max(1, math.ceil(total / _PAGE_SIZE))
            page = min(page, pages - 1)
            blocks.extend(["<h1>Approved bot peers</h1>",
                "<p>Group access is limited to named group chat IDs; it never enables DM access. "
                "Incoming bot messages only enter the owner inbox and are not dispatched to tools or a model.</p>"])
            for peer in peers[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
                name = peer.get("username") or peer.get("name") or str(peer.get("id"))
                groups = peer.get("group_chat_ids", [])
                blocks.append(f"<hr><h2>{html.escape(str(name))}</h2><p>ID {_code(peer.get('id'))} · "
                              f"DM {str(bool(peer.get('dm_enabled'))).lower()} · groups "
                              f"{', '.join(_code(item) for item in groups) or 'none'}</p>")
                rows.append([self._button(state, "Remove…", {"kind": "request", "action": "peer.remove",
                                      "params": {"bot_id": str(peer["id"])}}, "danger")])
                current_group = str(source.chat_id) if str(source.chat_type) in {"group", "forum"} else None
                dm_params = {"bot_id": str(peer["id"]),
                    "group_enabled": current_group in set(map(str, groups)) if current_group else False,
                    "dm_enabled": not bool(peer.get("dm_enabled"))}
                if current_group:
                    dm_params["group_chat_id"] = current_group
                rows.append([self._button(state, "Disable DM" if peer.get("dm_enabled") else "Enable DM",
                    {"kind": "request", "action": "peer.set_permissions", "params": dm_params},
                    "danger" if peer.get("dm_enabled") else "success")])
                if str(getattr(source, "chat_type", "")) in {"group", "forum"}:
                    current_chat = str(source.chat_id)
                    enabled = current_chat in set(map(str, groups))
                    if enabled or peer.get("dm_enabled"):
                        group_next = not enabled
                        dm_next = bool(peer.get("dm_enabled"))
                        if not group_next and not dm_next:
                            # Retain the peer's safe DM setting when removing the only group scope.
                            dm_next = False
                        rows.append([self._button(state, "Disable group here" if enabled else "Enable group here",
                                {"kind": "request", "action": "peer.set_permissions", "params": {
                                    "bot_id": str(peer["id"]), "group_enabled": group_next,
                                    "dm_enabled": dm_next, "group_chat_id": current_chat}},
                                "danger" if enabled else "success")])
            if not peers:
                blocks.append("<p>No bot peers are approved in this profile.</p>")
        pages = max(1, locals().get("pages", 1))
        total = max(0, locals().get("total", 0))
        page = max(0, min(page, pages - 1))
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(self._button(state, f"Previous page {page}", {"kind": "page", "page": page - 1}))
            if page + 1 < pages:
                nav.append(self._button(state, f"Next page {page + 2}", {"kind": "page", "page": page + 1}))
            if nav:
                rows.append(nav)
        rows.append([self._button(state, "Managed bots", {"kind": "view", "view": "managed", "page": 0}),
                     self._button(state, "Peers", {"kind": "view", "view": "peers", "page": 0}),
                     self._button(state, "Inbox", {"kind": "view", "view": "inbox", "page": 0})])
        if state.get("notice"):
            notice = html.escape(str(state["notice"]))
            blocks.append(f"<pre><code>{notice}</code></pre>" if str(state["notice"]).startswith("Error:") else f"<p>{notice}</p>")
        text = "".join(blocks) + f"<hr><p><i>{_footer(page, pages, total)}</i></p>"
        state["page"] = page
        await self._send_card(source, state, text, inline_markup(rows), reply_to=reply_to)

    async def open(self, source: Any, view: str, *, page: int = 0, reply_to: Any = None) -> bool:
        if not self._authorized(source):
            await self.adapter._send_rich_ui(source,
                "<h1>Owner only</h1><hr><p>These bot-management controls are available only to the profile owner.</p>",
                None, reply_to=reply_to)
            return True
        state = self._new_card(source, view, page=page)
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
                state["view"], state["page"], state["pending"] = action["view"], action.get("page", 0), None
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
                    state["view"] = "confirm-result"
                    state["notice"] = ("Change applied." if result.get("ok") else
                                        "Error: " + html.escape(str(result.get("error", "request failed"))[:500]))
                    await self._present_view(source, state)
            elif kind == "cancel":
                if action.get("confirm_id") != state.get("confirm_id"):
                    await event.answer("This confirmation is no longer current", alert=True)
                    return True
                state["pending"] = None
                state["confirm_id"] = None
                state["view"] = "peers"
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
        """Route /managedbots and /botpeers native commands, returning whether handled."""
        name = str(command or "").lstrip("/").split("@", 1)[0].lower()
        args = str(arguments or "").split()
        if name == "managedbots":
            if not args:
                return await self.open(source, "managed", reply_to=reply_to)
            if args[0].lower() == "create" and len(args) >= 2:
                params = {"username": args[1]}
                if len(args) >= 3:
                    params["name"] = " ".join(args[2:])
                result = await self.operations.request(source, "managed.create_link", **params)
                await self._display_result(source, "Create managed bot", result, reply_to=reply_to)
                return True
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
        if name != "botpeers":
            return False
        if not args:
            return await self.open(source, "peers", reply_to=reply_to)
        sub = args[0].lower()
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
                    "error": "Use /botpeers send <bot-id> dm <text> or /botpeers send <bot-id> group <chat-id> <text>."}, reply_to=reply_to)
                return True
            params = {"bot_id": bot_id, "scope": scope, "text": text}
            if scope == "group":
                params["group_chat_id"] = group_chat_id
            await self._stage(source, "peer.send", params)
            return True
        await self._display_result(source, "Bot peer commands", {"ok": False,
            "error": "Use /botpeers, /botpeers inbox, /botpeers add <bot-id> <group|dm|both>, /botpeers remove <bot-id>, or /botpeers send <bot-id> <dm|group> ..."}, reply_to=reply_to)
        return True

    async def handle_managed_update(self, update: Any, profile_home: str | None = None) -> dict[str, Any]:
        return await self.operations.handle_managed_update(update, profile_home=profile_home)

    async def handle_incoming(self, event: Any, source: Any) -> bool:
        return await self.operations.handle_incoming(event, source)
