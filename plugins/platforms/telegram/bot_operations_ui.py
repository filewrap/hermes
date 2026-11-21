"""Owner-operated managed-bot and bot-to-bot Telegram RichMessage UI.

Two standalone menus share this UI and its card/confirmation machinery, but
never link to each other — each result card offers only its own home:

* ``/managedbots`` — bots created through this manager bot: a roster home with
  Create and Manage, a create page, a per-bot manage menu, and access settings.
* ``/b2b`` — bot-to-bot communication: approved bot peers, the owner inbox, and
  adding peers. ``/botpeers`` opens the same page.

Copy stays plain: no function or method names, no hype. Bot details use
indented lines with copy pills; hints and page indexation are italic. Page
indexation renders only when there is more than one page.
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
    BotOperationsError,
    BotOperationsStore,
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
# Per-peer settings in display order, with the values each control cycles through.
_SETTING_CYCLE: dict[str, tuple[str, ...]] = {
    "intake": ("inbox", "turns"),
    "trigger": ("mention", "word", "direct"),
    "acceptance": ("full", "short"),
    "secure": ("allowed", "blocked"),
    "notify": ("on", "off"),
}
_SETTING_LABELS: tuple[tuple[str, str], ...] = (
    ("intake", "Intake"),
    ("trigger", "Trigger"),
    ("model", "Model"),
    ("acceptance", "Acceptance"),
    ("secure", "Secure talk"),
    ("notify", "Notify owner"),
)


def _code(value: Any) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def _footer(page: int, pages: int, total: int) -> str:
    return f"Page {page + 1} of {pages} • total {total}"


def _indent(text: str) -> str:
    return f"&nbsp;&nbsp;&nbsp;&nbsp;{text}"


def _copy_pill(label: str, value: str) -> str:
    return rich_button(label, kind="copy", value=value, style="success")


def _name_pill(label: str, bot_id: Any) -> str:
    """Bot name as a profile pill (the /start pattern); plain bold text when the id is not numeric."""
    raw = str(bot_id or "")
    text = str(label or raw)
    if raw.isdecimal() and text:
        return rich_button(text, kind="profile", value=raw)
    return f"<b>{html.escape(text)}</b>"


def _access_badge(restricted: bool) -> str:
    """Access state as a badge right after the bot name: restricted or open."""
    if restricted:
        return rich_button("restricted", kind="callback", value="badge:restricted", style="danger")
    return rich_button("open", kind="callback", value="badge:open", style="success")


def _manager_mode_badge(adapter: Any) -> str:
    """Manager-mode state as a badge; creation links need manager mode on."""
    if bool(getattr(adapter, "_bot_can_manage_bots", False)):
        return rich_button("on", kind="callback", value="badge:enabled", style="success")
    return rich_button("off", kind="callback", value="badge:disabled", style="danger")


def _observed(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%d %b %Y")
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def _when(value: Any) -> str:
    """Date and time for a last-exchange timestamp; "never" when the peer has none."""
    if value is None:
        return "never"
    try:
        return datetime.fromtimestamp(int(value)).strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError, OverflowError):
        return "unknown"


def _last_exchange_lines(peer: dict[str, Any], *, limit: int = 200) -> list[str]:
    """Last response and last sent lines for one peer, from its stored fields."""
    lines = []
    if peer.get("last_in_text"):
        text = html.escape(str(peer["last_in_text"])[:limit])
        lines.append(f"<p>{_indent('Last response: ' + text + ' (' + _when(peer.get('last_in_at')) + ')')}</p>")
    else:
        lines.append(f"<p>{_indent('Last response: none yet')}</p>")
    if peer.get("last_out_text"):
        text = html.escape(str(peer["last_out_text"])[:limit])
        lines.append(f"<p>{_indent('Last sent: ' + text + ' (' + _when(peer.get('last_out_at')) + ')')}</p>")
    else:
        lines.append(f"<p>{_indent('Last sent: none yet')}</p>")
    return lines


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
                              *, reply_to: Any = None, surface: str) -> None:
        state = self._new_card(source, "result")
        if result.get("ok"):
            if result.get("message_id") is not None:
                body = html.escape(f"Message sent (id {result['message_id']}) in chat {result.get('chat_id')}.")
            else:
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
        # Result cards offer only their own surface's home — the two menus never link to each other.
        home = "Managed bots" if surface == "managed" else "Bot-to-bot"
        home_view = "managed" if surface == "managed" else "b2b"
        rows.append([self._nav_button(state, home, home_view)])
        text = f"<h1>{html.escape(title)}</h1><hr><p>{body}</p>"
        await self._send_card(source, state, text, inline_markup(rows), reply_to=reply_to)

    async def request(self, source: Any, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Agent-tool source API: peer.send executes immediately for any admitted
        user; every other mutation stages a UI confirmation for the owner."""
        normalized = str(action).strip()
        aliases = {"bot.managed.list": "managed.list", "bot.managed.access.get": "managed.access.get",
                   "bot.managed.access.set": "managed.access.set", "bot.peers.list": "peers.list",
                   "bot.inbox.read": "inbox.read", "bot.peer.add": "peer.add",
                   "bot.peer.remove": "peer.remove", "bot.peer.send": "peer.send",
                   "bot.peer.set_permissions": "peer.set_permissions",
                   "bot.peer.set_settings": "peer.set_settings", "bot.peer.unban": "peer.unban"}
        normalized = aliases.get(normalized, normalized)
        if normalized == "peer.send":
            if not self.operations._is_admitted_sender(source):
                return {"ok": False, "error": "Sending requires an admitted Telegram user"}
        elif not self._authorized(source):
            return {"ok": False, "error": "Owner authorization is required"}
        result = await self.operations.request(source, normalized, **dict(params or {}))
        if normalized == "peer.send" and result.get("confirmation_required"):
            # Guard-free send: no confirmation card is staged; the send runs
            # at once through the existing confirm branch and its result
            # (sent message id or error) is returned to the caller.
            payload = {key: value for key, value in result.items()
                       if key not in {"ok", "confirmation_required", "message", "operation"}}
            return await self.operations.confirm(source, "peer.send", **payload)
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
            if result.get("ban"):
                return f"Remove bot {result.get('bot_id')} from this profile's peer allowlist and ban it so it cannot be added again?"
            return f"Remove bot {result.get('bot_id')} from this profile's peer allowlist?"
        if operation == "peer.unban":
            return f"Unban bot {result.get('bot_id')} so it can be added again?"
        if operation == "peer.set_settings":
            parts = []
            for name, value in (result.get("settings") or {}).items():
                shown = "session model" if name == "model" and value is None else str(value)
                parts.append(f"{name} to {shown}")
            return f"Set {', '.join(parts)} for peer {result.get('bot_id')}?"
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

    # -- Peer rendering helpers (shared by the /b2b hub, the peers list and Manage) --

    @staticmethod
    def _peer_pill(peer: dict[str, Any]) -> str:
        """The peer's name as its own profile pill, as /start builds it."""
        peer_id = str(peer.get("id"))
        label = str(peer.get("name") or peer.get("username") or peer_id)
        if peer_id.isdigit():
            return rich_button(label, kind="profile", value=peer_id)
        return f"<b>{html.escape(label)}</b>"

    @staticmethod
    def _value_badge(value: Any, *, style: str | None = None, toast: str | None = None) -> str:
        """A setting value as a badge: a toast-only ``badge:<type>`` button when
        the value maps to a known toast, else a disabled indicator (no toast
        is ever invented for an unknown value)."""
        if toast is not None:
            return rich_button(str(value), kind="callback", value=f"badge:{toast}", style=style)
        return rich_button(str(value), kind="disabled", style=style)

    @staticmethod
    def _scope_badges(peer: dict[str, Any]) -> list[str]:
        badges = []
        if peer.get("dm_enabled"):
            badges.append(rich_button("DM", kind="disabled", style="success"))
        if peer.get("group_chat_ids"):
            badges.append(rich_button("group", kind="disabled", style="success"))
        return badges

    @staticmethod
    def _test_badge(peer: dict[str, Any]) -> str | None:
        """Reachability badge from the stored add-test outcome; none when never tested."""
        if peer.get("last_test_ok") is True:
            return rich_button("passed", kind="disabled", style="success")
        if peer.get("last_test_ok") is False:
            return rich_button("failed", kind="disabled", style="danger")
        return None

    def _settings_details(self, state: dict[str, Any], peer: dict[str, Any]) -> list[str]:
        """One collapsible Settings section: all six settings, each with a
        value badge and an inline-button control that stages peer.set_settings."""
        peer_id = str(peer.get("id"))
        settings = peer.get("settings") or {}
        blocks = ["<details><summary>Settings</summary>"]
        for name, label in _SETTING_LABELS:
            current = settings.get(name)
            if name == "model":
                display = str(current) if current else "session model"
                badge = self._value_badge(display)
                control_label, next_value = "Use session model", None
            else:
                values = _SETTING_CYCLE[name]
                display = str(current)
                next_value = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
                control_label = f"Switch to {next_value}"
                if name == "notify":
                    on = current == "on"
                    badge = self._value_badge(display, style="success" if on else "danger",
                                              toast="enabled" if on else "disabled")
                elif name == "secure":
                    allowed = current == "allowed"
                    badge = self._value_badge(display, style="success" if allowed else "danger",
                                              toast="enabled" if allowed else "disabled")
                else:
                    badge = self._value_badge(display)
            blocks.append(f"<p>• {label}: {badge}</p>")
            token = self._token(state, {"kind": "request", "action": "peer.set_settings",
                                        "params": {"bot_id": peer_id, "settings": {name: next_value}}})
            blocks.append(rich_button_row([rich_button(control_label, kind="callback", value=token)]))
            if name == "acceptance":
                blocks.append(f"<p>{_indent('short reply: ' + _code(str(settings.get('acceptance_text') or '')))}</p>")
        blocks.append("</details>")
        return blocks

    def _peer_entry_blocks(self, state: dict[str, Any], peer: dict[str, Any],
                           *, with_settings: bool = False, text_limit: int = 200) -> list[str]:
        """One peer whole: name pill, username + id, scope badges, last
        exchange, test badge, and the Settings collapsible when asked for."""
        peer_id = str(peer.get("id"))
        username = str(peer.get("username") or "")
        blocks = [f"<p>• {self._peer_pill(peer)}</p>"]
        if username:
            blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
        blocks.append(f"<p>{_indent('id: ' + _copy_pill(peer_id, peer_id))}</p>")
        badges = self._scope_badges(peer)
        if badges:
            blocks.append(f"<p>{_indent('scopes: ' + ' '.join(badges))}</p>")
        blocks.extend(_last_exchange_lines(peer, limit=text_limit))
        test = self._test_badge(peer)
        if test is not None:
            blocks.append(f"<p>{_indent('test: ' + test)}</p>")
        add_test = (state or {}).get("add_test") or {}
        if str(add_test.get("bot_id")) == peer_id and isinstance(add_test.get("test"), dict):
            failed = add_test["test"]
            if not failed.get("ok") and failed.get("error"):
                blocks.append(f"<p>{_indent('test failed: ' + html.escape(str(failed['error'])))}</p>")
        if with_settings:
            blocks.extend(self._settings_details(state, peer))
        return blocks

    # -- Views: each returns (blocks, keyboard rows, total, pages) ------------

    async def _view_managed(self, source: Any, state: dict[str, Any]):
        result = await self.operations.request(source, "managed.list")
        bots = result.get("bots", []) if result.get("ok") else []
        blocks = ["<h1>Managed bots</h1>",
                  "<details><summary>About this list</summary>"
                  "<p>Bots created through this manager bot. Telegram reports each new or "
                  "updated child here; the list below is what this profile has seen.</p></details>"]
        page = max(0, int(state.get("page", 0)))
        total = len(bots)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for bot in bots[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            username = str(bot.get("username") or "")
            label = str(bot.get("name") or username or bot.get("id"))
            blocks.append(f"<p>• {_name_pill(label, bot.get('id'))}</p>")
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
        blocks = ["<h1>Create a managed bot</h1>",
                  f"<p>Manager mode {_manager_mode_badge(self.adapter)}</p>"]
        if created is not None:
            label = str(created.get("name") or username or created.get("id"))
            blocks.append("<p>This bot is now in the roster.</p>")
            blocks.append(f"<p>• {_name_pill(label, created.get('id'))}</p>")
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
                          "notified and the list updates on its own.</i></p>")
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
            label = str(bot.get("name") or username or bot_id)
            blocks.append(f"<p>• {_name_pill(label, bot_id)}</p>")
            if username:
                blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
            blocks.append(f"<p>{_indent('id: ' + _copy_pill(bot_id, bot_id))}</p>")
            blocks.append(f"<p>{_indent('observed: ' + html.escape(_observed(bot.get('observed_at'))))}</p>")
            actions = [rich_button("Access settings", kind="callback",
                                   value=self._token(state, {"kind": "access", "bot_id": bot_id}),
                                   style="primary")]
            if username:
                actions.append(rich_button("Open in Telegram", kind="url",
                                           value=f"https://t.me/{username}"))
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
        label = str(entry.get("name") or username or bot_id)
        users = result.get("add_users", [])
        blocks = ["<h1>Access settings</h1>",
                  f"<p>• {_name_pill(label, bot_id)} {_access_badge(bool(result.get('restricted')))}</p>"]
        if username:
            blocks.append(f"<p>{_indent('username: ' + _copy_pill('@' + username, '@' + username))}</p>")
        blocks.append(f"<p>{_indent('id: ' + _copy_pill(str(bot_id), str(bot_id)))}</p>")
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
                  f"<p>• inbox messages: <b>{len(messages)}</b></p>",
                  "<p>Direct messages need bot-to-bot mode on in BotFather for both bots.</p>"]
        page = max(0, int(state.get("page", 0)))
        total = len(peer_list)
        pages = max(1, math.ceil(total / _PAGE_SIZE)) if total else 1
        page = min(page, pages - 1)
        for peer in peer_list[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]:
            blocks.extend(self._peer_entry_blocks(state, peer, with_settings=True))
        if not peer_list:
            blocks.append("<p>No bot peers are approved in this profile yet.</p>")
            blocks.append("<p><i>Add one below. Approval needs your confirmation.</i></p>")
        rows = [[self._nav_button(state, "Peers", "peers"),
                 self._nav_button(state, "Inbox", "inbox"),
                 self._nav_button(state, "Add peer", "add_peer")],
                [self._nav_button(state, "Banned", "banned")]]
        return blocks, rows, total, pages

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
            groups = peer.get("group_chat_ids", [])
            blocks.extend(self._peer_entry_blocks(state, peer))
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
                       rich_button("Manage", kind="callback",
                                   value=self._token(state, {"kind": "view", "view": f"manage_peer:{peer_id}", "page": 0})),
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
                 self._nav_button(state, "Add peer", "add_peer")],
                [self._nav_button(state, "Banned", "banned")]]
        return blocks, rows, total, pages

    async def _view_peer_manage(self, source: Any, state: dict[str, Any], bot_id: str):
        result = await self.operations.request(source, "peers.list")
        peers = result.get("peers", []) if result.get("ok") else []
        entry = next((peer for peer in peers if str(peer.get("id")) == str(bot_id)), None)
        if entry is None:
            blocks = ["<h1>Manage bot peer</h1>",
                      "<p>This bot is not on this profile's allowlist.</p>"]
            rows = [[self._nav_button(state, "Bot-to-bot", "b2b"),
                     self._nav_button(state, "Peers", "peers")]]
            return blocks, rows, 0, 1
        blocks = ["<h1>Manage bot peer</h1>"]
        blocks.extend(self._peer_entry_blocks(state, entry, text_limit=500))
        settings = entry.get("settings") or {}
        summary = " · ".join(
            f"{name}: {settings.get(name) if settings.get(name) is not None else 'session model'}"
            for name, _label in _SETTING_LABELS)
        blocks.append(f"<p>{_indent('settings: ' + html.escape(summary))}</p>")
        blocks.extend(self._settings_details(state, entry))
        talk = None
        try:
            talk = self.operations._store(source).get_talk(bot_id)
        except (BotOperationsError, OSError, ValueError):
            talk = None
        if talk and talk.get("state") == "live":
            last = html.escape(str(talk.get("last_message") or "")[:200])
            blocks.append(f"<p>{_indent('Talk: live since ' + _when(talk.get('started_at')) + ' — last: ' + last)}</p>")
        rows = [[self._button(state, "Drop", {"kind": "request", "action": "peer.remove",
                                              "params": {"bot_id": str(bot_id)}}, "danger"),
                 self._button(state, "Ban", {"kind": "request", "action": "peer.remove",
                                             "params": {"bot_id": str(bot_id), "ban": True}}, "danger")],
                [self._nav_button(state, "Peers", "peers"),
                 self._nav_button(state, "Bot-to-bot", "b2b")]]
        return blocks, rows, 1, 1

    async def _view_banned(self, source: Any, state: dict[str, Any]):
        # No public banned-list read exists; read the stored list read-only
        # through the store's locked read (the store itself is untouched).
        store = BotOperationsStore(self.operations.profile_home(source))
        banned = store._read(lambda data: [str(item) for item in data.get("banned", [])])
        blocks = ["<h1>Banned bots</h1>",
                  "<p>Bots on this list cannot be added again until they are unbanned.</p>"]
        for bot_id in banned:
            blocks.append(f"<p>• {_copy_pill(bot_id, bot_id)}</p>")
        if not banned:
            blocks.append("<p>No bots are banned.</p>")
        rows = [[self._button(state, "Unban", {"kind": "request", "action": "peer.unban",
                                               "params": {"bot_id": bot_id}})]
                for bot_id in banned]
        rows.append([self._nav_button(state, "Bot-to-bot", "b2b"),
                     self._nav_button(state, "Peers", "peers")])
        return blocks, rows, len(banned), 1

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
        blocks.append("<p><i>Sends go out immediately; anyone admitted can send to an approved peer.</i></p>")
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
        elif view.startswith("manage_peer:"):
            blocks, rows, total, pages = await self._view_peer_manage(source, state, view.split(":", 1)[1])
        elif view == "banned":
            blocks, rows, total, pages = await self._view_banned(source, state)
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
        # Page indexation renders only when there is more than one page.
        text = "".join(blocks)
        if pages > 1:
            text += f"<hr><p><i>{_footer(page, pages, total)}</i></p>"
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
        surface = "managed" if str(action).startswith("managed.") else "b2b"
        if not result.get("ok"):
            if state is not None:
                state["notice"] = f"Error: {html.escape(str(result.get('error', 'request failed'))[:600])}"
                await self._present_view(source, state)
            else:
                await self._display_result(source, "Bot operation failed", result, surface=surface)
            return result
        if action == "peer.send" and result.get("confirmation_required"):
            # Guard-free send, the only action that bypasses confirmation:
            # run it at once through the existing confirm branch (re-validation
            # + reserve_send + send) and show the result card instead.
            payload = {key: value for key, value in result.items()
                       if key not in {"ok", "confirmation_required", "message", "operation"}}
            send_result = await self.operations.confirm(source, "peer.send", **payload)
            if state is not None:
                state["notice"] = ("Message sent." if send_result.get("ok") else
                                   "Error: " + html.escape(str(send_result.get("error", "request failed"))[:500]))
                await self._present_view(source, state)
            else:
                await self._display_result(source, "Send to bot", send_result, surface=surface)
            return send_result
        if result.get("confirmation_required"):
            card = state or self._new_card(source, "confirm")
            card["pending"] = {key: value for key, value in result.items()
                               if key not in {"ok", "confirmation_required"}}
            # Capture the return view before flipping the card to "confirm":
            # the card dict IS the state, so reading it after the flip would
            # return "confirm" and every confirmed action would land on the
            # managed hub instead of re-rendering the view it came from.
            card["return_view"] = (state.get("view") if state is not None else None) or (
                "peers" if action.startswith("peer.") else "managed")
            card["view"] = "confirm"
            card["confirm_id"] = secrets.token_hex(10)
            card["confirm_expires"] = time.monotonic() + _CONFIRM_TTL
            await self._present_confirmation(source, card)
        elif state is not None:
            state["notice"] = "Operation complete."
            await self._present_view(source, state)
        else:
            await self._display_result(source, "Bot operation", result, surface=surface)
        return result

    async def _present_confirmation(self, source: Any, state: dict[str, Any]) -> None:
        pending = state.get("pending") or {}
        summary = html.escape(self._confirmation_summary(pending))
        detail = (f"<pre>{html.escape(str(pending.get('text', '')))}</pre>"
                  if pending.get("operation") == "peer.send" else "")
        buttons = [[self._button(state, "Confirm one change", {"kind": "confirm", "confirm_id": state.get("confirm_id")}, "success"),
                    self._button(state, "Cancel", {"kind": "cancel", "confirm_id": state.get("confirm_id")}, "danger")]]
        text = (f"<h1>Owner confirmation required</h1><hr><p>{summary}</p>{detail}"
                f"<p>This request expires in five minutes and is bound to this profile and chat.</p>"
                f"<details><summary>Details</summary><p>Owner {_code(state['user_id'])} · chat {_code(state['chat_id'])} · "
                f"topic {_code(state['thread_id'] or 'none')} · profile {_code(state['profile_home'])}</p></details>")
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
        if not state:
            # The token was already consumed or the card was pruned: the
            # confirmation expired. The card itself is a live panel, so only
            # the toast answers — no edit, no new message.
            logger.warning("Expired Telegram bot-operations callback")
            await event.answer("This control expired.", alert=True)
            return True
        if (state["user_id"] != str(source.user_id)
                or state["chat_id"] != str(source.chat_id)
                or state["thread_id"] != str(getattr(source, "thread_id", "") or "")
                or state["profile_home"] != current_profile
                or (actor_id is not None and str(actor_id) != str(source.user_id))
                or message_id is None or state.get("message_id") is None
                or str(message_id) != str(state["message_id"])):
            await event.answer("This control does not match this message.", alert=True)
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
                state["add_test"] = None
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
                    self._confirm_notice(state, operation, result)
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

    @staticmethod
    def _confirm_notice(state: dict[str, Any], operation: str, result: dict[str, Any]) -> None:
        """Notice for a confirmed action. A peer.add also keeps the test
        outcome on the card, so the peer entry can badge it and show the
        failure reason in plain text."""
        if operation == "peer.add" and result.get("ok") and isinstance(result.get("test"), dict):
            state["add_test"] = {"bot_id": result.get("peer", {}).get("id"), "test": result["test"]}
            test = result["test"]
            state["notice"] = ("Change applied. Test message passed." if test.get("ok") else
                               "Change applied. Test message failed: " + str(test.get("error") or "unknown reason"))
            return
        state["notice"] = ("Change applied." if result.get("ok") else
                            "Error: " + html.escape(str(result.get("error", "request failed"))[:500]))

    async def handle_command(self, message: Any, source: Any, command: str,
                             arguments: str = "", reply_to: Any = None) -> bool:
        """Route /managedbots, /b2b and /botpeers native commands, returning whether handled.

        The two menus route separately: managed bots and bot-to-bot never share
        a card, a view, or a nav button.
        """
        name = str(command or "").lstrip("/").split("@", 1)[0].lower()
        args = str(arguments or "").split()
        if name == "managedbots":
            return await self._handle_managedbots_command(source, args, reply_to=reply_to)
        if name in {"b2b", "botpeers"}:
            return await self._handle_b2b_command(source, args, reply_to=reply_to)
        return False

    async def _handle_managedbots_command(self, source: Any, args: list[str],
                                          *, reply_to: Any = None) -> bool:
        # Owner-only: a non-owner gets the refusal card for every subcommand,
        # including unrecognized args — never the usage card.
        if not self._authorized(source):
            return await self.open(source, "managed", reply_to=reply_to)
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
            await self._display_result(source, "Managed-bot access", result, reply_to=reply_to, surface="managed")
            return True
        if args[0].lower() in {"restrict", "unrestrict"} and len(args) >= 2:
            current = await self.operations.request(source, "managed.access.get", bot_id=args[1])
            if not current.get("ok"):
                await self._display_result(source, "Managed-bot access", current, reply_to=reply_to, surface="managed")
                return True
            users = [int(user["id"]) for user in current.get("add_users", [])]
            await self._stage(source, "managed.access.set", {"bot_id": args[1],
                "restricted": args[0].lower() == "restrict",
                "add_users": users if args[0].lower() == "restrict" else []})
            return True
        await self._display_result(source, "Managed-bot commands", {"ok": False,
            "error": "Use /managedbots, /managedbots create <username> [name], /managedbots access <bot-id>, /managedbots restrict <bot-id>, or /managedbots unrestrict <bot-id>."}, reply_to=reply_to, surface="managed")
        return True

    async def _handle_b2b_command(self, source: Any, args: list[str],
                                  *, reply_to: Any = None) -> bool:
        """Route /b2b and /botpeers subcommands (hub, peers, inbox, banned, add, remove, send)."""
        if not args:
            return await self.open(source, "b2b", reply_to=reply_to)
        sub = args[0].lower()
        if sub == "peers":
            return await self.open(source, "peers", reply_to=reply_to)
        if sub == "banned":
            return await self.open(source, "banned", reply_to=reply_to)
        if sub == "inbox":
            return await self.open(source, "inbox", reply_to=reply_to)
        if sub == "add" and len(args) == 3:
            scope = args[2].lower()
            if scope not in {"group", "dm", "both"}:
                error = {"ok": False, "error": "Use group, dm, or both; group is limited to the current group chat."}
                await self._display_result(source, "Add bot peer", error, reply_to=reply_to, surface="b2b")
                return True
            group = scope in {"group", "both"}
            dm = scope in {"dm", "both"}
            if group and str(getattr(source, "chat_type", "")) not in {"group", "forum"}:
                await self._display_result(source, "Add bot peer", {"ok": False,
                    "error": "Run this command in the exact group whose bot access you want to allow."}, reply_to=reply_to, surface="b2b")
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
                    "error": "Use /b2b send <bot-id> dm <text> or /b2b send <bot-id> group <chat-id> <text>."}, reply_to=reply_to, surface="b2b")
                return True
            params = {"bot_id": bot_id, "scope": scope, "text": text}
            if scope == "group":
                params["group_chat_id"] = group_chat_id
            await self._stage(source, "peer.send", params)
            return True
        await self._display_result(source, "Bot-to-bot commands", {"ok": False,
            "error": "Use /b2b, /b2b peers, /b2b inbox, /b2b banned, /b2b add <bot-id> <group|dm|both>, /b2b remove <bot-id>, or /b2b send <bot-id> <dm|group> ..."}, reply_to=reply_to, surface="b2b")
        return True

    async def handle_managed_update(self, update: Any, profile_home: str | None = None) -> dict[str, Any]:
        return await self.operations.handle_managed_update(update, profile_home=profile_home)

    async def handle_incoming(self, event: Any, source: Any) -> bool:
        return await self.operations.handle_incoming(event, source)
