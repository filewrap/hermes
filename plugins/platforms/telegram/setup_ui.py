"""Owner-only Telegram setup control centre for profile-scoped Hermes configuration."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from html import escape
from pathlib import Path
from typing import Any

from .mtproto_rich import edit_request, inline_markup, send_request
from .setup_fields import FIELDS as _FIELDS
from .setup_fields import HOME_ITEMS as _HOME_ITEMS
from .setup_fields import HOME_PAGE_SIZE as _HOME_PAGE_SIZE
from .setup_fields import HUB_COMMANDS as _HUB_COMMANDS
from .setup_fields import SECTIONS as _SECTIONS
from .control_navigation import ControlNavigation, setup_frame

logger = logging.getLogger(__name__)

_STATE_TTL = 15 * 60
_INPUT_TTL = 5 * 60

_PAGE_SIZE = 6
_MISSING = object()


class TelegramSetupUI:
    """Private setup center whose callbacks and typed input are bound to one owner/profile."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._pending: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._receipts: dict[tuple[Any, ...], float] = {}
        self._action_lock = asyncio.Lock()

    def _home(self, source: Any) -> Path:
        settings_home = getattr(self.adapter, "_settings_home", None)
        if callable(settings_home):
            home = settings_home(source)
            if home is None:
                raise RuntimeError("The active Telegram profile could not be resolved")
            return Path(home).expanduser().resolve()
        runner = getattr(self.adapter, "gateway_runner", None)
        resolver = getattr(runner, "_resolve_profile_home_for_source", None)
        if callable(resolver):
            home = resolver(source)
            if home is None:
                raise RuntimeError("The active Telegram profile could not be resolved")
        else:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        return Path(home).expanduser().resolve()

    def _allowed(self, source: Any) -> bool:
        if (str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}
                or not getattr(source, "chat_id", None)
                or not getattr(source, "user_id", None)):
            return False
        authorized = getattr(self.adapter, "_authorized", None)
        owner = getattr(self.adapter, "_is_owner_user", None)
        if not callable(authorized) or not callable(owner):
            return False
        try:
            return bool(authorized(source)) and bool(owner(source.user_id, source=source))
        except Exception:
            logger.warning("Telegram setup owner authorization failed closed", exc_info=True)
            return False

    @staticmethod
    def _identity(home: Path, source: Any) -> tuple[str, str, str, str, str]:
        return (str(home), str(getattr(source, "profile", "") or ""),
                str(getattr(source, "user_id", "") or ""),
                str(getattr(source, "chat_id", "") or ""),
                str(getattr(source, "thread_id", "") or ""))

    def _prune(self) -> None:
        now = time.monotonic()
        for key, expires_at in list(self._receipts.items()):
            if now >= expires_at:
                self._receipts.pop(key, None)
        while len(self._receipts) > 256:
            self._receipts.pop(next(iter(self._receipts)))
        for key, pending in list(self._pending.items()):
            if now >= pending["expires_at"]:
                self._pending.pop(key, None)
                state = self._states.get(pending.get("state_id"))
                if state and state.get("pending_field") == pending.get("field"):
                    state["pending_field"] = None
                    state["pending_expires_at"] = None
        for sid, state in list(self._states.items()):
            if now - state["created_at"] > _STATE_TTL:
                self._drop(sid)

    def _drop(self, sid: int) -> None:
        state = self._states.pop(sid, None)
        if state:
            self._pending.pop(state["identity"], None)

    def cancel_for_source(self, source: Any) -> None:
        """Invalidate this source's cards and pending edits before credential capture."""
        if not self._allowed(source):
            return
        identity = self._identity(self._home(source), source)
        for sid, state in list(self._states.items()):
            if state["identity"] == identity:
                self._drop(sid)

    def _rotate_state(self, state: dict[str, Any]) -> None:
        """Make callbacks on the just-used keyboard one-shot before awaiting work."""
        old_id = int(state["id"])
        self._states.pop(old_id, None)
        self._counter += 1
        state["id"] = self._counter
        self._states[state["id"]] = state
        pending = self._pending.get(state["identity"])
        if pending and pending.get("state_id") == old_id:
            pending["state_id"] = state["id"]

    def _new_state(self, source: Any, home: Path, section: str = "home") -> dict[str, Any]:
        self._prune()
        identity = self._identity(home, source)
        for sid, old in list(self._states.items()):
            if old["identity"] == identity:
                self._drop(sid)
        self._counter += 1
        state = {
            "id": self._counter, "identity": identity, "home": str(home),
            "profile": str(getattr(source, "profile", "") or ""),
            "user_id": str(source.user_id), "chat_id": str(source.chat_id),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "section": section, "created_at": time.monotonic(), "message_id": None,
            "pending_field": None, "pending_expires_at": None,
            "navigation": ControlNavigation(),
            "option_field": None, "confirm_selection": None,
            "confirm_toggle": None,
        }
        self._states[state["id"]] = state
        return state

    def _profile_scope(self, home: str):
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(Path(home))

    def _load_config(self, state: dict[str, Any]) -> dict[str, Any]:
        from hermes_cli.config_effective import load_user_config_effective
        with self._profile_scope(state["home"]):
            config = load_user_config_effective(fail_closed=True)
        return config if isinstance(config, dict) else {}

    def _save_value(self, state: dict[str, Any], key: str, value: Any) -> Any:
        """Persist through the canonical config setter (including terminal env bridges)."""
        from hermes_cli.config import set_config_value
        from hermes_cli.config_effective import load_user_config_effective
        import yaml

        # The editor only exposes known live config paths. The shared setter retains the raw
        # YAML, validates its path, applies terminal runtime bridges, and invalidates its cache.
        with self._profile_scope(state["home"]):
            # PyYAML appends a document-end marker to root scalar dumps; set_config_value
            # expects the scalar token itself (not ``0.5\n...``), so keep its first line.
            scalar = yaml.safe_dump(value, default_flow_style=True, width=10000).splitlines()[0]
            set_config_value(key, scalar)
            reread = load_user_config_effective(fail_closed=True)
        if not isinstance(reread, dict):
            raise RuntimeError("Saved configuration could not be read back")
        actual = self._nested_value(reread, key)
        if actual is _MISSING:
            raise RuntimeError("Saved value is not present in the active profile configuration")
        # A terminal setting can have an intentional environment bridge; show the effective value.
        return actual

    @staticmethod
    def _nested_value(config: dict[str, Any], key: str) -> Any:
        value: Any = config
        for segment in key.split("."):
            if not isinstance(value, dict) or segment not in value:
                return _MISSING
            value = value[segment]
        return value

    @staticmethod
    def _value(config: dict[str, Any], key: str) -> Any:
        if key == "display.platforms.telegram.tool_progress":
            from gateway.display_config import resolve_tool_progress
            return resolve_tool_progress(config, "telegram")[0]
        value = TelegramSetupUI._nested_value(config, key)
        if value is not _MISSING:
            return value
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        value = TelegramSetupUI._nested_value(DEFAULT_CONFIG, key)
        return "Not set" if value is _MISSING else value

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, bool):
            return "On" if value else "Off"
        if value is None:
            return "Not set"
        return str(value)

    @staticmethod
    def _table(rows: list[tuple[str, str]]) -> str:
        html_rows = "".join(
            f"<tr><th>{escape(str(label))}</th><td>{escape(str(value))}</td></tr>"
            for label, value in rows
        )
        return "<table><tbody>" + html_rows + "</tbody></table>"

    @staticmethod
    def _footer(page: int, pages: int, total: int) -> str:
        return f"<hr/><p><i>Page {page} of {pages} • total {total}</i></p>"

    def _button(self, state: dict[str, Any], label: str, action: str, style: str | None = None):
        return (label, f"hm:setup:{state['id']}:{action}", style)

    @staticmethod
    def _page_count(section: str, inventory_node: int = 0) -> int:
        if section == "inventory":
            from .setup_cli_inventory import command_inventory
            node = command_inventory()[inventory_node]
            return max(1, math.ceil((len(node["children"]) + len(node["options"])) / _PAGE_SIZE))
        total = len(_HOME_ITEMS) if section == "home" else len(_SECTIONS.get(section, _SECTIONS["terminal"])[1])
        size = _HOME_PAGE_SIZE if section == "home" else _PAGE_SIZE
        return max(1, math.ceil(total / size))

    @staticmethod
    def _option_label(field: dict[str, Any], value: Any) -> str:
        for option_value, label in field["options"]:
            if value == option_value or str(value).lower() == str(option_value).lower():
                return label
        return TelegramSetupUI._display_value(value)

    def _view(self, state: dict[str, Any], *, note: str = "") -> tuple[str, Any]:
        section = state["section"]
        if section == "inventory":
            return self._inventory_view(state, note)
        config = self._load_config(state)
        if section == "home":
            pages = self._page_count(section)
            page = min(max(1, int(state.get("page", 1))), pages)
            state["page"] = page
            start = (page - 1) * _HOME_PAGE_SIZE
            visible = _HOME_ITEMS[start:start + _HOME_PAGE_SIZE]
            rows = [(label, description) for label, description, _action in visible]
            text = ("<h1>Hermes setup</h1><p>Owner-only controls for the active profile: "
                    + escape(state["profile"] or "default") + ". Editable settings use the existing runtime configuration. "
                    "Changes are saved and read back before confirmation.</p>"
                    "<p>Credentials, OAuth, dependency installation and service restarts require the existing "
                    "CLI flows. The command inventory explains each path and its prerequisites. "
                    "Prompt-bound memory, skill, and agent-guidance edits apply when a new session builds its prompt; "
                    "an active conversation's prompt cache is not reset.</p>"
                    "<hr/><h2>Setup overview</h2>" + self._table(rows))
            buttons = [[self._button(state, "Overview", "section:home", "primary")]]
            for offset in range(0, len(visible), 2):
                buttons.append([
                    self._button(state, item[0], item[2])
                    for item in visible[offset:offset + 2]
                ])
            total = len(_HOME_ITEMS)
        else:
            title, keys = _SECTIONS.get(section, _SECTIONS["terminal"])
            total = len(keys)
            pages = self._page_count(section)
            page = min(max(1, int(state.get("page", 1))), pages)
            state["page"] = page
            keys = keys[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE]
            rows = []
            for key in keys:
                field = _FIELDS[key]
                value = self._value(config, key)
                label = self._option_label(field, value) if field["type"] == "option" else self._display_value(value)
                rows.append((field["label"], label))
            text = ("<h1>Hermes setup</h1><p>These controls edit existing profile configuration only. "
                    "Opening this page changes nothing. Credentials and global security overrides "
                    "are not edited here.</p><hr/><h2>" + escape(title)
                    + "</h2>" + self._table(rows)
                    + "".join("<p><b>" + escape(_FIELDS[key]["label"]) + ":</b> "
                              + escape(_FIELDS[key]["help"]) + "</p>" for key in keys)
                    + "<p>Prompt-bound settings apply to newly built sessions; gateway startup settings require "
                    "a separately authorized restart. This menu never restarts the gateway.</p>")
            buttons = []
            for key in keys:
                field = _FIELDS[key]
                value = self._value(config, key)
                if field["type"] == "bool":
                    label = f"{field['label']} · {self._display_value(value)}"
                    buttons.append([self._button(state, label, f"toggle:{key}",
                                                 "success" if bool(value) else "danger")])
                elif field["type"] == "option":
                    label = f"{field['label']} · {self._option_label(field, value)}"
                    buttons.append([self._button(state, label, f"cycle:{key}")])
                else:
                    buttons.append([self._button(state, f"Edit {field['label']}", f"edit:{key}")])
            if section == "skills":
                buttons.append([self._button(state, "Manage skills", "nav:skills")])
            buttons.append([self._button(state, "← Back to setup", "back:previous", "primary")])
        if pages > 1:
            navigation = []
            if page > 1:
                navigation.append(self._button(state, f"Previous · {page - 1}/{pages}", f"page:{page - 1}"))
            if page < pages:
                navigation.append(self._button(state, f"Next · {page + 1}/{pages}", f"page:{page + 1}", "primary"))
            buttons.append(navigation)
        if section == "home" and state.get("navigation"):
            buttons.append([self._button(state, "← Back", "back:previous", "primary")])
        text = self._pending_notice(state, text, buttons)
        if note:
            text += f"<hr/><p><b>Status:</b> {escape(note)}</p>"
        text += self._footer(page, pages, total)
        return text, inline_markup(buttons)

    def _pending_notice(self, state: dict[str, Any], text: str, buttons: list) -> str:
        tools_ui = getattr(self.adapter, "_telegram_setup_tools_ui", None)
        if tools_ui is not None:
            notice = tools_ui.pending_notice_for_identity(state["identity"])
            if notice:
                text += "<hr/><p><b>" + escape(notice) + "</b></p>"
                buttons.append([self._button(state, "Review pending changes", "nav:setuptools", "primary")])
        return text

    def _inventory_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        from .setup_cli_inventory import command_inventory
        from .setup_inventory import iter_cli_only_items
        commands = command_inventory()
        node = commands[int(state.get("inventory_node", 0))]
        entries = [("command", commands[index]) for index in node["children"]]
        entries += [("option", option) for option in node["options"]]
        pages = self._page_count("inventory", node["id"])
        page = min(max(1, int(state.get("page", 1))), pages)
        state["page"] = page
        command = "hermes" + (" " + " ".join(node["path"]) if node["path"] else "")
        text = ("<h1>CLI command inventory</h1><h2><code>" + escape(command) + "</code></h2>"
                "<p>Browse every built-in command, nested subcommand, alias and visible argument. "
                "These are instructions, not executable buttons. No CLI command is run by browsing.</p>")
        if node["description"]:
            text += "<p>" + escape(node["description"]) + "</p>"
        if node["aliases"]:
            text += "<p><b>Aliases:</b> " + escape(", ".join(node["aliases"])) + "</p>"
        root = node["path"][0] if node["path"] else ""
        matches = [(area, label, path, reason) for area, label, path, reason in iter_cli_only_items()
                   if root and ("hermes " + root) in path]
        if matches:
            text += ("<p><b>CLI boundary:</b> " + escape(matches[0][3])
                     + " Use <code>" + escape(command + " --help") + "</code> on this Linux host.</p>")
        else:
            text += ("<p>Audited non-secret preferences are available in the setup sections. "
                     "Other command operations remain CLI-only because they may execute tools, "
                     "access credentials, alter files/services, or require terminal interaction. "
                     "Use <code>" + escape(command + " --help") + "</code> for the full CLI contract.</p>")
        rows, buttons = [], []
        for kind, item in entries[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE]:
            if kind == "command":
                label = " ".join(item["path"])
                rows.append(("hermes " + label, item["description"] or "Open to inspect nested commands and arguments"))
                buttons.append([self._button(state, item["path"][-1], f"inventory:{item['id']}")])
            else:
                label = " / ".join(item["flags"]) or item["metavar"]
                details = item["help"] or "Command argument"
                if item["required"]:
                    details += " Required."
                if item["choices"]:
                    details += " Choices: " + ", ".join(item["choices"])
                rows.append((label, details))
        text += self._table(rows)
        quick_links = {
            "setup": [("Setup sections", "section:home")],
            "model": [("Model selector", "nav:model"), ("Providers", "nav:providers")],
            "tools": [("Telegram toolsets", "nav:setuptools")],
            "mcp": [("Personal MCP controls", "nav:mcp")],
            "browser": [("Browser controls", "nav:browser")],
            "dashboard": [("Dashboard / WebUI", "nav:dashboard")],
            "memory": [("Memory preferences", "section:memory")],
            "skills": [("Skills controls", "nav:skills")],
            "gateway": [("Gateway preferences", "section:gateway")],
            "config": [("Setup preferences", "section:home")],
            "logs": [("Log viewer", "nav:logs")],
        }
        for label, action in quick_links.get(root, []):
            buttons.append([self._button(state, label, action)])
        navigation = []
        if page > 1:
            navigation.append(self._button(state, f"Previous · {page - 1}/{pages}", f"page:{page - 1}"))
        if page < pages:
            navigation.append(self._button(state, f"Next · {page + 1}/{pages}", f"page:{page + 1}", "primary"))
        if navigation:
            buttons.append(navigation)
        buttons.append([self._button(state, "← Back", "back:previous", "primary")])
        text = self._pending_notice(state, text, buttons)
        if note:
            text += "<p><b>Status:</b> " + escape(note) + "</p>"
        text += self._footer(page, pages, len(entries))
        return text, inline_markup(buttons)

    def _prompt_view(self, state: dict[str, Any], field_key: str, *, note: str = "") -> tuple[str, Any]:
        field = _FIELDS[field_key]
        bounds = ""
        if field["type"] in {"int", "float"}:
            bounds = f" Allowed range: {field['min']} to {field['max']}."
        text = ("<h1>Hermes setup</h1><hr/><h2>" + escape(field["label"])
                + "</h2><p>" + escape(field["help"])
                + "</p><p>Send the new value as a message in this private chat."
                + escape(bounds) + " Your input expires after five minutes. Send /cancel to stop editing.</p>")
        if note:
            text += f"<p><b>Status:</b> {escape(note)}</p>"
        text += self._footer(1, 1, 1)
        markup = inline_markup([[self._button(state, "← Back", "cancel:input", "primary")]])
        return text, markup

    def _option_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        key = state["option_field"]
        field = _FIELDS[key]
        current = self._value(self._load_config(state), key)
        text = ("<h1>" + escape(field["label"]) + "</h1><p>" + escape(field["help"])
                + "</p><p>Scope: this profile only. Opening this selector does not change configuration.</p>"
                + self._table([("Current selection", self._option_label(field, current))]))
        buttons = []
        selection = state.get("confirm_selection")
        if selection is not None:
            value, label = field["options"][selection]
            text += ("<h2>Confirm selection</h2><p>Save <b>" + escape(label) + "</b> to <code>"
                     + escape(key) + "</code>? This does not install dependencies, provision credentials, "
                     "restart a service or change any other setting. Existing sessions may retain their current configuration.</p>")
            buttons.append([self._button(state, "Confirm selection", "apply:selection", "success")])
        else:
            for index, (value, label) in enumerate(field["options"]):
                selected = type(value) is type(current) and value == current
                buttons.append([self._button(state, label + (" (current)" if selected else ""),
                                              f"select:{index}", "success" if selected else None)])
        buttons.append([self._button(state, "← Back", "cancel:selection", "primary")])
        if note:
            text += "<p><b>Status:</b> " + escape(note) + "</p>"
        text += self._footer(1, 1, len(field["options"]))
        return text, inline_markup(buttons)

    def _toggle_confirm_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        key, value = state["confirm_toggle"]
        field = _FIELDS[key]
        gate = ("Turning approval off allows the corresponding "
                "memory or skill changes without this review gate."
                if not value else
                "Turning approval on means the corresponding "
                "memory or skill changes will need this review gate first.")
        text = ("<h1>Confirm " + escape(field["label"]) + "</h1><p>"
                + escape(field["help"]) + "</p><p>Save <b>" + self._display_value(value)
                + "</b> for this profile? " + gate + " Existing conversations "
                "are not reset and no files are changed by this confirmation beyond the preference.</p>")
        if note:
            text += "<p><b>Status:</b> " + escape(note) + "</p>"
        text += self._footer(1, 1, 1)
        return text, inline_markup([
            [self._button(state, "Confirm change", "apply:toggle", "danger" if not value else "success")],
            [self._button(state, "← Back", "cancel:toggle", "primary")],
        ])

    async def _send(self, source: Any, state: dict[str, Any], text: str, markup: Any, reply_to: Any = None) -> None:
        if getattr(self.adapter, "_client", None) is None:
            return
        peer = self.adapter._peer_for(source.chat_id)
        topic_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
        request = send_request(
            peer, text,
            reply_to=int(reply_to) if reply_to is not None else None,
            top_msg_id=topic_id, reply_markup=markup, rich_format="html",
        )
        result = await self.adapter._mtproto_rich_request(request)
        sent_id = getattr(self.adapter, "_sent_message_id", None)
        message_id = (sent_id(result, random_id=getattr(request, "random_id", None),
                              peer=peer, topic_id=topic_id) if callable(sent_id) else None)
        if message_id is None:
            message = getattr(result, "message", None)
            message_id = getattr(message, "id", None) or getattr(result, "id", None)
        state["message_id"] = str(message_id) if message_id is not None else None

    async def _edit(self, state: dict[str, Any], text: str, markup: Any) -> None:
        if not state.get("message_id") or getattr(self.adapter, "_client", None) is None:
            return
        await self.adapter._mtproto_rich_request(edit_request(
            self.adapter._peer_for(state["chat_id"]), int(state["message_id"]), text,
            reply_markup=markup, rich_format="html",
        ))

    async def _refresh(self, state: dict[str, Any], note: str = "") -> None:
        if state.get("confirm_toggle"):
            text, markup = self._toggle_confirm_view(state, note=note)
        elif state.get("option_field"):
            text, markup = self._option_view(state, note=note)
        elif state.get("pending_field"):
            text, markup = self._prompt_view(state, state["pending_field"], note=note)
        else:
            text, markup = self._view(state, note=note)
        if note == "Working…":
            markup = None
        await self._edit(state, text, markup)

    async def _answer(self, event: Any, text: str = "", alert: bool = False) -> None:
        try:
            if text or alert:
                await event.answer(text, alert=alert)
            else:
                await event.answer()
        except Exception:
            logger.debug("Unable to acknowledge Telegram setup callback", exc_info=True)

    async def handle_command(self, source: Any, arguments: str = "", reply_to: Any = None) -> None:
        """Open the setup center, optionally directly at terminal/compression/display."""
        if not self._allowed(source):
            await self.adapter.send(source.chat_id,
                                    "Hermes setup is available only to the authorized owner in a private chat.",
                                    reply_to=reply_to)
            return
        try:
            home = self._home(source)
        except Exception:
            logger.warning("Telegram setup profile resolution failed closed", exc_info=True)
            await self.adapter.send(source.chat_id,
                                    "Hermes setup could not safely resolve the active profile. No settings were changed.",
                                    reply_to=reply_to)
            return
        requested = str(arguments or "").strip().lower()
        section = requested if requested in _SECTIONS else "home"
        self._prune()
        identity = self._identity(home, source)
        existing_message_id = next((old.get("message_id") for old in self._states.values()
                                    if old["identity"] == identity), None)
        routes = getattr(self.adapter, "_control_routes", None)
        identity_fn = getattr(self.adapter, "_route_identity", None)
        if not existing_message_id and callable(routes) and callable(identity_fn):
            prefix = identity_fn(source, "")[:-1]
            existing_message_id = next((route["message_id"] for key, route in routes().items()
                                        if key[:-1] == prefix), None)
        cancel_editors = getattr(self.adapter, "_cancel_card_editors", None)
        if existing_message_id and callable(cancel_editors):
            cancel_editors(source, existing_message_id)
        state = self._new_state(source, home, section)
        text, markup = self._view(state)
        if existing_message_id:
            if callable(routes) and callable(identity_fn):
                routes().pop(identity_fn(source, existing_message_id), None)
            state["message_id"] = existing_message_id
            await self._edit(state, text, markup)
        else:
            await self._send(source, state, text, markup, reply_to=reply_to)

    def _state_for_callback(self, event: Any, source: Any, state_id: int) -> dict[str, Any] | None:
        self._prune()
        state = self._states.get(state_id)
        if not state or not self._allowed(source):
            return None
        try:
            home = self._home(source)
        except Exception:
            return None
        callback_identity = self._identity(home, source)
        if (state["identity"][:4] != callback_identity[:4]
                or state["profile"] != str(getattr(source, "profile", "") or "")
                or state["user_id"] != str(getattr(source, "user_id", "") or "")
                or state["chat_id"] != str(getattr(source, "chat_id", "") or "")
                or (getattr(source, "thread_id", None) is not None
                    and state["thread_id"] != str(source.thread_id))
                or state["message_id"] != str(getattr(event, "message_id", "") or "")):
            return None
        return state

    @staticmethod
    def _source_with_state_thread(source: Any, state: dict[str, Any]) -> Any:
        if getattr(source, "thread_id", None) or not state.get("thread_id"):
            return source
        try:
            from gateway.session_identity import replace_source
            return replace_source(source, thread_id=state["thread_id"])
        except (TypeError, ValueError):
            return source

    @staticmethod
    def _parse_value(field: dict[str, Any], text: str) -> Any:
        value = text.strip()
        if not value or len(value) > 2048 or any(ord(char) < 32 and char not in "\t" for char in value):
            raise ValueError("Enter a non-empty value without control characters.")
        kind = field["type"]
        if kind == "path":
            path = Path(value).expanduser()
            if not path.is_dir():
                raise ValueError("That path is not an existing directory. Try a directory such as . or ~/projects.")
            return os.fspath(path.resolve())
        if kind == "language":
            import re
            if not re.fullmatch(r"(?i:auto|[a-z]{2,3}(?:-[a-z0-9]{2,8})*)", value):
                raise ValueError("Enter a language tag such as en, es, zh, or auto.")
            return value.lower() if value.lower() == "auto" else value
        if kind == "voice":
            import re
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
                raise ValueError("Enter a voice name containing only letters, numbers, dots, underscores, or hyphens.")
            return value
        if kind == "int":
            try:
                number = int(value, 10)
            except ValueError as exc:
                raise ValueError("Enter a whole number.") from exc
        elif kind == "float":
            try:
                number = float(value)
            except ValueError as exc:
                raise ValueError("Enter a number.") from exc
            if not math.isfinite(number):
                raise ValueError("Enter a finite number.")
        else:
            raise ValueError("This setting cannot be edited as text.")
        if not field["min"] <= number <= field["max"]:
            raise ValueError(f"Enter a value from {field['min']} to {field['max']}.")
        return number

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        """Handle only ``hm:setup:`` controls, validating owner, DM and originating card."""
        if not str(data).startswith("hm:setup:"):
            return False
        async with self._action_lock:
            pieces = str(data).split(":", 3)
            if len(pieces) != 4 or not pieces[2].isdigit():
                await self._answer(event, "This setup action is unavailable or expired.", alert=True)
                return True
            state = self._state_for_callback(event, source, int(pieces[2]))
            if state is None:
                await self._answer(event, "This setup action is expired or not authorized for this account.", alert=True)
                return True
            self._rotate_state(state)
            action_type, separator, payload = pieces[3].partition(":")
            handlers = {
                "section": self._handle_section_action,
                "edit": self._handle_edit_action,
                "toggle": self._handle_toggle_action,
                "cycle": self._handle_cycle_action,
                "page": self._handle_page_action,
                "nav": self._handle_navigation_action,
                "back": self._handle_back_action,
                "cancel": self._handle_cancel_action,
                "select": self._handle_select_action,
                "apply": self._handle_apply_action,
                "inventory": self._handle_inventory_action,
            }
            handler = handlers.get(action_type) if separator else None
            try:
                if handler is None:
                    raise ValueError("Unknown setup action")
                await handler(event, source, state, payload)
            except Exception as exc:
                logger.warning("Telegram setup action failed (%s)", type(exc).__name__)
                await self._answer(event)
                try:
                    await self._refresh(state, "The action could not be completed. No change was confirmed; displayed values are read back from configuration.")
                except Exception:
                    logger.debug("Could not refresh Telegram setup card", exc_info=True)
            return True

    async def _handle_section_action(self, event: Any, _source: Any,
                                     state: dict[str, Any], section: str) -> None:
        if section not in {"home", *_SECTIONS.keys()}:
            raise ValueError("Unknown setup section")
        self._pending.pop(state["identity"], None)
        state["pending_field"] = None
        state["pending_expires_at"] = None
        state["option_field"] = None
        state["confirm_selection"] = None
        state["confirm_toggle"] = None
        if state["section"] != section:
            state["navigation"].push(setup_frame(state))
        state["section"] = section
        state["page"] = 1
        await self._answer(event)
        await self._refresh(state)

    async def _handle_cancel_action(self, event: Any, _source: Any,
                                    state: dict[str, Any], _payload: str) -> None:
        self._pending.pop(state["identity"], None)
        state["pending_field"] = None
        state["pending_expires_at"] = None
        state["confirm_toggle"] = None
        if state.get("confirm_selection") is not None:
            state["confirm_selection"] = None
        else:
            state["option_field"] = None
        await self._answer(event)
        await self._refresh(state, "Input canceled. No setting was changed.")

    async def _handle_back_action(self, event: Any, _source: Any,
                                  state: dict[str, Any], _payload: str) -> None:
        self._pending.pop(state["identity"], None)
        state["pending_field"] = None
        state["pending_expires_at"] = None
        frame = state["navigation"].pop() or {"section": "home", "page": 1}
        state["section"] = frame.get("section", "home")
        state["page"] = frame.get("page", 1)
        state["inventory_node"] = frame.get("inventory_node", 0)
        await self._answer(event)
        await self._refresh(state)

    async def _handle_inventory_action(self, event: Any, _source: Any,
                                       state: dict[str, Any], node: str) -> None:
        from .setup_cli_inventory import command_inventory
        if not node.isdigit() or not 0 <= int(node) < len(command_inventory()):
            raise ValueError("Unknown CLI branch")
        self._pending.pop(state["identity"], None)
        state.update(pending_field=None, pending_expires_at=None, option_field=None, confirm_selection=None)
        state["navigation"].push(setup_frame(state))
        state.update(section="inventory", inventory_node=int(node), page=1)
        await self._answer(event)
        await self._refresh(state)

    async def _handle_edit_action(self, event: Any, _source: Any,
                                  state: dict[str, Any], key: str) -> None:
        if key not in _FIELDS or _FIELDS[key]["type"] in {"bool", "option"}:
            raise ValueError("This setting cannot be edited here")
        state["section"] = _FIELDS[key]["section"]
        state["pending_field"] = key
        state["pending_expires_at"] = time.monotonic() + _INPUT_TTL
        self._pending[state["identity"]] = {
            "state_id": state["id"], "field": key,
            "expires_at": state["pending_expires_at"],
        }
        await self._answer(event)
        await self._refresh(state)

    async def _handle_toggle_action(self, event: Any, _source: Any,
                                    state: dict[str, Any], key: str) -> None:
        if key not in _FIELDS or _FIELDS[key]["type"] != "bool":
            raise ValueError("This setting cannot be toggled here")
        current = bool(self._value(self._load_config(state), key))
        if _FIELDS[key].get("confirm"):
            state["confirm_toggle"] = (key, not current)
            await self._answer(event)
            await self._refresh(state)
            return
        await self._answer(event, "Saving setting…")
        await self._refresh(state, "Working…")
        actual = self._save_value(state, key, not current)
        await self._refresh(state, f"Saved and read back: {self._display_value(actual)}")

    async def _handle_cycle_action(self, event: Any, _source: Any,
                                   state: dict[str, Any], key: str) -> None:
        field = _FIELDS.get(key)
        if not field or field["type"] != "option" or not field.get("options"):
            raise ValueError("This setting does not have selectable options")
        self._pending.pop(state["identity"], None)
        state["pending_field"] = None
        state["pending_expires_at"] = None
        state["option_field"] = key
        state["confirm_selection"] = None
        await self._answer(event)
        await self._refresh(state)

    async def _handle_select_action(self, event: Any, source: Any,
                                    state: dict[str, Any], index: str) -> None:
        field = _FIELDS.get(state.get("option_field"))
        if not field or not index.isdigit() or not 0 <= int(index) < len(field["options"]):
            raise ValueError("This option is unavailable")
        state["confirm_selection"] = int(index)
        if field.get("confirm"):
            await self._answer(event)
            await self._refresh(state)
        else:
            await self._handle_apply_action(event, source, state, "selection")

    async def _handle_apply_action(self, event: Any, _source: Any,
                                   state: dict[str, Any], payload: str) -> None:
        if payload == "toggle" and state.get("confirm_toggle"):
            key, value = state["confirm_toggle"]
            if key not in _FIELDS or _FIELDS[key]["type"] != "bool" or not _FIELDS[key].get("confirm"):
                raise ValueError("This confirmation is unavailable")
            await self._answer(event)
            await self._refresh(state, "Working…")
            actual = self._save_value(state, key, value)
            state["confirm_toggle"] = None
            await self._refresh(state, f"Saved and read back: {self._display_value(actual)}")
            return
        field = _FIELDS.get(state.get("option_field"))
        index = state.get("confirm_selection")
        if payload != "selection" or not field or index is None:
            raise ValueError("There is no pending selection")
        selected = field["options"][index][0]
        await self._answer(event)
        await self._refresh(state, "Working…")
        actual = self._save_value(state, state["option_field"], selected)
        state["confirm_selection"] = None
        await self._refresh(state, f"Saved and read back: {self._option_label(field, actual)}")

    async def _handle_page_action(self, event: Any, _source: Any,
                                  state: dict[str, Any], page_text: str) -> None:
        if not page_text.isdigit():
            raise ValueError("Invalid setup page")
        page = int(page_text)
        if not 1 <= page <= self._page_count(state["section"], state.get("inventory_node", 0)):
            raise ValueError("Setup page is out of range")
        state["page"] = page
        await self._answer(event)
        await self._refresh(state)

    async def _handle_navigation_action(self, event: Any, source: Any,
                                        state: dict[str, Any], command: str) -> None:
        if command not in {item for _, item in _HUB_COMMANDS}:
            raise ValueError("Unknown control surface")
        await self._answer(event)
        dispatcher = getattr(self.adapter, "_dispatch_control_command", None)
        if not callable(dispatcher):
            await self.adapter.send(source.chat_id,
                                    "This control surface is not available in this adapter yet.",
                                    reply_to=getattr(event, "message_id", None))
            return
        # The callback event lets the existing controller transfer/edit its own surface. Do not
        # refresh the rotated setup hub before handing off.
        await dispatcher(self._source_with_state_thread(source, state), command, event=event)

    def _pending_for_source(self, home: Path, source: Any):
        identity = self._identity(home, source)
        # A missing thread id is not evidence that an inbound DM belongs to an editor
        # opened in a private topic. Only an exact source identity may consume the input.
        return identity, self._pending.get(identity)

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        """Consume only timely owner input for this profile/chat/thread; let other commands route normally."""
        async with self._action_lock:
            self._prune()
            try:
                home = self._home(source)
            except Exception:
                return False
            identity, pending = self._pending_for_source(home, source)
            message_id = (getattr(event, "message_id", None)
                          or getattr(getattr(event, "message", None), "id", None))
            receipt = (*identity, str(message_id)) if message_id is not None else None
            if receipt in self._receipts and self._allowed(source):
                return True
            if not pending:
                return False
            state = self._states.get(pending["state_id"])
            if not self._allowed(source) or not state or state["pending_field"] != pending["field"]:
                self._pending.pop(identity, None)
                if state:
                    state["pending_field"] = None
                return True

            message_text = str(text or "").strip()
            command = message_text.split(maxsplit=1)[0].lower() if message_text else ""
            if command == "/cancel" or command.startswith("/cancel@"):
                self._pending.pop(identity, None)
                state["pending_field"] = None
                state["pending_expires_at"] = None
                self._rotate_state(state)
                await self._refresh(state, "Input canceled. No setting was changed.")
                return True
            from hermes_cli.commands import resolve_command
            command_name = command.lstrip("/").split("@", 1)[0]
            if command.startswith("/") and resolve_command(command_name) is not None:
                self._pending.pop(identity, None)
                state["pending_field"] = None
                state["pending_expires_at"] = None
                self._rotate_state(state)
                await self._refresh(state, "Input canceled because a command was received.")
                return False

            if receipt is not None:
                self._receipts[receipt] = time.monotonic() + _INPUT_TTL
            field = _FIELDS[pending["field"]]
            try:
                value = self._parse_value(field, message_text)
            except ValueError as exc:
                await self._refresh(state, str(exc))
                return True

            # Consume the pending input before synchronous persistence so duplicate delivery
            # cannot write twice; rotate the visible keyboard and show progress first.
            self._pending.pop(identity, None)
            state["pending_field"] = None
            state["pending_expires_at"] = None
            self._rotate_state(state)
            await self._refresh(state, "Working…")
            try:
                actual = self._save_value(state, pending["field"], value)
                await self._refresh(state, f"Saved. Readback: {self._display_value(actual)}")
            except Exception as exc:
                logger.warning("Telegram setup input save failed (%s)", type(exc).__name__)
                await self._refresh(state, "The setting could not be saved; no change was confirmed.")
            return True
