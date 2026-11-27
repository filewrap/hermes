"""Owner-only Telegram UI for selecting the platform's existing toolsets.

Selection is staged in memory and applied through ``hermes_cli.tools_config`` only
when the owner confirms. The gateway rereads the selection on the next message turn.
"""
from __future__ import annotations

import logging
import math
import time
from html import escape
from pathlib import Path
from typing import Any

from .mtproto_rich import inline_markup

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 15 * 60
_PAGE_SIZE = 6
_PREFIX = "hm:setuptools:"

# These built-ins have no supported configuration flow here, require a separate
# install/auth workflow, are runtime-provided, or are explicitly opt-in integrations.
# They remain untouched if already configured; this screen never installs or probes them.
_HIDDEN_TOOLSETS = frozenset({
    "computer_use", "connections", "context_engine", "discord", "discord_admin",
    "homeassistant", "kanban", "spotify", "x_search", "yuanbao",
})


class TelegramSetupToolsUI:
    """Manage Telegram's existing standard toolsets for the authorized owner."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._drafts: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

    def _allowed(self, source: Any) -> bool:
        if (str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}
                or not getattr(source, "chat_id", None) or not getattr(source, "user_id", None)):
            return False
        authorized = getattr(self.adapter, "_authorized", None)
        owner = getattr(self.adapter, "_is_owner_user", None)
        if not callable(authorized) or not callable(owner):
            return False
        try:
            return bool(authorized(source)) and bool(owner(source.user_id, source=source))
        except Exception:
            logger.warning("Telegram tool setup authorization failed closed", exc_info=True)
            return False

    def _home(self, source: Any) -> Path:
        settings_home = getattr(self.adapter, "_settings_home", None)
        if callable(settings_home):
            home = settings_home(source)
            if home is None:
                raise RuntimeError("The active Telegram profile could not be resolved")
        else:
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

    @staticmethod
    def _identity(home: Path, source: Any) -> tuple[str, str, str, str, str]:
        return (str(home), str(getattr(source, "profile", "") or ""),
                str(getattr(source, "user_id", "") or ""),
                str(getattr(source, "chat_id", "") or ""),
                str(getattr(source, "thread_id", "") or ""))

    def _prune(self) -> None:
        now = time.monotonic()
        for state_id, state in list(self._states.items()):
            if now - state["created_at"] > _STATE_TTL_SECONDS:
                self._states.pop(state_id, None)
        for identity, state in list(self._drafts.items()):
            if now - state["created_at"] > _STATE_TTL_SECONDS:
                self._drafts.pop(identity, None)

    def _drop_state(self, state_id: int) -> None:
        """Invalidate the keyboard on departure, not an unapplied owner draft."""
        state = self._states.pop(state_id, None)
        if state and state["draft"] != state["original"]:
            self._drafts[state["identity"]] = state

    def pending_notice(self, source: Any) -> str:
        self._prune()
        if not self._allowed(source):
            return ""
        identity = self._identity(self._home(source), source)
        return self.pending_notice_for_identity(identity)

    def pending_notice_for_identity(self, identity: tuple[str, str, str, str, str]) -> str:
        """Called only with an owner-authorized setup state's complete identity."""
        self._prune()
        state = next((state for state in self._states.values() if state["identity"] == identity),
                     self._drafts.get(identity))
        if not state or state["draft"] == state["original"]:
            return ""
        count = len(state["draft"] ^ state["original"])
        return (f"Pending changes: {count} Telegram toolset changes are not saved. "
                "Review and apply or discard them before this draft expires (15 minutes after opening). "
                "After applying, the next Telegram message turn uses the saved selection, including in this "
                "conversation; the cached agent may be rebuilt. No gateway restart is required.")

    def _new_state(self, source: Any, home: Path, config: dict[str, Any], enabled: set[str]) -> dict[str, Any]:
        self._prune()
        identity = self._identity(home, source)
        for state_id, old in list(self._states.items()):
            if old["identity"] == identity:
                self._drop_state(state_id)
        self._counter += 1
        draft = self._drafts.pop(identity, None)
        if draft:
            draft.update(id=self._counter, message_id=None, change=True)
            self._states[draft["id"]] = draft
            return draft
        from hermes_cli.tools_config import CONFIGURABLE_TOOLSETS, _CONFIG_ONLY_TOOLSETS
        from hermes_cli.toolset_scope import toolset_allowed_for_platform

        catalog = [
            (key, " ".join(word for word in label.split() if any(char.isalnum() for char in word)), description)
            for key, label, description in CONFIGURABLE_TOOLSETS
            if key not in _CONFIG_ONLY_TOOLSETS and key not in _HIDDEN_TOOLSETS
            and toolset_allowed_for_platform(key, "telegram")
        ]
        raw_toolsets = (config.get("platform_toolsets") or {}).get("telegram")
        explicit = {str(item) for item in raw_toolsets} if isinstance(raw_toolsets, list) else set()
        # Keep hidden integrations exactly as currently effective (and retain
        # explicitly configured ones even if globally disabled). They are not
        # offered for editing, but applying visible choices must not silently
        # remove their existing selection.
        from hermes_cli.tools_config import _platform_default_keys
        visible_keys = {key for key, _label, _description in catalog}
        hidden_explicit = ((explicit - visible_keys - _platform_default_keys())
                           | (enabled & _HIDDEN_TOOLSETS))
        state = {
            "id": self._counter, "identity": identity, "home": str(home),
            "profile": str(getattr(source, "profile", "") or ""),
            "user_id": str(source.user_id), "chat_id": str(source.chat_id),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "message_id": None, "created_at": time.monotonic(), "page": 1,
            "catalog": catalog,
            "original": {key for key, _label, _description in catalog if key in enabled},
            "draft": {key for key, _label, _description in catalog if key in enabled},
            "hidden_explicit": hidden_explicit,
            "baseline_platform_toolsets": raw_toolsets if isinstance(raw_toolsets, list) else None,
            "change": None,
        }
        self._states[state["id"]] = state
        return state

    @staticmethod
    def _config_scope(home: str):
        from gateway.run import _profile_runtime_scope
        return _profile_runtime_scope(Path(home))

    def _load_config(self, home: str) -> dict[str, Any]:
        from hermes_cli.config import load_config
        with self._config_scope(home):
            config = load_config()
        if not isinstance(config, dict):
            raise RuntimeError("The active profile configuration could not be read")
        return config

    @staticmethod
    def _enabled_toolsets(config: dict[str, Any]) -> set[str]:
        from hermes_cli.tools_config import _get_platform_tools
        return set(_get_platform_tools(config, "telegram"))

    @staticmethod
    def _button(state: dict[str, Any], label: str, action: str, style: str | None = None):
        return (label, f"{_PREFIX}{state['id']}:{action}", style)

    @staticmethod
    def _footer(page: int, pages: int, total: int) -> str:
        return f"<hr/><p><i>Page {page} of {pages} • total {total}</i></p>"

    def _list_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        catalog = state["catalog"]
        total = len(catalog)
        pages = max(1, math.ceil(total / _PAGE_SIZE))
        page = min(max(1, int(state.get("page", 1))), pages)
        state["page"] = page
        visible = catalog[(page - 1) * _PAGE_SIZE:page * _PAGE_SIZE]
        added = state["draft"] - state["original"]
        removed = state["original"] - state["draft"]
        rows = "".join(
            f"<tr><th>{'Enabled:' if key in state['draft'] else 'Disabled:'} {escape(label)}</th>"
            f"<td><code>{escape(key)}</code> · {escape(description)}</td></tr>"
            for key, label, description in visible
        )
        profile = escape(state["profile"] or "default")
        text = (
            "<h1>Telegram toolsets</h1>"
            f"<p>Configure the standard Telegram toolsets in profile <b>{profile}</b>. "
            "This screen does not install plugins, request credentials, or probe services. Existing MCP and plugin "
            "entries are not listed or changed.</p>"
            "<p>Changes are staged until you choose <b>Apply</b>. The gateway reads saved selections on the next "
            "message turn, including in this conversation, and may rebuild the cached agent and its tool schemas. "
            "No gateway restart is required. A turn already running is not changed. Enabling tools may let "
            "the model access external data or perform actions described below.</p>"
            f"<p><b>Staged:</b> <b>{len(state['draft'])}</b> standard toolsets enabled"
            + (f" · <b>Added:</b> {escape(', '.join(sorted(added)))}" if added else "")
            + (f" · <b>Removed:</b> {escape(', '.join(sorted(removed)))}" if removed else "")
            + "</p><hr/><table><tbody>" + rows + "</tbody></table>"
        )
        if added or removed:
            text += ("<p><b>Pending changes:</b> Not saved or effective yet. Choose Review changes to inspect "
                     "the pending values, then explicitly apply for the next message turn or discard. "
                     "Returning to setup keeps this draft for up to 15 minutes after opening.</p>")
        if note:
            text += f"<hr/><p><b>Status:</b> {escape(note)}</p>"
        buttons = []
        for key, label, _description in visible:
            is_enabled = key in state["draft"]
            buttons.append([self._button(
                state, f"{'Disable' if is_enabled else 'Enable'} {label}", f"toggle:{key}",
                "success" if is_enabled else None,
            )])
        navigation = []
        if page > 1:
            navigation.append(self._button(state, f"Previous · {page - 1}/{pages}", f"page:{page - 1}"))
        if page < pages:
            navigation.append(self._button(state, f"Next · {page + 1}/{pages}", f"page:{page + 1}", "primary"))
        if navigation:
            buttons.append(navigation)
        if added or removed:
            buttons.append([self._button(state, "Review changes", "review", "primary"),
                            self._button(state, "Discard changes", "discard", "danger")])
        buttons.append([self._button(state, "Close", "close", "danger")])
        text += self._footer(page, pages, total)
        return text, inline_markup(buttons)

    def _confirm_view(self, state: dict[str, Any]) -> tuple[str, Any]:
        added = state["draft"] - state["original"]
        removed = state["original"] - state["draft"]
        if not added and not removed:
            return self._list_view(state, "There are no pending changes. Nothing was saved.")
        entries = {key: (label, description) for key, label, description in state["catalog"]}
        impact_rows = []
        for key in sorted(added):
            label, description = entries[key]
            impact_rows.append(
                f"<li><b>Enable {escape(label)}</b> (<code>{escape(key)}</code>): {escape(description)}. "
                "After applying, subsequent turns may use this capability to access external data or perform actions. Review its "
                "permissions and credentials first; no tool runs during setup.</li>"
            )
        for key in sorted(removed):
            label, description = entries[key]
            impact_rows.append(
                f"<li><b>Disable {escape(label)}</b> (<code>{escape(key)}</code>): removes {escape(description)} "
                "from the saved selection used on subsequent Telegram message turns.</li>"
            )
        text = (
            "<h1>Confirm toolset changes</h1><ul>" + "".join(impact_rows) + "</ul>"
            + "<p>Nothing has been saved yet. Apply writes Telegram's toolset selection to this profile only. "
              "Other platforms and profile settings are preserved. A running turn is not changed. On the next "
              "message turn, including in this conversation, the gateway reloads the selection and may rebuild "
              "the cached agent and its tool schemas. No gateway restart is required. "
              "Reviewing this summary does not apply or discard pending changes.</p>"
            + self._footer(1, 1, len(added) + len(removed))
        )
        return text, inline_markup([
            [self._button(state, "Apply for next turn", "apply", "success")],
            [self._button(state, "Review toolsets", "list")],
            [self._button(state, "Discard changes", "discard", "danger")],
        ])

    async def _answer(self, event: Any, text: str = "", alert: bool = False) -> None:
        try:
            if text or alert:
                await event.answer(text, alert=alert)
            else:
                await event.answer()
        except Exception:
            logger.debug("Unable to acknowledge Telegram toolset callback", exc_info=True)

    async def _edit(self, event: Any, source: Any, state: dict[str, Any], text: str, markup: Any) -> None:
        editor = getattr(self.adapter, "_edit_rich_ui_message", None)
        if callable(editor):
            await editor(event, source, text, markup)

    async def _render(self, event: Any, source: Any, state: dict[str, Any], note: str = "") -> None:
        if state.get("change") and not note:
            text, markup = self._confirm_view(state)
        else:
            text, markup = self._list_view(state, note)
        await self._edit(event, source, state, text, markup)

    async def handle_command(self, source: Any, arguments: str = "", reply_to: Any = None) -> None:
        """Open the private toolset selector; ``arguments`` is reserved for the routed command API."""
        if not self._allowed(source):
            await self.adapter._send_rich_ui(
                source, "<p>Telegram tool configuration is available only to the authorized owner in a private chat.</p>",
                reply_to=reply_to,
            )
            return
        try:
            home = self._home(source)
            config = self._load_config(str(home))
            enabled = self._enabled_toolsets(config)
            state = self._new_state(source, home, config, enabled)
            text, markup = self._confirm_view(state) if state["change"] else self._list_view(state)
            response = await self.adapter._send_rich_ui(source, text, markup, reply_to=reply_to)
            message_id = getattr(response, "id", None)
            state["message_id"] = str(message_id) if message_id is not None else None
        except Exception as exc:
            logger.warning("Could not open Telegram toolset configuration", exc_info=True)
            if isinstance(exc, RuntimeError) and str(exc):
                note = escape(str(exc))
            else:
                note = "Tool settings could not be safely loaded for the active profile. No settings were changed."
            await self.adapter._send_rich_ui(
                source, f"<p>{note}</p>",
                reply_to=reply_to,
            )

    def _state_for_callback(self, event: Any, source: Any, state_id: int) -> dict[str, Any] | None:
        self._prune()
        state = self._states.get(state_id)
        if not state or not self._allowed(source):
            return None
        try:
            home = self._home(source)
        except Exception:
            return None
        if (state["identity"][:4] != self._identity(home, source)[:4]
                or state["profile"] != str(getattr(source, "profile", "") or "")
                or state["user_id"] != str(getattr(source, "user_id", "") or "")
                or state["chat_id"] != str(getattr(source, "chat_id", "") or "")
                or (getattr(source, "thread_id", None) is not None
                    and state["thread_id"] != str(source.thread_id))
                or state["message_id"] != str(getattr(event, "message_id", "") or "")):
            return None
        return state

    def _rotate_state(self, state: dict[str, Any]) -> None:
        old_id = int(state["id"])
        self._states.pop(old_id, None)
        self._counter += 1
        state["id"] = self._counter
        self._states[state["id"]] = state

    async def _apply(self, state: dict[str, Any]) -> str:
        from hermes_cli.config import _CONFIG_LOCK, save_config
        from agent.skill_utils import parse_config_string_list

        # The CLI checklist saver deliberately clears global suppression and no_mcp.
        # This narrower Telegram editor must preserve both, along with plugin selections.
        with self._config_scope(state["home"]), _CONFIG_LOCK:
            from hermes_cli.config import load_config
            config = load_config()
            current_raw = (config.get("platform_toolsets") or {}).get("telegram")
            current_baseline = current_raw if isinstance(current_raw, list) else None
            if current_baseline != state["baseline_platform_toolsets"]:
                raise RuntimeError("Telegram toolsets changed elsewhere; reopen this screen and review the latest selection")
            selected = set(state["draft"]) | set(state["hidden_explicit"])
            disabled = set(parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets", [])))
            if (state["draft"] - state["original"]) & disabled:
                raise RuntimeError("A global disabled-toolset policy requires review in hermes tools before enabling this capability")
            known = set((config.get("known_builtin_toolsets") or {}).get("telegram") or [])
            known.update(key for key, _label, _description in state["catalog"])
            save_config({"platform_toolsets": {"telegram": sorted(selected)},
                         "known_builtin_toolsets": {"telegram": sorted(known)}},
                        merge_existing=True,
                        preserve_keys={("platform_toolsets", "telegram"),
                                       ("known_builtin_toolsets", "telegram")})
            reread = load_config()
            actual = self._enabled_toolsets(reread)
            expected = set(state["draft"])
            visible_keys = {key for key, _label, _description in state["catalog"]}
            if (actual & visible_keys) != expected:
                raise RuntimeError("Saved toolset selection did not match the profile readback")
            persisted = (reread.get("platform_toolsets") or {}).get("telegram")
            if not isinstance(persisted, list) or not expected.issubset({str(item) for item in persisted}):
                raise RuntimeError("Saved Telegram toolsets could not be verified")
        state["original"] = set(state["draft"])
        state["baseline_platform_toolsets"] = list(persisted)
        state["change"] = None
        return ("Saved and verified for the next Telegram message turn, including in this conversation. "
                "The cached agent may be rebuilt. A running turn was not changed. No gateway restart is required.")

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not str(data or "").startswith(_PREFIX):
            return False
        parts = str(data).split(":", 3)
        try:
            state_id = int(parts[2])
        except (IndexError, ValueError):
            await self._answer(event, "This tool settings card is unavailable.", alert=True)
            return True
        state = self._state_for_callback(event, source, state_id)
        if state is None or len(parts) != 4:
            await self._answer(event, "This card expired or belongs to another owner, profile, chat, topic, or message.", alert=True)
            return True
        self._rotate_state(state)
        await self._answer(event)
        action = parts[3]
        try:
            verb, _, argument = action.partition(":")
            if verb == "toggle":
                entry = next((row for row in state["catalog"] if row[0] == argument), None)
                if entry is None:
                    await self._render(event, source, state, "That toolset is not supported here.")
                    return True
                enabling = argument not in state["draft"]
                if enabling:
                    state["draft"].add(argument)
                else:
                    state["draft"].discard(argument)
                state["change"] = (argument, enabling)
                await self._render(event, source, state)
            elif verb == "page":
                state["change"] = None
                state["page"] = max(1, int(argument))
                await self._render(event, source, state)
            elif verb == "review":
                state["change"] = state["draft"] != state["original"]
                await self._render(event, source, state)
            elif verb == "list":
                state["change"] = None
                await self._render(event, source, state)
            elif verb == "discard":
                state["draft"] = set(state["original"])
                state["change"] = None
                state["page"] = 1
                await self._render(event, source, state, "Staged changes discarded. Nothing was saved.")
            elif verb == "apply":
                if state["draft"] == state["original"]:
                    state["change"] = None
                    await self._render(event, source, state, "There are no staged changes to apply.")
                    return True
                try:
                    note = await self._apply(state)
                except Exception as exc:
                    logger.warning("Telegram toolset save failed (%s)", type(exc).__name__)
                    state["change"] = None
                    if isinstance(exc, RuntimeError) and str(exc):
                        note = str(exc)
                    else:
                        note = ("The save could not be verified. A global policy or concurrent change may prevent it; "
                                "reopen and check the profile before retrying.")
                    await self._render(event, source, state, note)
                    return True
                await self._render(event, source, state, note)
            elif verb == "close":
                self._states.pop(state["id"], None)
                await self._edit(event, source, state,
                                 "<h1>Telegram toolsets</h1><p>Toolset configuration closed. Unapplied changes were not saved.</p>"
                                 + self._footer(1, 1, 0), None)
            else:
                await self._render(event, source, state, "This action is unavailable.")
        except Exception:
            logger.warning("Telegram toolset callback failed", exc_info=True)
            state["change"] = None
            await self._render(event, source, state, "The action failed. Review the profile's saved toolsets before retrying.")
        return True
