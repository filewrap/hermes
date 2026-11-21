"""Owner DM UI for Telegram Business Mode (chat automation).

Implements the ``/biz`` command family (design §7) and the rich cards
(design §9) for the business-bots framework. The onboarding DM and the
sent notice belong to the manager — this module never builds them.

Transport: every owner-facing response is rich HTML sent through the
adapter's ``_send_rich_ui`` (same transport as /start), with in-body
rich buttons (``rich_button``) for actions and profile pills for
customer names. Business replies themselves stay plain text; they are
never built here.

Manager access: the adapter owns the manager's home + lifetime, so the
UI takes a ``manager_factory`` callable (``lambda home:
adapter._business_manager(home)``) and never constructs
``BusinessStateDB`` itself. Tests inject a fake manager directly.
"""
from __future__ import annotations

import html
import inspect
import logging
import time
from typing import Any

from agent.i18n import t

from .business_manager import business_triggered
from .mtproto_rich import edit_request, rich_button, rich_button_row

logger = logging.getLogger(__name__)

_CALLBACK_PREFIX = "hm:biz:"
_CARD_ACTIONS = frozenset({"pause", "resume", "trigger", "trigger_word", "persona",
                           "mute", "unmute", "muted", "recent", "status", "connections"})
_CARD_VIEWS = {"pause": "status", "resume": "status", "trigger": "status",
               "trigger_word": "status", "persona": "status",
               "mute": "recent", "unmute": "recent", "muted": "status",
               "recent": "recent", "status": "status", "connections": "connections"}
# Card-action dispatch: action name -> handler method name on
# TelegramBusinessUI. A handler returns the toast for the shared view
# re-render, or None when it rendered its own view and answered already.
_CARD_ACTION_HANDLERS = {
    "pause": "_card_set_replying",
    "resume": "_card_set_replying",
    "trigger": "_card_toggle_trigger",
    "trigger_word": "_card_prompt_input",
    "persona": "_card_prompt_input",
    "muted": "_card_muted_list",
}
_VIEW_COMMANDS = {"recent", "connections"}
_EXCERPT_LEN = 120


def _t(key: str, default: str, **kwargs: Any) -> str:
    """Locale string with an English fallback when the key is missing."""
    try:
        value = t(key, **kwargs)
    except Exception:
        logger.debug("Business UI locale lookup failed for %s", key, exc_info=True)
        value = None
    if not value or value == key:
        try:
            return default.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.debug("Business UI locale fallback format failed for %s", key,
                         exc_info=True)
            return default
    return value


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _code(value: Any) -> str:
    return f"<code>{_esc(value)}</code>"


def _age(created_at: Any) -> str:
    try:
        seconds = max(0.0, time.time() - float(created_at))
    except (TypeError, ValueError):
        return "unknown age"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _excerpt(text: Any, max_len: int = _EXCERPT_LEN) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) > max_len:
        return flat[: max_len - 1].rstrip() + "…"
    return flat or "(no text)"


def _customer_pill(name: str, chat_id: str) -> str:
    """Customer name as a profile pill when the id is numeric, else bold text."""
    label = " ".join(str(name or "").split()) or f"Customer {chat_id}"
    if str(chat_id).isdecimal():
        return rich_button(label, kind="profile", value=str(chat_id))
    return f"<b>{_esc(label)}</b>"


def _chat_badge() -> str:
    """Small 'chat' badge marking a business-origin message (design §6)."""
    return rich_button("chat", kind="callback", value="badge:chat", style="primary")


def _connection_state_badge(conn: dict[str, Any]) -> str:
    """Active/inactive rich badge for a connection id (design §7)."""
    if conn.get("is_enabled"):
        return rich_button("active", kind="callback", value="badge:enabled",
                           style="success")
    return rich_button("inactive", kind="callback", value="badge:disabled",
                       style="danger")


_RIGHTS_LABELS = {
    "reply": "reply to customers",
    "read_messages": "read messages",
    "delete_sent_messages": "delete sent messages",
    "delete_received_messages": "delete received messages",
    "edit_name": "edit name",
    "edit_bio": "edit bio",
    "edit_profile_photo": "edit profile photo",
    "edit_username": "edit username",
    "view_gifts": "view gifts",
    "sell_gifts": "sell gifts",
    "change_gift_settings": "change gift settings",
    "transfer_and_upgrade_gifts": "transfer and upgrade gifts",
    "transfer_stars": "transfer stars",
    "manage_stories": "manage stories",
}


def _rights_lines(rights: Any) -> list[str]:
    """Granted permissions as indented lines; 'not recorded' when absent."""
    granted = [label for key, label in _RIGHTS_LABELS.items()
               if isinstance(rights, dict) and rights.get(key)]
    if not granted:
        return ["<p>&nbsp;&nbsp;&nbsp;&nbsp;permissions: not recorded</p>"]
    lines = ["<p>&nbsp;&nbsp;&nbsp;&nbsp;permissions:</p>"]
    lines.extend(
        f"<p>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;• {_esc(label)}</p>"
        for label in granted)
    return lines


def _status_button_row(connections: list[dict[str, Any]],
                       settings: dict[str, Any]) -> str:
    """Rich colored buttons only; the active control shows its selected state.

    Selected = ``style="success"`` (replying on, trigger mode on, the
    Status view itself); everything else is ``"primary"``.
    """
    replying = any(bool(c.get("is_enabled")) and bool(c.get("auto_draft", True))
                   for c in connections)
    rows = [rich_button_row([
        rich_button(
            "Replying: on" if replying else "Replying: off",
            kind="callback",
            value=f"hm:biz:{'pause' if replying else 'resume'}",
            style="success" if replying else "primary"),
        rich_button(
            _trigger_button_label(settings),
            kind="callback", value="hm:biz:trigger",
            style="success" if str(settings.get("trigger_mode") or "direct") == "trigger"
            else "primary"),
    ])]
    muted_count = sum(len(c.get("paused_chats") or []) for c in connections)
    persona = " ".join(str(settings.get("persona") or "").split())
    rows.append(rich_button_row([
        rich_button(
            f"Persona: {_excerpt(persona, 20)}" if persona else "Persona: default",
            kind="callback", value="hm:biz:persona"),
        rich_button(
            f"Muted chats ({muted_count})",
            kind="callback", value="hm:biz:muted"),
    ]))
    rows.append(rich_button_row([
        rich_button(_t("platform.telegram.business.button_recent", "Recent"),
                    kind="callback", value="hm:biz:recent"),
        rich_button(_t("platform.telegram.business.button_connections", "Connections"),
                    kind="callback", value="hm:biz:connections"),
        rich_button(_t("platform.telegram.business.button_status", "Status"),
                    kind="callback", value="hm:biz:status", style="success"),
    ]))
    return "".join(rows)


def _trigger_button_label(settings: dict[str, Any]) -> str:
    """Trigger button label: mode, with the current word when set."""
    if str(settings.get("trigger_mode") or "direct") != "trigger":
        return "Trigger: direct"
    word = " ".join(str(settings.get("trigger_word") or "").split())
    return f"Trigger: {word}" if word else "Trigger: on"


def _status_html(connections: list[dict[str, Any]],
                 settings: dict[str, Any],
                 recent_rows: list[dict[str, Any]] | None = None) -> str:
    lines = [f"<h1>{_esc(_t('platform.telegram.business.title', 'Business bots'))}</h1>"]
    active = [c for c in connections if c.get("is_enabled")]
    lines.append(f"<p>{len(active)} active connection(s)</p>")
    rows = list(recent_rows or [])
    for conn in connections:
        conn_id = str(conn.get("connection_id"))
        lines.append(f"<p>• {_code(conn_id)} {_connection_state_badge(conn)}</p>")
        lines.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;owner: {_code(conn.get('owner_user_id'))} · "
                     f"connected {_age(conn.get('created_at'))} · "
                     f"updated {_age(conn.get('updated_at'))}</p>")
        drafting = "on" if conn.get("auto_draft", True) else "paused"
        muted = conn.get("paused_chats") or []
        lines.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;auto-reply: {drafting} · "
                     f"muted chats: {len(muted)}</p>")
        mine = [r for r in rows if str(r.get("connection_id")) == conn_id]
        in_count = sum(1 for r in mine if r.get("direction") == "in")
        out_count = sum(1 for r in mine if r.get("direction") == "out")
        last_in = [r.get("created_at") for r in mine
                   if r.get("direction") == "in" and r.get("created_at") is not None]
        last = f"last customer message {_age(max(last_in))}" if last_in else "no customer messages yet"
        lines.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;messages: {in_count} in · "
                     f"{out_count} out · {last}</p>")
        lines.extend(_rights_lines(conn.get("rights")))
    mode = str(settings.get("trigger_mode") or "direct")
    word = str(settings.get("trigger_word") or "")
    trigger = f"trigger mode: {mode}" + (f", word: {_code(word)}" if word
                                         else ", no trigger word")
    lines.append(f"<p>{trigger}</p>")
    persona = " ".join(str(settings.get("persona") or "").split())
    if persona:
        lines.append(f"<p>persona: {_esc(_excerpt(persona, 100))}</p>")
    lines.append("<p><i>Replies go straight to the customer. Tap Replying to "
                 "pause all replies. Trigger limits replies to messages with "
                 "the word. Muted chats never get replies. Badges: active = "
                 "connection live, chat = sent by chat automation.</i></p>")
    lines.append(_status_button_row(connections, settings))
    return "".join(lines)


def _setup_html() -> str:
    return (
        f"<h1>{_esc(_t('platform.telegram.business.title', 'Business bots'))}</h1>"
        f"<p>{_esc(_t('platform.telegram.business.setup_body', 'No business connection yet.'))}</p>"
        "<p>Telegram → Settings → Business → Chatbots, add this bot, "
        "pick chats, and enable reply permission.</p>"
        "<p>Once connected, /biz shows the status card here and customer "
        "messages get direct replies, as you.</p>"
    )


def _recent_html(rows: list[dict[str, Any]], settings: dict[str, Any]) -> str:
    lines = [f"<h1>{_esc(_t('platform.telegram.business.recent_title', 'Recent messages'))}</h1>"]
    if not rows:
        lines.append(f"<p>{_esc(_t('platform.telegram.business.recent_empty', 'No recent messages.'))}</p>")
    for row in rows:
        chat_id = str(row.get("customer_chat_id"))
        name = str(row.get("customer_name") or "") or f"Customer {chat_id}"
        direction = "wrote" if row.get("direction") == "in" else "sent"
        try:
            triggered = business_triggered(
                str(row.get("text") or ""),
                mode=str(settings.get("trigger_mode") or "direct"),
                word=str(settings.get("trigger_word") or ""),
                reply_to_business_bot=False,
            )
        except Exception:
            logger.debug("Business trigger check failed for recent row", exc_info=True)
            triggered = False
        flag = "triggered" if triggered else "no trigger"
        lines.append(f"<p>• {_customer_pill(name, chat_id)} {_chat_badge()} {direction}: "
                     f"{_esc(_excerpt(row.get('text')))} · "
                     f"{_age(row.get('created_at'))} · {flag}</p>")
    lines.append(rich_button_row([rich_button(
        _t("platform.telegram.business.button_status", "Status"),
        kind="callback", value="hm:biz:status")]))
    return "".join(lines)


def _connections_html(connections: list[dict[str, Any]]) -> str:
    lines = [f"<h1>{_esc(_t('platform.telegram.business.connections_title', 'Business connections'))}</h1>"]
    if not connections:
        lines.append(f"<p>{_esc(_t('platform.telegram.business.connections_empty', 'No business connections.'))}</p>")
    for conn in connections:
        lines.append(f"<p>• {_code(conn.get('connection_id'))} {_connection_state_badge(conn)}</p>")
        lines.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;owner: {_code(conn.get('owner_user_id'))} · "
                     f"connected {_age(conn.get('created_at'))}</p>")
        drafting = "replying on" if conn.get("auto_draft", True) else "replying paused"
        send = "send on" if conn.get("can_reply") else "send off"
        muted = conn.get("paused_chats") or []
        lines.append(f"<p>&nbsp;&nbsp;&nbsp;&nbsp;{drafting} · {send} · "
                     f"muted chats: {len(muted)}</p>")
        lines.extend(_rights_lines(conn.get("rights")))
    lines.append(rich_button_row([rich_button(
        _t("platform.telegram.business.button_status", "Status"),
        kind="callback", value="hm:biz:status")]))
    return "".join(lines)


def _help_html() -> str:
    rows = [
        ("/biz", "status dashboard"),
        ("/biz pause", "pause replying for all connections"),
        ("/biz resume", "resume replying"),
        ("/biz off <chat_id>", "mute replying for one customer chat"),
        ("/biz on <chat_id>", "unmute replying for one customer chat"),
        ("/biz recent", "recent customer messages"),
        ("/biz connections", "business connections"),
        ("/biz trigger [word]", "show, set, or clear the trigger word"),
        ("/biz trigger off", "clear the trigger word"),
        ("/biz mode [direct|trigger]", "show or set the trigger mode"),
        ("/biz persona [text]", "show or set the replying persona"),
        ("/biz help", "this list"),
    ]
    lines = [f"<h1>{_esc(_t('platform.telegram.business.help_title', 'Business bots — commands'))}</h1>"]
    for command, description in rows:
        lines.append(f"<p>{_code(command)} — {_esc(description)}</p>")
    lines.append("<p><i>Replies go straight to the customer — there is no approval step.</i></p>")
    lines.append("<p><i>Badges: active = connection live, inactive = ended, "
                 "chat = sent by chat automation. Tap a badge to see what it means.</i></p>")
    lines.append(rich_button_row([rich_button(
        _t("platform.telegram.business.button_status", "Status"),
        kind="callback", value="hm:biz:status")]))
    return "".join(lines)


def _usage_html(error: str | None = None) -> str:
    lines = [f"<h1>{_esc(_t('platform.telegram.business.usage_title', 'Business bots — usage'))}</h1>"]
    if error:
        lines.append(f"<p>{_esc(error)}</p>")
    lines.append("<p>/biz, /biz pause, /biz resume, /biz off &lt;chat_id&gt;, "
                 "/biz on &lt;chat_id&gt;, /biz recent, "
                 "/biz connections, /biz trigger, /biz mode, /biz persona</p>")
    lines.append("<p>/biz help lists every command.</p>")
    return "".join(lines)


def _notice_html(title: str, body_html: str, *, status_button: bool = True) -> str:
    parts = [f"<h1>{_esc(title)}</h1>", body_html]
    if status_button:
        parts.append(rich_button_row([rich_button(
            _t("platform.telegram.business.button_status", "Status"),
            kind="callback", value="hm:biz:status")]))
    return "".join(parts)


class TelegramBusinessUI:
    """Owner-only DM UI for Telegram Business Mode: /biz + hm:biz: cards."""

    def __init__(self, adapter: Any, manager_factory: Any = None,
                 manager: Any = None):
        self.adapter = adapter
        self._manager_factory = manager_factory
        self._manager = manager

    # -- plumbing ---------------------------------------------------------

    def _home(self, source: Any) -> str | None:
        runner = getattr(self.adapter, "gateway_runner", None)
        route = getattr(runner, "_resolve_profile_home_for_source", None)
        home = route(source) if callable(route) else None
        if home is None:
            resolver = getattr(self.adapter, "_settings_home", None)
            home = resolver(source) if callable(resolver) else None
        return str(home) if home is not None else None

    def _manager_for(self, source: Any) -> Any:
        if self._manager is not None:
            return self._manager
        if self._manager_factory is None:
            raise RuntimeError("Business manager is not configured for this adapter")
        home = self._home(source)
        if home is None:
            raise RuntimeError("Profile routing is unavailable; the business request was refused")
        return self._manager_factory(home)

    def _authorized(self, source: Any) -> bool:
        if (getattr(source, "is_bot", False) or getattr(source, "is_guest", False)
                or getattr(source, "profile_route_rejected", False)
                or not str(getattr(source, "user_id", "")).isdecimal()):
            return False
        owner_check = getattr(self.adapter, "_is_owner_user", None)
        authorized = getattr(self.adapter, "_authorized", None)
        try:
            return bool(callable(owner_check) and owner_check(source.user_id, source)
                        and callable(authorized) and authorized(source))
        except Exception:
            logger.warning("Telegram business owner check failed", exc_info=True)
            return False

    async def _send_card(self, source: Any, html_text: str, reply_to: Any = None) -> None:
        await self.adapter._send_rich_ui(source, html_text, None, reply_to=reply_to)

    async def _edit_card(self, source: Any, message_id: Any, html_text: str) -> str | None:
        """Edit a card in place.

        Returns ``"edited"`` when the edit landed, ``"unchanged"`` when
        Telegram refused it because the content is already current
        (``MessageNotModifiedError`` — e.g. tapping Status while already
        viewing status rebuilds byte-identical html), and ``None`` when
        there is nothing safe to edit or the edit genuinely failed.
        """
        if message_id is None:
            return None
        try:
            peer_fn = getattr(self.adapter, "_peer_for", None)
            if callable(peer_fn):
                peer = peer_fn(source.chat_id)
            else:
                peer_fn = getattr(self.adapter, "_input_peer_for", None)
                if not callable(peer_fn):
                    return None
                peer = await peer_fn(source.chat_id)
            if inspect.isawaitable(peer):
                peer = await peer
            request = edit_request(peer, int(message_id), html_text, rich_format="html")
            await self.adapter._mtproto_rich_request(request)
            return "edited"
        except Exception as exc:
            if type(exc).__name__ == "MessageNotModifiedError":
                logger.debug("Business card is already current; no re-send")
                return "unchanged"
            logger.debug("Business card edit failed", exc_info=True)
            return None

    def _persist_settings(self, source: Any, changes: dict[str, Any]) -> bool:
        """Persist §10 settings via the config worker.

        Returns False when persistence failed — the in-memory change
        still applies, and the caller says so in its response.
        """
        try:
            from .business_config import save_business_settings
        except ImportError:
            logger.debug("business_config helper not available; "
                         "settings apply in memory only", exc_info=True)
            return False
        home = self._home(source)
        if home is None:
            return False
        try:
            save_business_settings(home, dict(changes))
        except Exception:
            logger.warning("Could not persist business settings", exc_info=True)
            return False
        return True

    @staticmethod
    def _persist_note(persisted: bool) -> str:
        if persisted:
            return "<p><i>Saved.</i></p>"
        return "<p><i>Applies until restart — could not save.</i></p>"

    # -- view builders ----------------------------------------------------

    def _connection_row(self, source: Any, manager: Any,
                        connection_id: str) -> dict[str, Any] | None:
        for conn in manager.list_connections(owner_user_id=str(source.user_id),
                                             enabled_only=False):
            if str(conn.get("connection_id")) == str(connection_id):
                return conn
        return None

    async def _build_view_html(self, source: Any, manager: Any, view: str) -> str:
        if view == "recent":
            rows = manager.recent_messages(str(source.chat_id), limit=10)
            return _recent_html(rows, manager.get_settings())
        if view in {"connections", "status"}:
            # Read-time sweep (design §5): an already-dead connection
            # clears on the next render, without another update.
            manager.purge_dead_connections(str(source.user_id))
        if view == "connections":
            return _connections_html(manager.list_connections(
                owner_user_id=str(source.user_id), enabled_only=False))
        if view == "help":
            return _help_html()
        connections = manager.list_connections(owner_user_id=str(source.user_id),
                                               enabled_only=False)
        if not connections:
            return _setup_html()
        recent = manager.recent_messages(str(source.chat_id), limit=50)
        return _status_html(connections, manager.get_settings(), recent)

    async def _show_view(self, source: Any, manager: Any, view: str,
                         reply_to: Any = None) -> bool:
        await self._send_card(source, await self._build_view_html(source, manager, view),
                              reply_to=reply_to)
        return True

    async def _send_owner_only(self, source: Any, reply_to: Any = None) -> bool:
        text = _t("platform.telegram.business.owner_only",
                  "Business bot controls are available only to the profile owner.")
        body = f"<p>{_esc(text)}</p>"
        await self._send_card(
            source, _notice_html(_t("platform.telegram.business.owner_only_title", "Owner only"),
                                 body, status_button=False), reply_to=reply_to)
        return True

    async def open(self, source: Any, view: str = "status", *,
                   reply_to: Any = None) -> bool:
        """Open one business view as a fresh card; non-owners get the refusal."""
        if not self._authorized(source):
            return await self._send_owner_only(source, reply_to=reply_to)
        try:
            manager = self._manager_for(source)
        except Exception:
            logger.warning("Business manager unavailable for open()", exc_info=True)
            await self._send_card(source, _notice_html(
                "Business bots",
                "<p>Business mode is not available right now.</p>"),
                reply_to=reply_to)
            return True
        return await self._show_view(source, manager, view, reply_to)

    # -- /biz subcommands --------------------------------------------------

    async def _cmd_drafting(self, source: Any, manager: Any, pause: bool,
                            reply_to: Any = None) -> bool:
        active = [c for c in manager.list_connections(
            owner_user_id=str(source.user_id), enabled_only=False)
            if c.get("is_enabled")]
        if not active:
            verb = "pause" if pause else "resume"
            await self._send_card(source, _notice_html(
                "Business bots",
                f"<p>No active connections — nothing to {verb}.</p>"),
                reply_to=reply_to)
            return True
        for conn in active:
            manager.set_auto_draft(str(conn["connection_id"]), not pause)
        noun = "connection" if len(active) == 1 else "connections"
        title = ("Replying paused" if pause else "Replying resumed")
        await self._send_card(source, _notice_html(
            title, f"<p>{title} for {len(active)} active {noun}.</p>"),
            reply_to=reply_to)
        return True

    def _find_connection_for_chat(self, source: Any, manager: Any,
                                  customer_chat_id: str) -> str | None:
        owner_chat = str(source.chat_id)
        for row in manager.recent_messages(owner_chat, limit=50):
            if str(row.get("customer_chat_id")) == str(customer_chat_id):
                return str(row.get("connection_id"))
        return None

    async def _cmd_mute(self, source: Any, manager: Any, args: list[str],
                        mute: bool, reply_to: Any = None) -> bool:
        chat_id = args[0] if args else None
        connection_id: str | None = None
        if chat_id is not None and connection_id is None:
            connection_id = self._find_connection_for_chat(source, manager, chat_id)
        if connection_id is None or chat_id is None:
            if chat_id is not None:
                error = (f"Unknown customer chat {chat_id} — pick one from "
                         "/biz recent.")
            else:
                error = "Pass a chat id: /biz off <chat_id>."
            await self._send_card(source, _usage_html(error), reply_to=reply_to)
            return True
        row = self._connection_row(source, manager, connection_id)
        paused = list((row or {}).get("paused_chats") or [])
        if mute:
            if chat_id not in paused:
                paused.append(chat_id)
            manager.set_paused_chats(connection_id, paused)
            title, verb = "Chat muted", "muted"
        else:
            if chat_id in paused:
                paused.remove(chat_id)
            manager.set_paused_chats(connection_id, paused)
            title, verb = "Chat unmuted", "unmuted"
        await self._send_card(source, _notice_html(
            title, f"<p>Customer chat {_code(chat_id)} is {verb} — no replies "
                   f"from it until you turn it back on.</p>"), reply_to=reply_to)
        return True

    async def _cmd_trigger(self, source: Any, manager: Any, args: list[str],
                           reply_to: Any = None) -> bool:
        if not args:
            settings = manager.get_settings()
            word = str(settings.get("trigger_word") or "")
            body = (f"<p>Trigger mode: {_code(settings.get('trigger_mode'))} · "
                    f"trigger word: {_code(word) if word else '(none)'}</p>")
            await self._send_card(source, _notice_html("Business trigger", body),
                                  reply_to=reply_to)
            return True
        if args[0].lower() == "off":
            result = manager.set_trigger(word="")
            persisted = self._persist_settings(source, {"trigger_word": ""})
            body = ("<p>Trigger word cleared.</p>"
                    + self._persist_note(persisted))
            await self._send_card(source, _notice_html("Business trigger", body),
                                  reply_to=reply_to)
            return True
        try:
            result = manager.set_trigger(word=args[0])
        except ValueError as exc:
            logger.debug("Business trigger word rejected: %s", exc, exc_info=True)
            await self._send_card(source, _usage_html(
                "The trigger word must be a single word."), reply_to=reply_to)
            return True
        persisted = self._persist_settings(
            source, {"trigger_word": result.get("trigger_word", "")})
        body = (f"<p>Trigger word set to {_code(result.get('trigger_word'))}.</p>"
                + self._persist_note(persisted))
        await self._send_card(source, _notice_html("Business trigger", body),
                              reply_to=reply_to)
        return True

    async def _cmd_mode(self, source: Any, manager: Any, args: list[str],
                        reply_to: Any = None) -> bool:
        if not args:
            settings = manager.get_settings()
            body = (f"<p>Trigger mode: {_code(settings.get('trigger_mode'))}</p>"
                    "<p>Use /biz mode direct or /biz mode trigger to change it.</p>")
            await self._send_card(source, _notice_html("Trigger mode", body),
                                  reply_to=reply_to)
            return True
        mode = args[0].lower()
        if mode not in {"direct", "trigger"}:
            await self._send_card(source, _usage_html(
                "Use /biz mode direct or /biz mode trigger."), reply_to=reply_to)
            return True
        try:
            result = manager.set_trigger(mode=mode)
        except ValueError as exc:
            logger.debug("Business trigger mode rejected: %s", exc, exc_info=True)
            await self._send_card(source, _usage_html(str(exc)), reply_to=reply_to)
            return True
        persisted = self._persist_settings(
            source, {"trigger_mode": result.get("trigger_mode", mode)})
        body = (f"<p>Trigger mode set to {_code(mode)}.</p>"
                + self._persist_note(persisted))
        await self._send_card(source, _notice_html("Trigger mode", body),
                              reply_to=reply_to)
        return True

    async def _cmd_persona(self, source: Any, manager: Any, args: list[str],
                           reply_to: Any = None) -> bool:
        if not args:
            settings = manager.get_settings()
            body = ("<p>Current persona:</p>"
                    f"<blockquote>{_esc(settings.get('persona'))}</blockquote>")
            await self._send_card(source, _notice_html("Business persona", body),
                                  reply_to=reply_to)
            return True
        persona = " ".join(args)
        result = manager.set_persona(persona)
        persisted = self._persist_settings(
            source, {"owner_persona": result.get("persona", persona)})
        body = (f"<blockquote>{_esc(_excerpt(result.get('persona', persona), 500))}</blockquote>"
                + self._persist_note(persisted))
        await self._send_card(source, _notice_html("Persona updated", body),
                              reply_to=reply_to)
        return True

    async def handle_command(self, message: Any, source: Any, command: str,
                             arguments: str = "", reply_to: Any = None) -> bool:
        """Route /biz (+ subcommands), returning whether handled."""
        name = str(command or "").lstrip("/").split("@", 1)[0].lower()
        if name != "biz":
            return False
        if not self._authorized(source):
            return await self._send_owner_only(source, reply_to=reply_to)
        try:
            manager = self._manager_for(source)
        except Exception:
            logger.warning("Business manager unavailable for /biz", exc_info=True)
            await self._send_card(source, _notice_html(
                "Business bots", "<p>Business mode is not available right now.</p>"),
                reply_to=reply_to)
            return True
        args = str(arguments or "").split()
        if not args:
            return await self._show_view(source, manager, "status", reply_to)
        sub = args[0].lower()
        rest = args[1:]
        if sub in _VIEW_COMMANDS:
            return await self._show_view(source, manager, sub, reply_to)
        if sub == "help":
            return await self._show_view(source, manager, "help", reply_to)
        if sub == "pause":
            return await self._cmd_drafting(source, manager, True, reply_to)
        if sub == "resume":
            return await self._cmd_drafting(source, manager, False, reply_to)
        if sub in {"off", "on"}:
            return await self._cmd_mute(source, manager, rest, sub == "off", reply_to)
        if sub == "trigger":
            return await self._cmd_trigger(source, manager, rest, reply_to)
        if sub == "mode":
            return await self._cmd_mode(source, manager, rest, reply_to)
        if sub == "persona":
            return await self._cmd_persona(source, manager, rest, reply_to)
        await self._send_card(source, _usage_html(
            f"Unknown business command: {sub}. See /biz help."), reply_to=reply_to)
        return True

    # -- hm:biz: callbacks ---------------------------------------------------

    @staticmethod
    def _callback_message_id(event: Any) -> int | None:
        query = getattr(event, "query", None)
        message = getattr(query, "message", None)
        for value in (getattr(query, "msg_id", None),
                      getattr(event, "message_id", None),
                      getattr(message, "id", None)):
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                logger.debug("Business callback carried a bad message id",
                             exc_info=True)
                continue
        return None

    async def _card_set_replying(self, event: Any, source: Any, manager: Any,
                                 action: str, message_id: int | None) -> str:
        """Pause/resume arm: flip auto-reply on every enabled connection."""
        for conn in manager.list_connections(
                owner_user_id=str(source.user_id), enabled_only=False):
            if conn.get("is_enabled"):
                manager.set_auto_draft(str(conn["connection_id"]), action == "resume")
        if action == "resume":
            return _t("platform.telegram.business.toast_resumed", "Replying resumed")
        return _t("platform.telegram.business.toast_paused", "Replying paused")

    async def _card_toggle_trigger(self, event: Any, source: Any, manager: Any,
                                   action: str, message_id: int | None) -> str:
        """Trigger arm: flip direct <-> trigger mode and persist it."""
        settings = manager.get_settings()
        new_mode = "trigger" if settings.get("trigger_mode") == "direct" else "direct"
        try:
            result = manager.set_trigger(mode=new_mode)
            new_mode = str(result.get("trigger_mode") or new_mode)
        except ValueError:
            new_mode = str(settings.get("trigger_mode") or "direct")
        self._persist_settings(source, {"trigger_mode": new_mode})
        return f"Trigger mode: {new_mode}"

    async def _card_prompt_input(self, event: Any, source: Any, manager: Any,
                                 action: str, message_id: int | None) -> None:
        """Trigger-word/persona arm: prompt for the new value.

        The manager armed the owner-input capture before this ran; the
        next DM is consumed as the new value. Renders its own notice.
        """
        noun = "trigger word" if action == "trigger_word" else "persona"
        title = "Trigger word" if action == "trigger_word" else "Persona"
        html_text = _notice_html(
            title, f"<p>Send the new {noun} as your next message.</p>")
        if await self._edit_card(source, message_id, html_text) is None:
            await self._send_card(source, html_text)
        await event.answer(f"Send the new {noun}.")
        return None

    async def _card_muted_list(self, event: Any, source: Any, manager: Any,
                               action: str, message_id: int | None) -> None:
        """Muted arm: list the muted chats with an Unmute button each."""
        muted: list[str] = []
        for conn in manager.list_connections(
                owner_user_id=str(source.user_id), enabled_only=False):
            for chat_id in conn.get("paused_chats") or []:
                if str(chat_id) not in muted:
                    muted.append(str(chat_id))
        if muted:
            body = "".join(f"<p>• {_code(chat_id)}</p>" for chat_id in muted)
        else:
            body = "<p>No muted chats.</p>"
        html_text = _notice_html("Muted chats", body)
        if muted:
            html_text += rich_button_row([
                rich_button(f"Unmute {chat_id}", kind="callback",
                            value=f"hm:biz:unmute:{chat_id}")
                for chat_id in muted
            ])
        if await self._edit_card(source, message_id, html_text) is None:
            await self._send_card(source, html_text)
        await event.answer("Updated")
        return None

    async def _handle_card_action(self, event: Any, source: Any, manager: Any,
                                  action: str, message_id: int | None) -> None:
        handler_name = _CARD_ACTION_HANDLERS.get(action)
        if handler_name is not None:
            toast = await getattr(self, handler_name)(
                event, source, manager, action, message_id)
            if toast is None:
                return  # the handler rendered its own view and answered
        else:
            toast = _t("platform.telegram.business.toast_updated", "Updated")
        html_text = await self._build_view_html(source, manager, _CARD_VIEWS[action])
        # A fresh card goes out ONLY when the edit genuinely failed (the
        # card message is gone). An "unchanged" edit means the card
        # already shows the current state — re-sending it would spam the
        # owner with a duplicate on every tap of the current view.
        if await self._edit_card(source, message_id, html_text) is None:
            await self._send_card(source, html_text)
        await event.answer(toast)

    async def _handle_chat_callback(self, event: Any, source: Any, manager: Any,
                                    raw: str, message_id: int | None) -> None:
        """A per-chat hm:biz: tap (mute/unmute) or a 1-part card action.

        The manager applies the change (mute/unmute) or arms the
        owner-input capture (trigger_word/persona) and returns the view
        name to render; this renders it.
        """
        async def answer(text: Any = None, alert: bool = False) -> None:
            await event.answer(str(text or "") or None, alert=bool(alert))

        result = await manager.handle_callback(
            data=raw, caller_user_id=str(source.user_id), answer=answer)
        if isinstance(result, str):
            await self._handle_card_action(event, source, manager, result, message_id)

    async def handle_callback(self, event: Any, source: Any, data: Any) -> bool:
        """Dispatch an hm:biz: callback; False when the data is not ours."""
        raw = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data or "")
        if not raw.startswith(_CALLBACK_PREFIX):
            return False
        if not self._authorized(source):
            await event.answer(
                "Only the connected account owner can use these buttons.", alert=True)
            return True
        try:
            manager = self._manager_for(source)
        except Exception:
            logger.warning("Business manager unavailable for callback", exc_info=True)
            await event.answer("Business mode is not available right now.")
            return True
        parts = raw[len(_CALLBACK_PREFIX):].split(":")
        message_id = self._callback_message_id(event)
        if len(parts) == 1 and parts[0] in _CARD_ACTIONS:
            await self._handle_chat_callback(event, source, manager, raw, message_id)
            return True
        if len(parts) == 2 and parts[0] in {"mute", "unmute"}:
            await self._handle_chat_callback(event, source, manager, raw, message_id)
            return True
        await event.answer("Unknown action.")
        return True


# ---------------------------------------------------------------------------
# Module-level entries (integration seams)
# ---------------------------------------------------------------------------
# ``operations_routing`` and ``business_adapter`` call these by module
# name; they build the UI on the adapter's cached business manager (the
# same manager the adapter's own callback path uses) and delegate to the
# methods above. The ``business_adapter`` import stays lazy: the adapter
# imports this module lazily too, so a module-level import would cycle.


def _ui_for(adapter: Any) -> TelegramBusinessUI:
    from .business_adapter import business_manager

    return TelegramBusinessUI(adapter, manager=business_manager(adapter))


async def handle_command(adapter: Any, message: Any, source: Any, command: str,
                         arguments: str = "", reply_to: Any = None) -> bool:
    """Module entry for ``/biz`` (called by operations_routing)."""
    return await _ui_for(adapter).handle_command(
        message, source, command, arguments, reply_to=reply_to)


async def handle_card_action(adapter: Any, event: Any, source: Any,
                             action: str) -> None:
    """Module entry for ``hm:biz:<card-action>`` taps (called by business_adapter)."""
    ui = _ui_for(adapter)
    manager = ui._manager_for(source)
    await ui._handle_card_action(
        event, source, manager, action, ui._callback_message_id(event))
