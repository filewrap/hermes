"""Native-MTProto general-plugin manager for Telegram.

Plugin loading and enable/disable changes are profile-wide. Admitted users may
browse, while mutations require the configured owner in a private chat. Every
card is bound to its actor, profile, and exact RichMessage. Capability grants
use the same explicit consent record as the CLI.
"""
from __future__ import annotations

import asyncio
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
# Live install progress edits are flood-gated: at most one card edit per
# window, so a fast phase sequence cannot hit Telegram's edit rate limit.
_INSTALL_EDIT_WINDOW_SECONDS = 2.0
_INSTALL_PHASE_LABELS = {
    "starting": "Starting…",
    "downloading": "Downloading…",
    "python_packages": "Installing Python packages…",
    "loading_tools": "Loading tools…",
}


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


def _rich_inline_button(label: str, payload: str, color: Optional[str]) -> Any:
    from telethon import types

    style = {"success": "bg_success", "danger": "bg_danger", "primary": "bg_primary"}.get(color or "")
    return types.TextButton(
        text=types.TextPlain(text=str(label)),
        type=types.InlineButtonTypeCallback(data=str(payload).encode("ascii")),
        style=types.RichButtonStyle(**{style: True}) if style else None,
    )


def _url_inline_button(label: str, url: str) -> Any | None:
    """An in-body URL button, or None when the URL is not a plain https link."""
    from urllib.parse import urlsplit

    from telethon import types

    try:
        parsed = urlsplit(str(url or ""))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            return None
    except (TypeError, ValueError):
        return None
    return types.TextButton(
        text=types.TextPlain(text=_compact_text(str(label), limit=64)),
        type=types.InlineButtonTypeUrl(url=str(url)),
    )


def _github_owner_url(repo: str) -> str | None:
    """Owner profile URL for a github.com repo, else None."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(repo or ""))
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        return f"https://github.com/{parts[0]}"
    except (TypeError, ValueError):
        return None


def _short_repo_label(repo: str) -> str:
    """Short label for a repo URL: owner/repo for GitHub, else the URL itself."""
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(repo or ""))
        if (parsed.hostname or "").lower() == "github.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
    except (TypeError, ValueError):
        pass
    return str(repo or "")


class TelegramPluginManagementUI:
    """Interactive controls for installed and catalog general plugins using MTProto only."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._pending_inputs: dict[tuple[str, str, str, str], dict[str, Any]] = {}

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

    @staticmethod
    def _user_key(source: Any) -> tuple[str, str, str, str]:
        return (
            str(getattr(source, "profile", "") or ""),
            str(getattr(source, "chat_id", "") or ""),
            str(getattr(source, "user_id", "") or ""),
            str(getattr(source, "thread_id", "") or ""),
        )

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
            "query": "",
            "category": "all",
            "selected_catalog_name": "",
            "setup_var": "",
            "uninstall_target": "",
            "visible_catalog": [],
            "visible_search": [],
        }
        self._states[state["id"]] = state
        return state

    def _prune(self) -> None:
        now = time.monotonic()
        for state_id, state in list(self._states.items()):
            if now - state.get("created", now) > _STATE_TTL_SECONDS:
                self._states.pop(state_id, None)
        for key, pending in list(self._pending_inputs.items()):
            if now - pending.get("created", now) > _STATE_TTL_SECONDS:
                self._pending_inputs.pop(key, None)

    async def _delete_incoming(self, source: Any, event: Any = None) -> None:
        msg_id = (
            getattr(source, "message_id", None)
            or getattr(getattr(event, "message", None), "id", None)
            or getattr(event, "message_id", None)
            or getattr(event, "id", None)
        )
        delete = getattr(self.adapter, "delete_message", None)
        if msg_id and callable(delete):
            try:
                await delete(str(getattr(source, "chat_id", "")), str(msg_id))
            except Exception:
                logger.debug("Failed to delete incoming plugin-menu message", exc_info=True)

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

    def _catalog_all(self, state: dict[str, Any]) -> list[Any]:
        cached = state.get("_catalog_cache")
        if isinstance(cached, list) and cached:
            return cached
        from hermes_cli.plugin_catalog import load_catalog_live
        entries = load_catalog_live() or []
        state["_catalog_cache"] = entries
        return entries

    def _catalog_filtered(self, state: dict[str, Any]) -> list[Any]:
        entries = self._catalog_all(state)
        maintainer = str(state.get("maintainer") or "").strip().casefold()
        if maintainer:
            entries = [e for e in entries if str(getattr(e, "maintainer", "")).casefold() == maintainer]
        category = str(state.get("category") or "all").casefold()
        if category in {"", "all"}:
            return entries
        return [e for e in entries if str(getattr(e, "category", "")).casefold() == category]

    def _search_plugins(self, state: dict[str, Any], query: str) -> list[dict[str, Any]]:
        query_cf = query.strip().casefold()
        if not query_cf:
            return []
        results: list[dict[str, Any]] = []
        seen = set()
        for row in state.get("plugins", []):
            name = str(row.get("name", ""))
            desc = str(row.get("description", ""))
            if query_cf in name.casefold() or query_cf in desc.casefold():
                results.append({
                    "name": name,
                    "title": name,
                    "version": row.get("version", ""),
                    "description": desc,
                    "is_installed": True,
                    "status": row.get("status", "installed"),
                    "key": row.get("key", name),
                    "catalog_name": name,
                    "source": row.get("source", "user"),
                })
                seen.add(name.casefold())
        for entry in self._catalog_all(state):
            ename = str(entry.name)
            etitle = str(getattr(entry, "title", "") or "")
            edesc = str(getattr(entry, "description", "") or "")
            ecat = str(getattr(entry, "category", "") or "")
            if (query_cf in ename.casefold()
                    or query_cf in etitle.casefold()
                    or query_cf in edesc.casefold()
                    or query_cf in ecat.casefold()):
                if ename.casefold() not in seen:
                    results.append({
                        "name": ename,
                        "title": etitle or ename,
                        "version": getattr(entry, "version", ""),
                        "description": edesc,
                        "is_installed": False,
                        "status": "catalog",
                        "key": ename,
                        "catalog_name": ename,
                        "source": "catalog",
                        "category": ecat,
                    })
                    seen.add(ename.casefold())
        return results

    def _get_plugin_env_status(self, state: dict[str, Any], name_or_key: str) -> list[dict[str, Any]]:
        with self._profile_scope(state):
            from hermes_cli.config import get_env_value
            from hermes_cli import plugins_cmd
            manifest: dict[str, Any] = {}
            for n, _ver, _desc, _src, path, key in plugins_cmd._discover_all_plugins():
                if key == name_or_key or n == name_or_key:
                    if path and Path(path).is_dir():
                        manifest = plugins_cmd._read_manifest(Path(path))
                        env_ex = Path(path) / ".env.example"
                        if env_ex.is_file():
                            try:
                                for line in env_ex.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                                    line = line.strip()
                                    if line and not line.startswith("#") and "=" in line:
                                        k = line.split("=", 1)[0].replace("export ", "").strip()
                                        if k:
                                            req = manifest.setdefault("requires_env", [])
                                            if k not in req and not any(isinstance(x, dict) and x.get("name") == k for x in req):
                                                req.append(k)
                            except Exception:
                                logger.debug("Failed to read plugin .env.example for %s", name_or_key, exc_info=True)
                    break

            env_names: list[str] = []
            if manifest:
                for item in manifest.get("requires_env") or []:
                    if isinstance(item, str):
                        env_names.append(item)
                    elif isinstance(item, dict) and item.get("name"):
                        env_names.append(item["name"])
            else:
                from hermes_cli.plugin_catalog import get_live_catalog_entry, get_catalog_entry
                entry = get_live_catalog_entry(name_or_key) or get_catalog_entry(name_or_key)
                if entry and getattr(entry, "capabilities", None):
                    for item in getattr(entry.capabilities, "requires_env", []) or []:
                        if isinstance(item, str):
                            env_names.append(item)
                        elif isinstance(item, dict) and item.get("name"):
                            env_names.append(item["name"])

            results = []
            for var_name in dict.fromkeys(env_names):
                val = get_env_value(var_name)
                is_set = bool(val and str(val).strip())
                results.append({"name": var_name, "is_set": is_set})
            return results

    def _install_catalog_plugin(self, state: dict[str, Any], entry_name: str,
                                on_step: Any = None) -> dict[str, Any]:
        with self._profile_scope(state):
            from hermes_cli.plugins_cmd import dashboard_install_plugin
            return dashboard_install_plugin(
                identifier="",
                force=False,
                enable=True,
                catalog_name=entry_name,
                assume_deps_consent=True,
                on_step=on_step,
            )

    def _uninstall_plugin(self, state: dict[str, Any], plugin_name: str) -> dict[str, Any]:
        with self._profile_scope(state):
            from hermes_cli.plugins_cmd import dashboard_remove_user_plugin
            return dashboard_remove_user_plugin(plugin_name)

    def _render(self, state: dict[str, Any]) -> tuple[Any, Any]:
        view = state.get("view", "list")
        if view == "closed":
            return "# Plugin controls closed\n\nThis menu is closed. Open /plugins to browse installed plugins.", None
        if view == "confirm-enable":
            return self._render_confirm_enable(state)
        if view == "detail":
            return self._render_detail(state)
        if view == "catalog":
            return self._render_catalog(state)
        if view == "catalog_detail":
            return self._render_catalog_detail(state)
        if view == "search":
            return self._render_search(state)
        if view == "setup":
            return self._render_setup(state)
        if view == "setup_input":
            return self._render_setup_input(state)
        if view == "search_input":
            return self._render_search_input(state)
        if view == "confirm_install":
            return self._render_confirm_install(state)
        if view == "installing":
            return self._render_installing(state)
        if view == "confirm_uninstall":
            return self._render_confirm_uninstall(state)
        return self._render_list(state)

    def _render_confirm_enable(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
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
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])
        keyboard.append([("Grant & enable", self._button(state, "grant"), "success")])
        keyboard.append([
            ("Enable without grants", self._button(state, "enable-no-grants"), "primary"),
            ("Cancel", self._button(state, "cancel"), "danger"),
        ])
        return "\n".join(lines), inline_markup(keyboard)

    def _render_detail(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
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

        env_status = self._get_plugin_env_status(state, plugin["key"])
        if env_status:
            env_items = []
            for item in env_status:
                s_label = "Configured" if item["is_set"] else "Missing"
                env_items.append(types.PageListItemText(text=types.TextPlain(
                    text=f"{item['name']}: {s_label}"
                )))
            blocks.extend([
                types.PageBlockHeading2(text=types.TextPlain(text="Environment Variables")),
                types.PageBlockList(items=env_items),
            ])

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
            if env_status:
                keyboard.append([("Configure Secrets", self._button(state, "setup", plugin["name"]), "primary")])
            if plugin.get("source") == "user":
                keyboard.append([("Uninstall Plugin", self._button(state, "c_uninst", plugin["name"]), "danger")])
        keyboard.append([("Back to plugins", self._button(state, "back"), "primary"),
                         ("Close", self._button(state, "close"), "danger")])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    def _render_list(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types
        plugins = state.get("plugins", [])
        enabled_count = sum(row["status"] == "enabled" for row in plugins)
        disabled_count = sum(row["status"] == "disabled" for row in plugins)
        not_enabled_count = len(plugins) - enabled_count - disabled_count
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)

        current_filter = str(state.get("filter") or "all")
        page_items = self._filtered(state)
        page_count = max(1, (len(page_items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(0, int(state.get("page", 0) or 0)), page_count - 1)
        state["page"] = page

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
        for plugin in visible:
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
                (_compact_text(p["name"], limit=32),
                 self._button(state, "detail", str(start + idx)),
                 "success" if current_filter == "all" and p["status"] == "enabled" else None)
                for idx, p in enumerate(visible[offset:offset + 2], start=offset)
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
        keyboard.append([
            ("Browse Catalog (975+)", self._button(state, "cat"), "primary"),
            ("Search Plugins", self._button(state, "s_start"), None),
        ])
        keyboard.append([("Refresh", self._button(state, "refresh"), None),
                         ("Close", self._button(state, "close"), "danger")])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    def _render_catalog(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
        current_cat = str(state.get("category") or "all")
        items = self._catalog_filtered(state)
        page_count = max(1, (len(items) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(0, int(state.get("page", 0) or 0)), page_count - 1)
        state["page"] = page
        start = page * _PAGE_SIZE
        visible = items[start:start + _PAGE_SIZE]
        state["visible_catalog"] = [entry.name for entry in visible]

        blocks = [
            types.PageBlockHeading1(text=types.TextPlain(text="Plugin Catalog (975+)")),
            types.PageBlockParagraph(text=types.TextPlain(
                text=f"Browse verified plugins and tools. Category: {current_cat.title()} ({len(items)} plugins)."
            )),
            types.PageBlockDivider(),
        ]
        maintainer_filter = str(state.get("maintainer") or "").strip()
        if maintainer_filter:
            blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextBold(text=types.TextPlain(text="Maintainer: ")),
                types.TextPlain(text=_compact_text(maintainer_filter, limit=64)),
            ])))
            keyboard.append([("Clear maintainer filter", self._button(state, "cat_mc"), None)])
        table_rows = [types.PageTableRow(cells=[
            types.PageTableCell(text=types.TextPlain(text="Plugin"), header=True),
            types.PageTableCell(text=types.TextPlain(text="Category"), header=True),
        ])]
        for entry in visible:
            title_text = entry.title or entry.name
            cat_text = entry.category or "general"
            table_rows.append(types.PageTableRow(cells=[
                types.PageTableCell(text=types.TextPlain(text=_compact_text(title_text, limit=48))),
                types.PageTableCell(text=types.TextPlain(text=_compact_text(cat_text, limit=20))),
            ]))
        blocks.append(types.PageBlockTable(
            title=types.TextEmpty(), rows=table_rows, bordered=True, striped=True,
        ))
        if not visible:
            blocks.append(types.PageBlockParagraph(text=types.TextPlain(text="No plugins found in this category.")))

        for offset in range(0, len(visible), 2):
            keyboard.append([
                (_compact_text(entry.title or entry.name, limit=28),
                 self._button(state, "cat_d", str(start + idx)), None)
                for idx, entry in enumerate(visible[offset:offset + 2], start=offset)
            ])
        if notice:
            blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextBold(text=types.TextPlain(text="Notice: ")),
                types.TextPlain(text=notice),
            ])))
        blocks.append(types.PageBlockDivider())
        blocks.append(types.PageBlockParagraph(text=types.TextItalic(text=types.TextPlain(
            text=f"Page {page + 1} of {page_count} • total {len(items)}"
        ))))
        cat_row_1 = [
            (label, self._button(state, "cat_f", value), "primary" if current_cat == value else None)
            for label, value in (("All", "all"), ("Desktop", "desktop"), ("Tools", "tools"), ("Platform", "platform"))
        ]
        cat_row_2 = [
            (label, self._button(state, "cat_f", value), "primary" if current_cat == value else None)
            for label, value in (("Models", "models"), ("Web", "web"), ("Memory", "memory"), ("Voice", "voice"))
        ]
        keyboard.append(cat_row_1)
        keyboard.append(cat_row_2)

        navigation = []
        if page:
            navigation.append(("‹ Previous", self._button(state, "cat_p", str(page - 1)), "primary"))
        if page + 1 < page_count:
            navigation.append(("Next ›", self._button(state, "cat_p", str(page + 1)), "primary"))
        if navigation:
            keyboard.append(navigation)

        keyboard.append([
            ("Search Catalog", self._button(state, "s_start"), "primary"),
            ("Installed Plugins", self._button(state, "inst_list"), None),
        ])
        keyboard.append([("Refresh", self._button(state, "refresh"), None),
                         ("Close", self._button(state, "close"), "danger")])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    def _render_catalog_detail(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        entry_name = str(state.get("selected_catalog_name") or "")
        from hermes_cli.plugin_catalog import get_live_catalog_entry, get_catalog_entry
        entry = get_live_catalog_entry(entry_name) or get_catalog_entry(entry_name)
        if entry is None:
            state["view"] = "catalog"
            state["notice"] = f"Catalog plugin '{entry_name}' not found."
            return self._render(state)

        installed_row = next((r for r in state.get("plugins", [])
                             if r["name"] == entry.name or r["key"] == entry.name or r["key"] == f"user/{entry.name}"), None)
        env_status = self._get_plugin_env_status(state, entry.name)
        notice = _compact_text(state.get("notice"), limit=240)

        blocks = [
            types.PageBlockHeading1(text=types.TextPlain(text=_compact_text(entry.title or entry.name, limit=96))),
            types.PageBlockParagraph(text=types.TextPlain(
                text=_description_text(entry.description) or "No description provided."
            )),
            types.PageBlockDivider(),
        ]

        def _cell(text: str) -> types.PageTableCell:
            return types.PageTableCell(text=types.TextPlain(text=text))

        def _label(text: str) -> types.PageTableCell:
            return types.PageTableCell(text=types.TextBold(text=types.TextPlain(text=text)))

        rows = [
            (_label("Category"), _cell(_compact_text(entry.category or "general", limit=32))),
            (_label("Version"), _cell(_compact_text(entry.version or "pinned", limit=32))),
            (_label("Tier"), _cell(_compact_text(entry.tier or "community", limit=32))),
        ]
        maintainer = str(entry.maintainer or "").strip()
        if maintainer:
            maintainer_parts: list[Any] = []
            maintainer_button = _url_inline_button(maintainer, _github_owner_url(entry.repo) or "")
            if maintainer_button is not None:
                maintainer_parts.append(maintainer_button)
            else:
                maintainer_parts.append(types.TextPlain(text=maintainer))
            if maintainer.isascii():
                maintainer_parts.append(types.TextPlain(text=" "))
                maintainer_parts.append(_rich_inline_button(
                    "Show all", self._button(state, "cat_m", maintainer), "primary",
                ))
            maintainer_cell = types.PageTableCell(text=types.TextConcat(texts=maintainer_parts))
        else:
            maintainer_cell = _cell("—")
        rows.append((_label("Maintainer"), maintainer_cell))
        repo = str(entry.repo or "").strip()
        repo_button = _url_inline_button(_short_repo_label(repo), repo) if repo else None
        rows.append((_label("Repository"),
                     types.PageTableCell(text=repo_button) if repo_button is not None else _cell(repo or "—")))
        if installed_row:
            status_cell = types.PageTableCell(text=_rich_inline_button(
                f"Installed ({installed_row['status']})", self._button(state, "status"),
                "success" if installed_row["status"] == "enabled" else "danger",
            ))
        else:
            status_cell = types.PageTableCell(text=_rich_inline_button(
                "Not installed", self._button(state, "status"), "danger",
            ))
        rows.append((_label("Status"), status_cell))
        if getattr(entry, "requires_hermes", ""):
            rows.append((_label("Requires Hermes"), _cell(_compact_text(entry.requires_hermes, limit=48))))
        platforms = [str(p) for p in (getattr(entry, "platforms", None) or [])]
        if platforms:
            rows.append((_label("Platforms"), _cell(_compact_text(", ".join(platforms), limit=96))))
        if getattr(entry, "subdir", ""):
            rows.append((_label("Subdirectory"), _cell(_compact_text(entry.subdir, limit=96))))
        sha = str(getattr(entry, "sha", "") or "").strip()
        if sha:
            rows.append((_label("Pinned commit"), _cell(sha[:7])))
        docs_url = str(getattr(entry, "docs_url", "") or "").strip()
        if docs_url:
            docs_button = _url_inline_button("Open docs", docs_url)
            rows.append((_label("Docs"),
                         types.PageTableCell(text=docs_button) if docs_button is not None else _cell(docs_url)))
        caps = getattr(entry, "capabilities", None)
        if caps and getattr(caps, "provides_tools", None):
            rows.append((_label("Tools"), _cell(_compact_text(", ".join(caps.provides_tools), limit=160))))
        if caps and getattr(caps, "provides_hooks", None):
            rows.append((_label("Hooks"), _cell(_compact_text(", ".join(caps.provides_hooks), limit=160))))
        if caps and getattr(caps, "provides_middleware", None):
            rows.append((_label("Middleware"), _cell(_compact_text(", ".join(caps.provides_middleware), limit=160))))
        blocks.append(types.PageBlockTable(
            title=types.TextEmpty(), rows=[types.PageTableRow(cells=[a, b]) for a, b in rows],
            bordered=True, striped=True,
        ))
        if env_status:
            env_items = []
            for item in env_status:
                s_label = "Configured" if item["is_set"] else "Missing"
                env_items.append(types.PageListItemText(text=types.TextPlain(
                    text=f"{item['name']}: {s_label}"
                )))
            blocks.extend([
                types.PageBlockHeading2(text=types.TextPlain(text="Environment Requirements")),
                types.PageBlockList(items=env_items),
            ])
        if getattr(entry, "known_issues", None):
            blocks.extend([
                types.PageBlockHeading2(text=types.TextPlain(text="Known Issues")),
                types.PageBlockList(items=[
                    types.PageListItemText(text=types.TextPlain(text=_compact_text(ki, limit=320)))
                    for ki in entry.known_issues
                ]),
            ])
        if notice:
            blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextBold(text=types.TextPlain(text="Notice: ")),
                types.TextPlain(text=notice),
            ])))

        if state.get("owner_controls"):
            if not installed_row:
                keyboard.append([("Install Plugin", self._button(state, "c_inst", entry.name), "success")])
            else:
                if installed_row["status"] == "enabled":
                    keyboard.append([("Disable", self._button(state, "disable_cat", entry.name), "danger")])
                else:
                    keyboard.append([("Enable", self._button(state, "enable_cat", entry.name), "success")])
                if installed_row.get("source") == "user":
                    keyboard.append([("Uninstall Plugin", self._button(state, "c_uninst", entry.name), "danger")])
            if env_status:
                keyboard.append([("Configure Secrets", self._button(state, "setup", entry.name), "primary")])

        keyboard.append([
            ("‹ Back to Catalog", self._button(state, "cdet_back"), "primary"),
            ("Close", self._button(state, "close"), "danger"),
        ])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    def _render_search(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
        query = str(state.get("query") or "")
        results = self._search_plugins(state, query)
        page_count = max(1, (len(results) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = min(max(0, int(state.get("page", 0) or 0)), page_count - 1)
        state["page"] = page
        start = page * _PAGE_SIZE
        visible = results[start:start + _PAGE_SIZE]
        state["visible_search"] = visible

        blocks = [
            types.PageBlockHeading1(text=types.TextPlain(text=f'Search: "{_compact_text(query, limit=32)}"') ),
            types.PageBlockParagraph(text=types.TextPlain(
                text=f"Found {len(results)} plugins matching '{query}'."
            )),
            types.PageBlockDivider(),
        ]
        table_rows = [types.PageTableRow(cells=[
            types.PageTableCell(text=types.TextPlain(text="Plugin"), header=True),
            types.PageTableCell(text=types.TextPlain(text="Source"), header=True),
        ])]
        for r in visible:
            tag = "Installed" if r["is_installed"] else "Catalog"
            table_rows.append(types.PageTableRow(cells=[
                types.PageTableCell(text=types.TextPlain(text=_compact_text(r["title"], limit=48))),
                types.PageTableCell(text=types.TextPlain(text=tag)),
            ]))
        blocks.append(types.PageBlockTable(
            title=types.TextEmpty(), rows=table_rows, bordered=True, striped=True,
        ))
        if not visible:
            blocks.append(types.PageBlockParagraph(text=types.TextPlain(text=f"No plugins found matching '{query}'.")))

        for offset in range(0, len(visible), 2):
            keyboard.append([
                (_compact_text(r["title"], limit=28),
                 self._button(state, "s_det", str(start + idx)),
                 "success" if r["is_installed"] else None)
                for idx, r in enumerate(visible[offset:offset + 2], start=offset)
            ])
        if notice:
            blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextBold(text=types.TextPlain(text="Notice: ")),
                types.TextPlain(text=notice),
            ])))
        blocks.append(types.PageBlockDivider())
        blocks.append(types.PageBlockParagraph(text=types.TextItalic(text=types.TextPlain(
            text=f"Page {page + 1} of {page_count} • total {len(results)}"
        ))))
        navigation = []
        if page:
            navigation.append(("‹ Previous", self._button(state, "s_p", str(page - 1)), "primary"))
        if page + 1 < page_count:
            navigation.append(("Next ›", self._button(state, "s_p", str(page + 1)), "primary"))
        if navigation:
            keyboard.append(navigation)

        keyboard.append([
            ("New Search", self._button(state, "s_start"), "primary"),
            ("Clear Search", self._button(state, "s_clear"), None),
        ])
        keyboard.append([
            ("Installed Plugins", self._button(state, "inst_list"), None),
            ("Full Catalog", self._button(state, "cat"), None),
            ("Close", self._button(state, "close"), "danger"),
        ])
        return types.InputRichMessage(blocks=blocks), inline_markup(keyboard)

    def _render_setup(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
        plugin_name = str(state.get("selected_catalog_name") or (self._selected(state) or {}).get("name") or "")
        env_status = self._get_plugin_env_status(state, plugin_name)
        lines = [
            f"# Configure {plugin_name}",
            "Environment variables for this plugin are stored securely in your profile `.env`.",
            "---",
            "**Required Variables:**",
        ]
        if not env_status:
            lines.append("No environment variables are declared for this plugin.")
        else:
            for item in env_status:
                s_icon = "Configured" if item["is_set"] else "Missing"
                lines.append(f"- `{item['name']}`: {s_icon}")
        lines.extend([
            "",
            "Tap a variable below to securely enter or update its value in chat.",
            "Messages containing secrets are immediately deleted from chat for security.",
        ])
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])

        if state.get("owner_controls"):
            for item in env_status:
                v_name = item["name"]
                lbl = f"Set {v_name[:20]}" if not item["is_set"] else f"{v_name[:18]} (Update)"
                btn_color = "primary" if not item["is_set"] else None
                keyboard.append([(lbl, self._button(state, "set_v", v_name), btn_color)])

        keyboard.append([
            ("‹ Back", self._button(state, "cdet_back" if state.get("selected_catalog_name") else "back"), "primary"),
            ("Close", self._button(state, "close"), "danger"),
        ])
        return "\n".join(lines), inline_markup(keyboard)

    def _render_setup_input(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        var_name = str(state.get("setup_var") or "")
        plugin_name = str(state.get("selected_catalog_name") or (self._selected(state) or {}).get("name") or "")
        notice = _compact_text(state.get("notice"), limit=240)
        lines = [
            f"# Enter value for `{var_name}`",
            f"Configuring `{var_name}` for plugin **{plugin_name}**.",
            "---",
            f"Please reply in this chat with the secret/value for `{var_name}`.",
            "",
            "**Security Note:** Your message containing the secret will be saved directly into `.env` "
            "and **immediately deleted from this chat**.",
            "",
            "Send `/cancel` or tap Cancel below to abort.",
        ]
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])
        keyboard.append([("Cancel", self._button(state, "c_input"), "danger")])
        return "\n".join(lines), inline_markup(keyboard)

    def _render_search_input(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        notice = _compact_text(state.get("notice"), limit=240)
        lines = [
            "# Search Plugins",
            "Please reply in this chat with your search keywords (e.g. `telegram`, `database`, `voice`, `github`).",
            "",
            "Send `/cancel` or tap Cancel below to abort.",
        ]
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])
        keyboard.append([("Cancel", self._button(state, "c_input"), "danger")])
        return "\n".join(lines), inline_markup(keyboard)

    def _render_confirm_install(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        entry_name = str(state.get("selected_catalog_name") or "")
        notice = _compact_text(state.get("notice"), limit=240)
        lines = [
            f"# Install {entry_name}?",
            f"This will download and install plugin **{entry_name}** into your active profile.",
            "---",
            "Plugins run in-process and have access to system capabilities. Only install plugins you trust.",
        ]
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])
        keyboard.append([("Confirm Install", self._button(state, "do_inst"), "success")])
        keyboard.append([("Cancel", self._button(state, "cdet_back"), "danger")])
        return "\n".join(lines), inline_markup(keyboard)

    def _render_installing(self, state: dict[str, Any]) -> tuple[Any, Any]:
        entry_name = str(state.get("selected_catalog_name") or "")
        phase = str(state.get("install_phase") or "starting")
        label = _INSTALL_PHASE_LABELS.get(phase, "Installing…")
        lines = [
            f"# Installing {entry_name}",
            label,
            "---",
            "The card updates as each step starts.",
        ]
        return "\n".join(lines), None

    def _render_confirm_uninstall(self, state: dict[str, Any]) -> tuple[Any, Any]:
        keyboard: list[list[tuple[str, str, Optional[str]]]] = []
        target = str(state.get("uninstall_target") or state.get("selected_catalog_name") or (self._selected(state) or {}).get("name") or "")
        notice = _compact_text(state.get("notice"), limit=240)
        lines = [
            f"# Uninstall {target}?",
            f"Are you sure you want to uninstall `{target}` from this profile?",
            "---",
            "This will delete the plugin directory. Configured secrets in `.env` are preserved.",
        ]
        if notice:
            lines.extend(["", f"**Notice:** {notice}"])
        keyboard.append([("Confirm Uninstall", self._button(state, "do_uninst"), "danger")])
        keyboard.append([("Cancel", self._button(state, "uninst_back"), "primary")])
        return "\n".join(lines), inline_markup(keyboard)

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

    async def open(
        self, source: Any, *, profile_home: Any, reply_to: Any = None,
        query: str = "", view: str = "list", catalog_name: Optional[str] = None,
    ) -> bool:
        if not self._is_authorized(source):
            return False
        state = self._new_state(source, profile_home)
        try:
            with self._profile_scope(state):
                self._refresh(state)
                if query:
                    state["query"] = query.strip()
                    state["view"] = "search"
                elif view == "catalog":
                    state["view"] = "catalog"
                elif catalog_name:
                    state["selected_catalog_name"] = catalog_name.strip()
                    state["view"] = "catalog_detail"
                else:
                    state["view"] = "list"
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

        mutation_actions = {"enable", "disable", "grant", "enable-no-grants", "c_inst", "do_inst",
                            "c_uninst", "do_uninst", "enable_cat", "disable_cat", "set_v"}
        if action in mutation_actions and not state["owner_controls"]:
            await event.answer("Only the configured owner can change shared plugin settings, in a private chat.", alert=True)
            return True
        await event.answer()
        if action == "status":
            return True
        try:
            with self._profile_scope(state):
                state["notice"] = ""
                await self._process_callback_action(event, source, state, action, value)
                await self._edit(event, source, state)
        except Exception as exc:
            logger.warning("Telegram plugin-menu action failed (%s)", type(exc).__name__, exc_info=True)
            state["notice"] = f"Action failed safely ({type(exc).__name__}); no gateway restart was triggered."
            try:
                await self._edit(event, source, state)
            except Exception:
                logger.warning("Could not render Telegram plugin-menu error state", exc_info=True)
        return True

    async def _process_callback_action(
        self, event: Any, source: Any, state: dict[str, Any], action: str, value: str,
    ) -> None:
        if action == "close":
            self._states.pop(state["id"], None)
            state.update({"view": "closed", "plugins": [], "notice": "Plugin menu closed."})
            return
        if action == "refresh":
            self._refresh(state)
            state.pop("_catalog_cache", None)
            return
        if action == "filter":
            if value not in {"all", "enabled", "disabled", "not-enabled"}:
                raise ValueError("Unknown plugin filter")
            state["filter"] = value
            state["page"] = 0
            return
        if action == "page":
            state["page"] = max(0, int(value))
            return
        if action == "detail":
            index = int(value)
            visible = self._filtered(state)
            if index < 0 or index >= len(visible):
                raise ValueError("This plugin list changed; refresh and choose it again.")
            state["selected_key"] = visible[index]["key"]
            state["view"] = "detail"
            return
        if action in {"back", "cancel"}:
            state["view"] = "list"
            state["confirm_declared"] = []
            state["confirm_pending"] = []
            return
        if action == "cat":
            state["view"] = "catalog"
            state["page"] = 0
            return
        if action == "cat_f":
            state["category"] = value
            state["maintainer"] = ""
            state["page"] = 0
            return
        if action == "cat_m":
            maintainer = str(value or "").strip()
            if not maintainer:
                raise ValueError("No maintainer selected")
            state["maintainer"] = maintainer
            state["category"] = "all"
            state["page"] = 0
            state["view"] = "catalog"
            return
        if action == "cat_mc":
            state["maintainer"] = ""
            state["page"] = 0
            state["view"] = "catalog"
            return
        if action == "cat_p":
            state["page"] = max(0, int(value))
            return
        if action == "cat_d":
            index = int(value)
            items = self._catalog_filtered(state)
            if index < 0 or index >= len(items):
                raise ValueError("Catalog page expired; please refresh.")
            state["selected_catalog_name"] = items[index].name
            state["view"] = "catalog_detail"
            return
        if action == "inst_list":
            state["view"] = "list"
            state["page"] = 0
            self._refresh(state)
            return
        if action == "s_start":
            state["view"] = "search_input"
            user_key = self._user_key(source)
            self._pending_inputs[user_key] = {
                "kind": "search", "state_id": state["id"], "state": state, "created": time.monotonic(),
            }
            return
        if action == "s_p":
            state["page"] = max(0, int(value))
            return
        if action == "s_det":
            index = int(value)
            results = self._search_plugins(state, str(state.get("query") or ""))
            if index < 0 or index >= len(results):
                raise ValueError("Search results expired; please search again.")
            item = results[index]
            if item["is_installed"]:
                state["selected_key"] = item["key"]
                state["view"] = "detail"
            else:
                state["selected_catalog_name"] = item["name"]
                state["view"] = "catalog_detail"
            return
        if action == "s_clear":
            state["query"] = ""
            state["view"] = "catalog"
            state["page"] = 0
            return
        if action == "c_input":
            user_key = self._user_key(source)
            self._pending_inputs.pop(user_key, None)
            state["setup_var"] = ""
            state["view"] = "setup" if state.get("selected_catalog_name") else "list"
            state["notice"] = "Input cancelled."
            return
        if action == "cdet_back":
            state["view"] = "search" if state.get("query") else "catalog"
            return
        if action == "uninst_back":
            state["view"] = "detail" if state.get("selected_key") else "catalog_detail"
            return
        if action == "c_inst":
            state["selected_catalog_name"] = value or state.get("selected_catalog_name")
            state["view"] = "confirm_install"
            return
        if action == "do_inst":
            entry_name = str(state.get("selected_catalog_name") or "")
            if not entry_name:
                raise ValueError("No catalog plugin selected for installation")
            state["install_phase"] = "starting"
            state["view"] = "installing"
            await self._edit(event, source, state)
            loop = asyncio.get_running_loop()
            last_edit = [time.monotonic()]

            def _on_step(phase: Any) -> None:
                state["install_phase"] = str(getattr(phase, "value", phase))
                now = time.monotonic()
                if now - last_edit[0] < _INSTALL_EDIT_WINDOW_SECONDS:
                    return
                last_edit[0] = now
                future = asyncio.run_coroutine_threadsafe(
                    self._edit(event, source, state), loop,
                )

                def _log_edit_failure(done: Any) -> None:
                    try:
                        exc = done.exception()
                    except Exception:
                        return
                    if exc is not None:
                        logger.warning("Telegram live-install edit failed (%s)", type(exc).__name__)

                future.add_done_callback(_log_edit_failure)

            try:
                res = await asyncio.to_thread(
                    self._install_catalog_plugin, state, entry_name, _on_step,
                )
            except Exception:
                state.pop("install_phase", None)
                state["view"] = "catalog_detail"
                raise
            state.pop("install_phase", None)
            if res.get("ok"):
                self._refresh(state)
                missing = res.get("missing_env") or []
                if missing:
                    state["notice"] = f"Installed {entry_name}! Setup required: {', '.join(missing)}"
                    state["view"] = "setup"
                else:
                    state["notice"] = f"Successfully installed and enabled {entry_name}!"
                    state["view"] = "catalog_detail"
            else:
                state["notice"] = f"Install failed: {res.get('error', 'Unknown error')}"
                state["view"] = "catalog_detail"
            return
        if action == "c_uninst":
            state["uninstall_target"] = value or state.get("selected_catalog_name") or (self._selected(state) or {}).get("name")
            state["view"] = "confirm_uninstall"
            return
        if action == "do_uninst":
            target = str(state.get("uninstall_target") or state.get("selected_catalog_name") or (self._selected(state) or {}).get("name") or "")
            if not target:
                raise ValueError("No plugin selected for uninstall")
            res = self._uninstall_plugin(state, target)
            if res.get("ok"):
                state["notice"] = f"Successfully uninstalled {target}."
                self._refresh(state)
                state["view"] = "list"
            else:
                state["notice"] = f"Uninstall failed: {res.get('error', 'Unknown error')}"
                state["view"] = "detail" if state.get("selected_key") else "catalog_detail"
            return
        if action == "setup":
            state["selected_catalog_name"] = value or state.get("selected_catalog_name") or (self._selected(state) or {}).get("name")
            state["view"] = "setup"
            return
        if action == "set_v":
            state["setup_var"] = value
            state["view"] = "setup_input"
            user_key = self._user_key(source)
            self._pending_inputs[user_key] = {
                "kind": "setup_secret", "state_id": state["id"], "state": state, "var_name": value, "created": time.monotonic(),
            }
            return
        if action == "enable_cat":
            found = next((r for r in state.get("plugins", []) if r["name"] == value or r["key"] == value), None)
            if found:
                state["selected_key"] = found["key"]
                await self._request_enable(state)
            return
        if action == "disable_cat":
            found = next((r for r in state.get("plugins", []) if r["name"] == value or r["key"] == value), None)
            if found:
                state["selected_key"] = found["key"]
                self._set_enabled(state, False)
                state["notice"] = f"Plugin {found['name']} disabled."
            return
        if action == "enable":
            if state.get("view") != "detail":
                raise ValueError("Reopen plugin details before enabling")
            await self._request_enable(state)
            return
        if action == "grant":
            if state.get("view") != "confirm-enable" or not state.get("confirm_pending"):
                raise ValueError("No pending capability consent is available")
            await self._apply_enable(state, grant=True)
            return
        if action == "enable-no-grants":
            if state.get("view") != "confirm-enable":
                raise ValueError("Review the plugin confirmation before enabling")
            await self._apply_enable(state, grant=False)
            return
        if action == "disable":
            if state.get("view") != "detail":
                raise ValueError("Reopen plugin details before disabling")
            self._set_enabled(state, False)
            state["notice"] = "Plugin disabled for new sessions. No gateway restart was triggered."
            return
        raise ValueError(f"Unknown plugin action '{action}'")

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        self._prune()
        if str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}:
            return False
        if not getattr(source, "user_id", None):
            return False
        user_key = self._user_key(source)
        pending = self._pending_inputs.get(user_key)
        if not pending:
            return False
        state = pending.get("state")
        if not isinstance(state, dict):
            self._pending_inputs.pop(user_key, None)
            return False

        value = str(text or "").strip()
        if value.casefold() in {"/cancel", "cancel"}:
            self._pending_inputs.pop(user_key, None)
            await self._delete_incoming(source, event)
            state["notice"] = "Input cancelled."
            state["view"] = "setup" if pending.get("kind") == "setup_secret" else "catalog"
            try:
                await self._edit(None, source, state)
            except Exception:
                logger.debug("Failed to refresh plugin menu after input", exc_info=True)
            return True

        if pending.get("kind") == "setup_secret":
            var_name = str(pending.get("var_name") or "")
            if not var_name:
                self._pending_inputs.pop(user_key, None)
                return False
            if not value:
                await self._delete_incoming(source, event)
                state["notice"] = "Secret value cannot be empty. Send a value or /cancel."
                try:
                    await self._edit(None, source, state)
                except Exception:
                    logger.debug("Failed to refresh plugin menu after input", exc_info=True)
                return True
            with self._profile_scope(state):
                from hermes_cli.config import save_env_value
                save_env_value(var_name, value)
            await self._delete_incoming(source, event)
            self._pending_inputs.pop(user_key, None)
            state["notice"] = f"Saved {var_name} securely in .env and deleted message."
            state["view"] = "setup"
            try:
                await self._edit(None, source, state)
            except Exception:
                logger.debug("Failed to refresh plugin menu after input", exc_info=True)
            return True

        if pending.get("kind") == "search":
            query = value[:80]
            await self._delete_incoming(source, event)
            self._pending_inputs.pop(user_key, None)
            state["query"] = query
            state["view"] = "search"
            state["page"] = 0
            state["notice"] = ""
            try:
                await self._edit(None, source, state)
            except Exception:
                logger.debug("Failed to refresh plugin menu after input", exc_info=True)
            return True

        return False

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
