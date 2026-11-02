"""Native-MTProto general-plugin manager for Telegram.

Plugin loading and enable/disable changes are profile-wide. Admitted users may
browse, while mutations require the configured owner in a private chat. Every
card is bound to its actor, profile, and exact RichMessage. Capability grants
use the same explicit consent record as the CLI.
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from plugins.platforms.telegram.mtproto_rich import inline_markup

logger = logging.getLogger(__name__)
_STATE_TTL_SECONDS = 900
_PAGE_SIZE = 10


def _compact_text(value: Any, *, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _description_text(value: Any) -> str:
    """Keep the full manifest description as readable paragraph text."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = "\n".join(re.sub(r"[\t ]+", " ", line).strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _rich_inline_button(label: str, payload: str, color: str) -> Any:
    from telethon import types

    style = {"success": "bg_success", "danger": "bg_danger", "primary": "bg_primary"}.get(color)
    return types.TextButton(
        text=types.TextPlain(text=str(label)),
        type=types.InlineButtonTypeCallback(data=str(payload).encode("ascii")),
        style=types.RichButtonStyle(**{style: True}) if style else None,
    )


class TelegramPluginManagementUI:
    """Interactive controls for installed general plugins using MTProto only."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}

    def _is_authorized(self, source: Any) -> bool:
        if (not getattr(source, "chat_id", None)
                or not str(getattr(source, "user_id", "") or "").isdecimal()):
            return False
        authorized = getattr(self.adapter, "_authorized", None)
        try:
            return bool(callable(authorized) and authorized(source))
        except Exception:
            logger.warning("Telegram plugin-menu access check failed closed", exc_info=True)
            return False

    def _owner_controls_available(self, source: Any) -> bool:
        if (not self._is_authorized(source)
                or getattr(source, "chat_type", None) not in {"dm", "private", "direct"}):
            return False
        owner = getattr(self.adapter, "_is_owner_user", None)
        try:
            return bool(callable(owner) and owner(source.user_id, source))
        except Exception:
            logger.warning("Telegram plugin owner check failed closed", exc_info=True)
            return False

    @contextmanager
    def _profile_scope(self, state: dict[str, Any]):
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(Path(state["profile_home"])):
            yield

    def _new_state(self, source: Any, profile_home: Any) -> dict[str, Any]:
        self._prune()
        self._counter += 1
        state = {
            "id": self._counter,
            "created": time.monotonic(),
            "profile_home": str(Path(profile_home).expanduser().resolve()),
            "profile": str(getattr(source, "profile", "") or ""),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
            "user_id": str(getattr(source, "user_id", "") or ""),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "owner_controls": self._owner_controls_available(source),
            "message_id": None,
            "view": "list",
            "filter": "all",
            "page": 0,
            "selected_key": "",
            "plugins": [],
            "notice": "",
            "confirm_declared": [],
            "confirm_pending": [],
        }
        self._states[state["id"]] = state
        return state

    def _prune(self) -> None:
        now = time.monotonic()
        for state_id, state in list(self._states.items()):
            if now - state.get("created", now) > _STATE_TTL_SECONDS:
                self._states.pop(state_id, None)

    def _refresh(self, state: dict[str, Any]) -> None:
        """Refresh installed general plugins without importing plugin code."""
        from hermes_cli import plugins_cmd
        enabled = plugins_cmd._get_enabled_set()
        disabled = plugins_cmd._get_disabled_set()
        rows = []
        for name, version, description, source, path, key in plugins_cmd._discover_all_plugins():
            status = plugins_cmd._plugin_status(name, enabled, disabled, key=key)
            rows.append({
                "name": str(name),
                "version": str(version or ""),
                "description": str(description or ""),
                "source": str(source or ""),
                "key": str(key or name),
                "status": status,
            })
        state["plugins"] = sorted(rows, key=lambda row: row["key"].casefold())
        state["page"] = min(int(state.get("page", 0) or 0), max(0, (len(self._filtered(state)) - 1) // _PAGE_SIZE))

    @staticmethod
    def _filtered(state: dict[str, Any]) -> list[dict[str, Any]]:
        wanted = str(state.get("filter") or "all")
        rows = state.get("plugins", [])
        if wanted == "enabled":
            return [row for row in rows if row["status"] == "enabled"]
        if wanted == "disabled":
            return [row for row in rows if row["status"] == "disabled"]
        if wanted == "not-enabled":
            return [row for row in rows if row["status"] != "enabled"]
        return list(rows)

    @staticmethod
    def _selected(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        key = str(state.get("selected_key") or "")
        return next((row for row in state.get("plugins", []) if row["key"] == key), None)

    @staticmethod
    def _button(state: dict[str, Any], action: str, value: str = "") -> str:
        return f"pl:{state['id']}:{action}" + (f":{value}" if value else "")

    def _render(self, state: dict[str, Any]) -> tuple[Any, Any]:
        plugins = state.get("plugins", [])
        enabled_count = sum(row["status"] == "enabled" for row in plugins)
        disabled_count = sum(row["status"] == "disabled" for row in plugins)
        not_enabled_count = len(plugins) - enabled_count - disabled_count
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)

        def append_notice(lines: list[str]) -> None:
            if notice:
                lines.extend(["", f"**Notice:** {notice}"])

        if state.get("view") == "closed":
            return (
                "# Plugin controls closed\n\nThis menu is closed. Open /plugins to browse installed plugins.",
                None,
            )

        if state.get("view") == "confirm-enable":
            plugin = self._selected(state)
            if plugin is None:
                state["view"] = "list"
                state["notice"] = "Plugin details expired; choose the plugin again."
                return self._render(state)
            lines = [
                f"# Enable {_compact_text(plugin['name'], limit=96)}?",
                "Review the requested capabilities before enabling this plugin.",
                "---",
            ]
            from hermes_cli.plugin_capabilities import CAPABILITY_REGISTRY
            for capability in state.get("confirm_pending", []):
                spec = CAPABILITY_REGISTRY.get(capability)
                description = _compact_text(
                    spec.description if spec else "Declared plugin capability.", limit=220,
                )
                lines.append(f"- {capability} — Not granted: {description}")
            lines.extend([
                "",
                "Plugins run in-process and are not sandboxed. Grant only if you trust this plugin. "
                "Without a grant, the related capability gates remain off.",
            ])
            append_notice(lines)
            keyboard.append([("Grant & enable", self._button(state, "grant"), "success")])
            keyboard.append([
                ("Enable without grants", self._button(state, "enable-no-grants"), "primary"),
                ("Cancel", self._button(state, "cancel"), "danger"),
            ])
            return "\n".join(lines), inline_markup(keyboard)

        if state.get("view") == "detail":
            from telethon import types

            plugin = self._selected(state)
            if plugin is None:
                state["view"] = "list"
                state["notice"] = "Plugin details expired; choose the plugin again."
                return self._render(state)
            enabled = plugin["status"] == "enabled"
            status = _rich_inline_button(
                "ENABLED" if enabled else "DISABLED", self._button(state, "status"),
                "success" if enabled else "danger",
            )
            blocks = [
                types.PageBlockHeading1(text=types.TextPlain(text=_compact_text(plugin["name"], limit=96))),
                types.PageBlockParagraph(text=types.TextPlain(
                    text=_description_text(plugin.get("description"))
                    or "No description provided in the plugin manifest."
                )),
                types.PageBlockDivider(),
                types.PageBlockParagraph(text=types.TextConcat(texts=[
                    types.TextBold(text=types.TextPlain(text="Status: ")),
                    status,
                ])),
                types.PageBlockParagraph(text=types.TextConcat(texts=[
                    types.TextBold(text=types.TextPlain(text="Type: ")),
                    types.TextPlain(text="General plugin"),
                ])),
                types.PageBlockParagraph(text=types.TextConcat(texts=[
                    types.TextBold(text=types.TextPlain(text="Source: ")),
                    types.TextPlain(text=_compact_text(plugin.get("source"), limit=96)),
                ])),
            ]
            if plugin.get("version"):
                blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                    types.TextBold(text=types.TextPlain(text="Version: ")),
                    types.TextPlain(text=_compact_text(plugin["version"], limit=64)),
                ])))

            with self._profile_scope(state):
                from hermes_cli import plugins_cmd
                from hermes_cli.plugin_capabilities import CAPABILITY_REGISTRY, plugin_capability_granted
                declared = plugins_cmd._declared_capabilities_for_key(plugin["key"])
                capability_rows = []
                for capability in declared:
                    granted = plugin_capability_granted(plugin["key"], capability)
                    spec = CAPABILITY_REGISTRY.get(capability)
                    purpose = _compact_text(
                        spec.description if spec else "Declared plugin capability.", limit=220,
                    )
                    capability_rows.append(types.PageListItemText(text=types.TextPlain(
                        text=f"{capability} — {'Granted' if granted else 'Not granted'}: {purpose}"
                    )))
                config = plugins_cmd._config_value("plugins", "entries", plugin["key"], default={})
            if capability_rows:
                blocks.extend([
                    types.PageBlockHeading2(text=types.TextPlain(text="Capabilities")),
                    types.PageBlockList(items=capability_rows),
                ])

            entry = config if isinstance(config, dict) else {}
            if entry.get("allow_tool_override") is True:
                blocks.append(types.PageBlockParagraph(text=types.TextPlain(
                    text="This plugin is allowed to override built-in tools."
                )))
            if not state.get("owner_controls"):
                blocks.append(types.PageBlockParagraph(text=types.TextPlain(
                    text="Only the configured owner in a private chat can change shared plugin settings."
                )))
            if notice:
                blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                    types.TextBold(text=types.TextPlain(text="Notice: ")),
                    types.TextPlain(text=notice),
                ])))
            if state.get("owner_controls"):
                if enabled:
                    keyboard.append([("Disable", self._button(state, "disable"), "danger")])
                else:
                    keyboard.append([("Enable", self._button(state, "enable"), "success")])
            keyboard.append([("Back to plugins", self._button(state, "back"), "primary"),
                             ("Close", self._button(state, "close"), "danger")])
            return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

        current_filter = str(state.get("filter") or "all")
        page_items = self._filtered(state)
        page_count = max(1, (len(page_items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(0, int(state.get("page", 0) or 0)), page_count - 1)
        state["page"] = page
        from telethon import types

        blocks = [
            types.PageBlockHeading1(text=types.TextPlain(text="General plugins")),
            types.PageBlockParagraph(text=types.TextPlain(
                text="Installed plugins for this profile. Select a name below to review its description and status."
            )),
            types.PageBlockDivider(),
        ]
        filter_values = [
            (f"{len(plugins)} installed", "primary"),
            (f"{enabled_count} enabled", "success"),
            (f"{disabled_count} disabled", "danger"),
            (f"{not_enabled_count} not enabled", "danger"),
        ]
        count_texts = []
        for index, (label, color) in enumerate(filter_values):
            if index:
                count_texts.append(types.TextPlain(text=" · "))
            count_texts.append(_rich_inline_button(label, self._button(state, "status"), color))
        blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=count_texts)))
        blocks.append(types.PageBlockDivider())
        start = page * _PAGE_SIZE
        visible = page_items[start:start + _PAGE_SIZE]
        table_rows = [types.PageTableRow(cells=[
            types.PageTableCell(text=types.TextPlain(text="Plugin"), header=True),
            types.PageTableCell(text=types.TextPlain(text="Status"), header=True),
        ])]
        for index, plugin in enumerate(visible, start=start):
            enabled = plugin["status"] == "enabled"
            table_rows.append(types.PageTableRow(cells=[
                types.PageTableCell(text=types.TextPlain(text=_compact_text(plugin["name"], limit=96))),
                types.PageTableCell(text=_rich_inline_button(
                    "enabled" if enabled else "Not enabled", self._button(state, "status"),
                    "success" if enabled else "danger",
                )),
            ]))
        blocks.append(types.PageBlockTable(
            title=types.TextEmpty(), rows=table_rows, bordered=True, striped=True,
        ))
        if not visible:
            blocks.append(types.PageBlockParagraph(text=types.TextPlain(text="No plugins match this filter.")))
        for offset in range(0, len(visible), 2):
            keyboard.append([
                (_compact_text(plugin["name"], limit=32),
                 self._button(state, "detail", str(start + index)), None)
                for index, plugin in enumerate(visible[offset:offset + 2], start=offset)
            ])
        if notice:
            blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextBold(text=types.TextPlain(text="Notice: ")),
                types.TextPlain(text=notice),
            ])))
        blocks.append(types.PageBlockDivider())
        blocks.append(types.PageBlockParagraph(text=types.TextItalic(text=types.TextPlain(
            text=f"Page {page + 1} of {page_count} • total {len(page_items)}"
        ))))
        filter_row = [
            (label, self._button(state, "filter", value), "primary" if current_filter == value else None)
            for label, value in (("All", "all"), ("Enabled", "enabled"), ("Disabled", "disabled"),
                                 ("Not enabled", "not-enabled"))
        ]
        keyboard.append(filter_row)
        navigation = []
        if page:
            navigation.append(("‹ Previous", self._button(state, "page", str(page - 1)), "primary"))
        if page + 1 < page_count:
            navigation.append(("Next ›", self._button(state, "page", str(page + 1)), "primary"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([("Refresh", self._button(state, "refresh"), None),
                         ("Close", self._button(state, "close"), "danger")])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    @staticmethod
    def _message_id(adapter: Any, result: Any) -> Optional[str]:
        getter = getattr(adapter, "_sent_message_id", None)
        if callable(getter):
            try:
                value = getter(result)
                if value is not None:
                    return str(value)
            except Exception:
                pass
        for obj in (getattr(result, "message", None), result):
            value = getattr(obj, "id", None)
            if value is not None:
                return str(value)
        return None

    async def _send(self, source: Any, state: dict[str, Any], reply_to: Any = None) -> None:
        sender = getattr(self.adapter, "_send_rich_ui", None)
        if not callable(sender):
            raise RuntimeError("Telegram RichMessage sender is unavailable")
        text, markup = self._render(state)
        result = await sender(source, text, markup, reply_to=reply_to)
        state["message_id"] = self._message_id(self.adapter, result)
        if not state["message_id"]:
            raise RuntimeError("Telegram did not confirm the plugin-menu message")

    async def _edit(self, event: Any, source: Any, state: dict[str, Any]) -> None:
        editor = getattr(self.adapter, "_edit_rich_ui_message", None)
        if not callable(editor):
            raise RuntimeError("Telegram RichMessage editor is unavailable")
        text, markup = self._render(state)
        if event is None:
            from types import SimpleNamespace
            event = SimpleNamespace(message_id=state.get("message_id"))
        await editor(event, source, text, markup)

    def _bound_source(self, state: Optional[dict[str, Any]], event: Any, source: Any) -> Any:
        if not state or not self._is_authorized(source):
            return None
        expected_thread = str(state.get("thread_id") or "")
        actual_thread = str(getattr(source, "thread_id", "") or "")
        valid = (
            time.monotonic() - state.get("created", 0) <= _STATE_TTL_SECONDS
            and state.get("chat_id") == str(getattr(source, "chat_id", "") or "")
            and state.get("user_id") == str(getattr(source, "user_id", "") or "")
            and state.get("profile") == str(getattr(source, "profile", "") or "")
            and state.get("message_id") == str(getattr(event, "message_id", "") or "")
        )
        if not valid:
            return None
        if actual_thread and actual_thread != expected_thread:
            return None
        if not actual_thread and expected_thread:
            try:
                from gateway.session_identity import replace_source
                source = replace_source(source, thread_id=expected_thread)
            except (TypeError, ValueError):
                return None
        try:
            runner = getattr(self.adapter, "gateway_runner", None)
            resolver = getattr(runner, "_resolve_profile_home_for_source", None)
            if callable(resolver):
                resolved = resolver(source)
                if resolved is None or Path(resolved).expanduser().resolve() != Path(state["profile_home"]):
                    return None
        except Exception:
            return None
        return source

    async def open(self, source: Any, *, profile_home: Any, reply_to: Any = None) -> bool:
        if not self._is_authorized(source):
            return False
        state = self._new_state(source, profile_home)
        try:
            with self._profile_scope(state):
                self._refresh(state)
            await self._send(source, state, reply_to=reply_to)
            return True
        except Exception:
            self._states.pop(state["id"], None)
            logger.warning("Could not open Telegram plugin menu", exc_info=True)
            return False

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not str(data).startswith("pl:"):
            return False
        self._prune()
        parts = str(data).split(":", 3)
        if len(parts) < 3:
            await event.answer("This plugin action is unavailable.", alert=True)
            return True
        try:
            state_id = int(parts[1])
        except ValueError:
            await event.answer("This plugin action is unavailable.", alert=True)
            return True
        state = self._states.get(state_id)
        source = self._bound_source(state, event, source)
        if source is None:
            await event.answer("This plugin menu expired or belongs to another account/profile.", alert=True)
            return True
        action = parts[2]
        value = parts[3] if len(parts) > 3 else ""
        state["owner_controls"] = self._owner_controls_available(source)
        if action in {"enable", "disable", "grant", "enable-no-grants"} and not state["owner_controls"]:
            await event.answer("Only the configured owner can change shared plugin settings, in a private chat.", alert=True)
            return True
        await event.answer()
        if action == "status":
            return True
        try:
            with self._profile_scope(state):
                state["notice"] = ""
                if action == "close":
                    self._states.pop(state_id, None)
                    await self._edit(event, source, {**state, "view": "closed", "plugins": [], "notice": "Plugin menu closed."})
                    return True
                if action == "refresh":
                    self._refresh(state)
                elif action == "filter":
                    if value not in {"all", "enabled", "disabled", "not-enabled"}:
                        raise ValueError("Unknown plugin filter")
                    state["filter"] = value
                    state["page"] = 0
                elif action == "page":
                    page = int(value)
                    if page < 0:
                        raise ValueError("Invalid page")
                    state["page"] = page
                elif action == "detail":
                    index = int(value)
                    visible = self._filtered(state)
                    if index < 0 or index >= len(visible):
                        raise ValueError("This plugin list changed; refresh and choose it again.")
                    state["selected_key"] = visible[index]["key"]
                    state["view"] = "detail"
                elif action in {"back", "cancel"}:
                    state["view"] = "list"
                    state["confirm_declared"] = []
                    state["confirm_pending"] = []
                elif action == "enable":
                    if state.get("view") != "detail":
                        raise ValueError("Reopen plugin details before enabling")
                    await self._request_enable(state)
                elif action == "grant":
                    if state.get("view") != "confirm-enable" or not state.get("confirm_pending"):
                        raise ValueError("No pending capability consent is available")
                    await self._apply_enable(state, grant=True)
                elif action == "enable-no-grants":
                    if state.get("view") != "confirm-enable":
                        raise ValueError("Review the plugin confirmation before enabling")
                    await self._apply_enable(state, grant=False)
                elif action == "disable":
                    if state.get("view") != "detail":
                        raise ValueError("Reopen plugin details before disabling")
                    self._set_enabled(state, False)
                    state["notice"] = "Plugin disabled for new sessions. No gateway restart was triggered."
                else:
                    raise ValueError("Unknown plugin action")
                await self._edit(event, source, state)
        except Exception as exc:
            logger.warning("Telegram plugin-menu action failed (%s)", type(exc).__name__, exc_info=True)
            state["notice"] = f"Action failed safely ({type(exc).__name__}); no gateway restart was triggered."
            try:
                await self._edit(event, source, state)
            except Exception:
                logger.warning("Could not render Telegram plugin-menu error state", exc_info=True)
        return True

    async def _request_enable(self, state: dict[str, Any]) -> None:
        from hermes_cli import plugins_cmd
        from hermes_cli.plugin_capabilities import pending_capabilities
        plugin = self._selected(state)
        if plugin is None:
            raise ValueError("Plugin details are unavailable")
        resolved = plugins_cmd._resolve_plugin_key_and_source(plugin["key"])
        if resolved is None or resolved[0] != plugin["key"]:
            raise ValueError("Plugin is no longer installed")
        declared = plugins_cmd._declared_capabilities_for_key(plugin["key"])
        if resolved[1] != "bundled":
            config = plugins_cmd._config_value("plugins", "entries", plugin["key"], default={})
            pending = pending_capabilities(plugin["key"], declared, config={"plugins": {"entries": {plugin["key"]: config}}})
            if pending:
                state["confirm_declared"] = list(declared)
                state["confirm_pending"] = list(pending)
                state["view"] = "confirm-enable"
                return
        self._set_enabled(state, True)
        if declared and resolved[1] != "bundled":
            from hermes_cli.plugin_capabilities import record_consent
            record_consent(plugin["key"], [], declared)
        state["notice"] = "Plugin enabled for new sessions. No gateway restart was triggered."
        self._refresh(state)
        state["view"] = "detail"

    async def _apply_enable(self, state: dict[str, Any], *, grant: bool) -> None:
        from hermes_cli import plugins_cmd
        from hermes_cli.plugin_capabilities import pending_capabilities, record_consent
        plugin = self._selected(state)
        if plugin is None:
            raise ValueError("Plugin details are unavailable")
        resolved = plugins_cmd._resolve_plugin_key_and_source(plugin["key"])
        if resolved is None or resolved[0] != plugin["key"] or resolved[1] == "bundled":
            raise ValueError("Plugin changed since this confirmation; reopen its details")
        declared = plugins_cmd._declared_capabilities_for_key(plugin["key"])
        config = plugins_cmd._config_value("plugins", "entries", plugin["key"], default={})
        pending = pending_capabilities(plugin["key"], declared,
                                       config={"plugins": {"entries": {plugin["key"]: config}}})
        if grant and (declared != state.get("confirm_declared") or pending != state.get("confirm_pending")):
            state["confirm_declared"] = list(declared)
            state["confirm_pending"] = list(pending)
            state["notice"] = "The plugin's capability declaration changed. Review this updated list before granting."
            state["view"] = "confirm-enable" if pending else "detail"
            return
        if grant:
            if pending:
                record_consent(plugin["key"], pending, declared)
            elif declared:
                record_consent(plugin["key"], [], declared)
        self._set_enabled(state, True)
        state["notice"] = ("Plugin enabled and the listed capabilities granted for new sessions."
                            if grant else "Plugin enabled without granting any new capabilities.")
        state["notice"] += " No gateway restart was triggered."
        state["view"] = "detail"
        self._refresh(state)

    def _set_enabled(self, state: dict[str, Any], enabled: bool) -> None:
        """Update the profile allow/deny lists atomically, removing stale aliases."""
        from hermes_cli import plugins_cmd
        from hermes_cli.config import load_config, save_config
        plugin = self._selected(state)
        if plugin is None:
            raise ValueError("Plugin details are unavailable")
        resolved = plugins_cmd._resolve_plugin_key_and_source(plugin["key"])
        if resolved is None or resolved[0] != plugin["key"]:
            raise ValueError("Plugin is no longer installed")
        manifest_name = next((entry[0] for entry in plugins_cmd._discover_all_plugins()
                              if entry[5] == plugin["key"]), plugin["name"])
        config = load_config() or {}
        plugins = config.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            plugins = config["plugins"] = {}
        enabled_names = {value for value in plugins.get("enabled", []) if isinstance(value, str)} \
            if isinstance(plugins.get("enabled"), list) else set()
        disabled_names = {value for value in plugins.get("disabled", []) if isinstance(value, str)} \
            if isinstance(plugins.get("disabled"), list) else set()
        key = plugin["key"]
        aliases = {key, key.split("/")[-1], manifest_name, plugin["name"]}
        if enabled:
            enabled_names.add(key)
            disabled_names.difference_update(aliases)
        else:
            enabled_names.difference_update(aliases)
            disabled_names.add(key)
        plugins["enabled"] = sorted(enabled_names)
        plugins["disabled"] = sorted(disabled_names)
        save_config(config)

    async def handle_callback_dispatch(self, event: Any, source: Any, data: str) -> bool:
        """Adapter-facing callback wrapper; kept as a named seam for tests."""
        return await self.handle_callback(event, source, data)
