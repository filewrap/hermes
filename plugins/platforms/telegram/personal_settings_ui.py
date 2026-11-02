"""Private, per-Telegram-user personal settings UI for the MTProto adapter."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from argparse import Namespace
from pathlib import Path
from typing import Any, Optional

from gateway.telegram_personal_settings import (
    _validated_changes,
    load_personal_settings,
    personal_settings_path,
    public_personal_settings,
    save_personal_mcp_settings,
    save_personal_settings,
    validate_personal_mcp_config,
)
from plugins.platforms.telegram.mtproto_rich import edit_request, inline_markup, send_request

logger = logging.getLogger(__name__)

_STATE_TTL_SECONDS = 60 * 60
_MCP_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_CONTROL_VIEWS = {"home", "attachments", "overview", "learning", "mcp", "model"}


class TelegramPersonalSettingsUI:
    """Keep Telegram personal settings private to one user in one active profile."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._owner_state: dict[tuple[str, str, str, str], int] = {}
        self._pending: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def _home(self, source: Any) -> Path:
        runner = getattr(self.adapter, "gateway_runner", None)
        resolver = getattr(runner, "_resolve_profile_home_for_source", None)
        home = resolver(source) if callable(resolver) else None
        if home is None:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        return Path(home).expanduser().resolve()

    @staticmethod
    def _owner_key(home: Path, source: Any) -> tuple[str, str, str, str]:
        return (str(home), str(source.user_id or ""), str(source.chat_id or ""), str(source.thread_id or ""))

    @staticmethod
    def _pending_key(home: Path, source: Any) -> tuple[str, str, str, str]:
        return (str(home), str(source.user_id or ""), str(source.chat_id or ""), str(source.thread_id or ""))

    def cancel_for_source(self, source: Any) -> None:
        """Invalidate a private flow when navigation switches to a different editor."""
        home = self._home(source)
        key = self._owner_key(home, source)
        state_id = self._owner_state.get(key)
        if state_id is not None:
            self._drop_state(state_id)
        self._pending.pop(self._pending_key(home, source), None)

    def _new_state(self, source: Any, home: Path, message_id: Any = None) -> dict[str, Any]:
        self._counter += 1
        state = {
            "id": self._counter,
            "home": str(home),
            "profile": str(getattr(source, "profile", "") or ""),
            "chat_id": str(source.chat_id),
            "thread_id": str(source.thread_id) if source.thread_id else None,
            "user_id": str(source.user_id),
            "message_id": str(message_id) if message_id is not None else None,
            "created_at": time.monotonic(),
            "pending_mode": None,
            "control_action": "home",
            "control_page": 0,
        }
        self._states[state["id"]] = state
        self._owner_state[self._owner_key(home, source)] = state["id"]
        return state

    def _prune(self) -> None:
        now = time.monotonic()
        for state_id, state in list(self._states.items()):
            if now - state["created_at"] <= _STATE_TTL_SECONDS:
                continue
            self._drop_state(state_id)

    def _drop_state(self, state_id: int) -> None:
        state = self._states.pop(state_id, None)
        if not state:
            return
        self._pending.pop((state["home"], state["user_id"], state["chat_id"], str(state.get("thread_id") or "")), None)
        path = state.get("import_path")
        if path:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not clean expired personal-settings import file", exc_info=True)
        pending_import = state.get("pending_import") or {}
        if pending_import.get("path"):
            try:
                Path(pending_import["path"]).unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not clean expired personal import archive", exc_info=True)
        for key, value in list(self._owner_state.items()):
            if value == state_id:
                self._owner_state.pop(key, None)

    def _rotate_callback_state(self, state: dict[str, Any]) -> None:
        """Invalidate every button on the just-clicked card before awaiting I/O."""
        old_id = int(state["id"])
        self._counter += 1
        new_id = self._counter
        self._states.pop(old_id, None)
        state["id"] = new_id
        self._states[new_id] = state
        owner_key = (state["home"], state["user_id"], state["chat_id"], str(state.get("thread_id") or ""))
        self._owner_state[owner_key] = new_id
        for pending in self._pending.values():
            if pending.get("state_id") == old_id:
                pending["state_id"] = new_id

    @staticmethod
    def _is_private(source: Any) -> bool:
        return str(getattr(source, "chat_type", "")) == "dm"

    async def _answer(self, event: Any, text: str = "") -> None:
        try:
            await event.answer(text) if text else await event.answer()
        except Exception:
            logger.debug("Could not acknowledge Telegram settings callback", exc_info=True)

    async def _edit(self, state: dict[str, Any], text: str, markup: Any, *, rich_format: str = "auto") -> None:
        message_id = state.get("message_id")
        if self.adapter._client is None or not message_id:
            return
        try:
            await self.adapter._mtproto_rich_request(edit_request(
                self.adapter._peer_for(state["chat_id"]), int(message_id), text, reply_markup=markup,
                rich_format=rich_format,
            ))
        except Exception as exc:
            from telethon.errors import MessageNotModifiedError
            if isinstance(exc, MessageNotModifiedError):
                logger.debug("Telegram personal-settings card is already current")
                return
            raise

    async def _render_home(self, state: dict[str, Any], *, note: str = "") -> None:
        state["control_action"] = "home"
        state["control_page"] = 0
        complete = getattr(self.adapter, "_complete_control_frame", None)
        if callable(complete):
            complete({"kind": "personal", "action": "home", "page": 0})
        text, markup = self._home_view(state, note=note)
        await self._edit(state, text, markup)

    def _push_control_frame(self, source: Any, state: dict[str, Any]) -> bool:
        """Remember only a safe destination, never rendered or persisted settings."""
        action = state.get("control_action", "home")
        if action not in _CONTROL_VIEWS:
            action = "home"
        page = state.get("control_page", 0)
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            page = 0
        push = getattr(self.adapter, "_push_control_frame", None)
        if not callable(push):
            return False
        return bool(push(source, state.get("message_id"), {
            "kind": "personal", "action": action, "page": page,
        }))

    async def restore_control_frame(self, source: Any, message_id: Any, frame: dict[str, Any]) -> bool:
        """Recreate a safe personal-settings destination from current readbacks."""
        if (not isinstance(frame, dict) or frame.get("kind") != "personal"
                or frame.get("action") not in _CONTROL_VIEWS
                or not self._is_private(source) or not source.user_id):
            return False
        action = frame["action"]
        page = frame.get("page", 0)
        if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page <= 10000:
            return False
        state = self._new_state(source, self._home(source), message_id=message_id)
        state["user_name"] = str(getattr(source, "user_name", None) or "Telegram name")
        state["invocation_message_id"] = None
        state["help_origin"] = None
        state["control_action"] = action
        state["control_page"] = page
        if action == "home":
            await self._render_home(state)
        elif action == "attachments":
            await self._show_attachment_settings(state)
        elif action == "overview":
            from plugins.platforms.telegram.rich_ui import learning_overview_view
            text, markup = learning_overview_view(load_personal_settings(state["home"], state["user_id"]), state["id"])
            await self._edit(state, text, markup)
        elif action == "learning":
            from plugins.platforms.telegram.rich_ui import self_learning_view
            text, markup = self_learning_view(load_personal_settings(state["home"], state["user_id"]), state["id"])
            await self._edit(state, text, markup)
        elif action == "mcp":
            await self._show_mcp(state, page=page)
        elif action == "model":
            await self._open_personal_model_picker(source, state)
        return True

    def _home_view(self, state: dict[str, Any], *, note: str = "") -> tuple[str, Any]:
        state["control_action"] = "home"
        state["control_page"] = 0
        from plugins.platforms.telegram.rich_ui import personal_settings_view, settings_page_html
        from plugins.platforms.telegram.mtproto_rich import rich_button
        settings = load_personal_settings(state["home"], state["user_id"])
        _original_text, markup = personal_settings_view(settings, state.get("user_name", "Telegram name"), state["id"])
        from gateway.telegram_skills import list_personal_skills
        own_skills = list_personal_skills(state["home"], state["user_id"])
        from gateway.telegram_personal_settings import effective_auto_load_skill_names
        selected_skills = effective_auto_load_skill_names(state["home"], state["user_id"], settings=settings)
        selected_mcp = list(settings.get("selected_mcp_servers") or [])
        mcp_mode = settings.get("mcp_mode", "inherit")
        from plugins.platforms.telegram.attachment_ui import format_size
        threshold = int(settings.get("attachment_auto_download_threshold_bytes", 200 * 1024 * 1024))
        explicit_limit = int(settings.get("attachment_explicit_download_limit_bytes", 2 * 1024 * 1024 * 1024))
        instruction_value = lambda key: str(settings.get(key) or "") or "Not set"

        def pill(label: str, name: str, style: str | None = None) -> str:
            return rich_button(label, kind="callback", value=f"ps:{state['id']}:{name}", style=style)

        def instruction_actions(name: str, key: str) -> str:
            pills = [pill("Edit", f"edit-{name}")]
            if settings.get(key):
                pills.append(pill("Remove", f"remove-{name}", "danger"))
            return " ".join(pills)

        def toggle(name: str, enabled: bool) -> str:
            return pill("Turn off" if enabled else "Turn on", f"{name}-toggle",
                        "danger" if enabled else "success")

        user_name = state.get("user_name", "Telegram name") or "Telegram name"
        user_id = str(state.get("user_id") or "")
        name_action = (rich_button(user_name, kind="profile", value=user_id)
                       if user_id.isdecimal() else "")
        rows = [
            ("Name", user_name, name_action),
            ("Private skill library", f"{len(own_skills)} skills",
             pill("View", "skills", "primary")),
            ("Auto-load skills", f"{len(selected_skills)} selected (profile defaults + personal additions)",
             pill("Manage", "skills", "primary")),
            ("MCP servers", "Profile defaults" if mcp_mode == "inherit" else
             f"Personal selection · {len(selected_mcp)}: {', '.join(map(str, selected_mcp)) or 'None'}",
             pill("Manage", "mcp")),
            ("Default model", settings.get("default_model") or "Profile default", pill("Change", "model")),
            ("Default provider", settings.get("default_provider") or "Profile default", ""),
            ("General instructions", instruction_value("general_instructions"),
             instruction_actions("general", "general_instructions")),
            ("Search instructions", instruction_value("search_instructions"),
             instruction_actions("search", "search_instructions")),
            ("Memory", "On" if settings.get("memory_enabled", True) else "Off",
             toggle("memory", bool(settings.get("memory_enabled", True)))),
            ("Self-learning", "On" if settings.get("self_learning_enabled", True) else "Off",
             toggle("learning", bool(settings.get("self_learning_enabled", True)))),
            ("Message reactions", "On" if settings.get("reactions_enabled", True) else "Off",
             toggle("reactions", bool(settings.get("reactions_enabled", True)))),
            ("Optional text reactions (heuristic)", "On" if settings.get("text_reactions_enabled", False) else "Off",
             toggle("text-reactions", bool(settings.get("text_reactions_enabled", False)))),
            ("Turn activity trail", "On" if settings.get("activity_trail_enabled", False) else "Off",
             toggle("activity-trail", bool(settings.get("activity_trail_enabled", False)))),
            ("File downloads", f"{'Automatic' if settings.get('attachment_download_mode', 'auto') == 'auto' else 'Explicit only'}; "
             f"auto threshold {format_size(threshold)}; explicit receive limit {format_size(explicit_limit)}",
             pill("Open", "attachments")),
        ]
        if note:
            rows.append(("Status", note, ""))
        text = settings_page_html(
            "Personal settings",
            "These preferences apply only to your Telegram account in this profile.",
            rows,
        )
        markup = self._add_attachment_button(markup, state["id"], settings)
        if state.get("help_origin") is not None:
            markup.rows.extend(inline_markup([
                [("Back to help", f"ps:{state['id']}:back-help", "primary")],
            ]).rows)
        self._move_close_last(markup)
        return text, markup

    @staticmethod
    def _move_close_last(markup: Any) -> Any:
        """Keep the existing Close callback and style, but put its row last."""
        if markup is None:
            return markup
        close_entry = None
        row_type = type(markup.rows[0]) if markup.rows else None
        for row in list(markup.rows):
            for wrapped in list(row.buttons):
                button = getattr(wrapped, "button", wrapped)
                if getattr(button, "text", None) == "Close":
                    close_entry = wrapped
                    row.buttons.remove(wrapped)
                    if not row.buttons:
                        markup.rows.remove(row)
                    break
            if close_entry is not None:
                break
        if close_entry is not None:
            if row_type is not None:
                markup.rows.append(row_type(buttons=[close_entry]))
        return markup

    @staticmethod
    def _add_attachment_button(markup: Any, state_id: int, settings: dict[str, Any]) -> Any:
        """Add file-download and optional text-reaction controls to the private card."""
        text_reactions = bool(settings.get("text_reactions_enabled", False))
        extra = inline_markup([
            [(f"Optional text reactions · {'On' if text_reactions else 'Off'}", f"ps:{state_id}:text-reactions-toggle", "success" if text_reactions else None)],
            [('File downloads', f"ps:{state_id}:attachments", None)],
        ])
        if markup is None:
            return extra
        markup.rows.extend(extra.rows)
        return markup

    async def _show_attachment_settings(self, state: dict[str, Any], note: str = "") -> None:
        state["control_action"] = "attachments"
        state["control_page"] = 0
        from plugins.platforms.telegram.attachment_ui import format_size
        from gateway.telegram_personal_settings import (
            ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES,
            ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES,
        )

        settings = load_personal_settings(state["home"], state["user_id"])
        mode = str(settings.get("attachment_download_mode", "auto"))
        threshold = int(settings.get("attachment_auto_download_threshold_bytes", 200 * 1024 * 1024))
        explicit_limit = int(settings.get("attachment_explicit_download_limit_bytes", 2 * 1024 * 1024 * 1024))
        try:
            from plugins.platforms.telegram.attachment_ui import (
                automatic_download_cap, effective_explicit_download_limit,
            )
            auto_cap = automatic_download_cap()
            effective_explicit = effective_explicit_download_limit(settings)
        except Exception:
            auto_cap = 0
            effective_explicit = explicit_limit
        effective_auto = min(threshold, auto_cap) if auto_cap else threshold
        auto_cap_label = format_size(auto_cap) if auto_cap else "No Hermes cache cap configured"
        mode_label = "Automatic under threshold" if mode == "auto" else "Explicit download only"
        from plugins.platforms.telegram.mtproto_rich import rich_button
        from plugins.platforms.telegram.rich_ui import settings_page_html

        def mode_pill(label: str, name: str, active: bool) -> str:
            return rich_button(label, kind="callback", value=f"ps:{state['id']}:{name}",
                               style="success" if active else None)

        rows = [
            ("Mode", mode_label,
             f"{mode_pill('Automatic', 'download-mode-auto', mode == 'auto')} "
             f"{mode_pill('Explicit only', 'download-mode-explicit', mode == 'explicit')}"),
            ("Auto-download threshold", format_size(threshold), ""),
            ("Automatic cache safety cap", auto_cap_label, ""),
            ("Effective automatic-download size", format_size(effective_auto), ""),
            ("Explicit receive limit", format_size(explicit_limit), ""),
            ("Effective explicit-download limit", format_size(effective_explicit), ""),
            ("Download behavior", "Files above the threshold, files with unknown sizes, or files in explicit-only mode are not downloaded automatically.", ""),
            ("Transfer limits", "Explicit downloads are streamed to disk and honor any operator-configured inbound media cap. The explicit limit applies to receiving files only; outgoing uploads remain subject to bot/provider limits.", ""),
        ]
        if note:
            rows.append(("Status", note, ""))
        text = settings_page_html(
            "File download settings",
            "These file download preferences apply only to your Telegram account in this profile.",
            rows,
        )
        action = lambda value: f"ps:{state['id']}:{value}"
        choice_rows = [[
            ("Automatic" + (" ✓" if mode == "auto" else ""), action("download-mode-auto"), "success" if mode == "auto" else None),
            ("Explicit only" + (" ✓" if mode == "explicit" else ""), action("download-mode-explicit"), "success" if mode == "explicit" else None),
        ]]
        threshold_buttons = []
        for value in ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES:
            mib = value // (1024 * 1024)
            label = f"{mib} MiB" + (" ✓" if value == threshold else "")
            threshold_buttons.append((label, action(f"download-threshold:{mib}"), "primary" if value == threshold else None))
        explicit_buttons = []
        for value in ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES:
            mib = value // (1024 * 1024)
            label = format_size(value) + (" ✓" if value == explicit_limit else "")
            explicit_buttons.append((label, action(f"explicit-limit:{mib}"), "primary" if value == explicit_limit else None))
        choice_rows.extend([
            threshold_buttons[:2], threshold_buttons[2:], explicit_buttons[:2], explicit_buttons[2:],
            [("‹ Personal settings", action("home"), "primary")],
            [("Close", action("close"), "danger")],
        ])
        await self._edit(state, text, inline_markup(choice_rows))

    async def open(self, source: Any, *, reply_to: Any = None, message_id: Any = None,
                   help_origin: tuple[str, int] | None = None,
                   invocation_message_id: Any = None) -> None:
        self._prune()
        if not self._is_private(source):
            await self.adapter.send(
                source.chat_id,
                "Personal settings are private. Open a direct chat with this bot and send `/settings`.",
                reply_to=reply_to,
                metadata={"thread_id": source.thread_id} if source.thread_id else None,
            )
            return
        if not source.user_id:
            await self.adapter.send(source.chat_id, "Telegram could not identify your account for personal settings.", reply_to=reply_to)
            return
        home = self._home(source)
        key = self._owner_key(home, source)
        state_id = self._owner_state.get(key)
        state = self._states.get(state_id) if state_id is not None else None
        if state is None:
            state = self._new_state(source, home, message_id=message_id)
        elif message_id is not None:
            # The help card becomes the settings card when opened from an inline button.
            state["message_id"] = str(message_id)
        state["created_at"] = time.monotonic()
        state["user_name"] = str(source.user_name or "Telegram name")
        try:
            invocation_id = int(invocation_message_id)
        except (TypeError, ValueError):
            invocation_id = 0
        state["invocation_message_id"] = str(invocation_id) if invocation_id > 0 else None
        # Explicit /settings and non-help entry points must not inherit an old help target.
        state["help_origin"] = help_origin
        text, markup = self._home_view(state)
        if message_id is not None:
            await self._edit(state, text, markup)
            return
        if state.get("message_id"):
            await self._edit(state, text, markup)
            return
        metadata = {"thread_id": source.thread_id} if source.thread_id else {}
        try:
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(source.chat_id), text,
                reply_to=int(reply_to) if reply_to is not None else (int(source.thread_id) if source.thread_id else None),
                top_msg_id=int(source.thread_id) if source.thread_id else None,
                reply_markup=markup,
            ))
            state["message_id"] = self.adapter._sent_message_id(response)
        except Exception:
            logger.warning("Could not open Telegram personal settings", exc_info=True)
            await self.adapter.send(source.chat_id, "Could not open personal settings. Please try again.", reply_to=reply_to, metadata=metadata)

    def _state_for_callback(self, state_id: int, source: Any, event: Any) -> tuple[Optional[dict[str, Any]], Any]:
        self._prune()
        state = self._states.get(state_id)
        if not state or not self._is_private(source):
            return None, source
        if (state["profile"] != str(getattr(source, "profile", "") or "")
                or state["chat_id"] != str(getattr(source, "chat_id", "") or "")
                or state["user_id"] != str(getattr(source, "user_id", "") or "")
                or state.get("message_id") != str(getattr(event, "message_id", "") or "")):
            return None, source

        expected_thread = str(state.get("thread_id") or "")
        incoming_thread = str(getattr(source, "thread_id", "") or "")
        if incoming_thread and incoming_thread != expected_thread:
            return None, source
        bound_source = source
        if not incoming_thread and expected_thread:
            try:
                from gateway.session_identity import replace_source
                bound_source = replace_source(source, thread_id=expected_thread)
            except (TypeError, ValueError):
                return None, source
        try:
            if str(self._home(bound_source)) != state["home"]:
                return None, source
        except Exception:
            return None, source
        return state, bound_source

    async def _delete_attached_menu_and_command(self, state: dict[str, Any], source: Any, event: Any) -> None:
        """Delete only this authorized card and its explicitly tracked command message."""
        menu_id = str(state.get("message_id") or "")
        if (not menu_id or menu_id != str(getattr(event, "message_id", "") or "")
                or state.get("chat_id") != str(source.chat_id)
                or state.get("user_id") != str(source.user_id or "")
                or state.get("thread_id") != (str(source.thread_id) if source.thread_id else None)):
            return
        message_ids = [menu_id]
        command_id = str(state.get("invocation_message_id") or "")
        if command_id and command_id != menu_id:
            message_ids.append(command_id)
        try:
            numeric_ids = [int(value) for value in message_ids if int(value) > 0]
        except (TypeError, ValueError):
            logger.debug("Skipping Telegram settings close with invalid tracked message ID")
            return
        client = getattr(self.adapter, "_client", None)
        delete_messages = getattr(client, "delete_messages", None)
        if not callable(delete_messages):
            raise RuntimeError("Telegram message deletion is unavailable")
        # Let the feature's callback handler refresh a retryable card on failure.
        await delete_messages(self.adapter._peer_for(state["chat_id"]), numeric_ids)

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not data.startswith("ps:"):
            return False
        if not self._is_private(source) or not source.user_id:
            await self._answer(event, "Personal settings are available only in your private chat.")
            return True
        parts = data.split(":", 2)
        if len(parts) != 3:
            await self._answer(event, "This settings action is unavailable.")
            return True
        try:
            state_id = int(parts[1])
        except ValueError:
            await self._answer(event, "This settings action is unavailable.")
            return True
        state, source = self._state_for_callback(state_id, source, event)
        if state is None:
            await self._answer(event, "This settings card expired or belongs to another account.")
            return True
        action = parts[2]
        # The event loop can schedule overlapping callback requests while an edit/export is
        # awaiting Telegram. Rotate synchronously so a second tap cannot repeat that operation.
        self._rotate_callback_state(state)
        await self._answer(event)
        try:
            await self._dispatch_action(state, source, event, action)
        except Exception:
            logger.exception("Telegram personal-settings action failed")
            self._cancel_pending(state, source)
            await self._render_home(state, note="This action failed safely; no other account's settings were changed.")
        return True

    async def _dispatch_action(self, state: dict[str, Any], source: Any, event: Any, action: str) -> None:
        home, user_id = state["home"], state["user_id"]
        if action == "back-help" and state.get("help_origin") is not None:
            from plugins.platforms.telegram.rich_ui import help_view
            view, page = state["help_origin"]
            text, markup = help_view(view, page, skill_commands=self.adapter._help_skill_commands(source))
            await self._edit(state, text, markup)
            self._drop_state(state["id"])
            return
        if action == "home":
            state["pending_mode"] = None
            self._pending.pop(self._pending_key(Path(home), source), None)
            await self._render_home(state)
            return
        if action == "close":
            await self._delete_attached_menu_and_command(state, source, event)
            self._drop_state(state["id"])
            return
        if action == "attachments":
            self._push_control_frame(source, state)
            await self._show_attachment_settings(state)
            return
        if action == "text-reactions-toggle":
            current = load_personal_settings(home, user_id)
            save_personal_settings(home, user_id, {
                "text_reactions_enabled": not bool(current.get("text_reactions_enabled", False)),
            })
            await self._render_home(state, note="Optional heuristic text reactions updated. Contextual model-requested reactions remain separate.")
            return
        if action == "activity-trail-toggle":
            current = load_personal_settings(home, user_id)
            enabled = not bool(current.get("activity_trail_enabled", False))
            save_personal_settings(home, user_id, {"activity_trail_enabled": enabled})
            await self._render_home(
                state,
                note=f"Turn activity trail {'enabled' if enabled else 'disabled'} for your Telegram account in this profile.",
            )
            return
        if action in {"download-mode-auto", "download-mode-explicit"}:
            mode = "auto" if action.endswith("auto") else "explicit"
            save_personal_settings(home, user_id, {"attachment_download_mode": mode})
            await self._show_attachment_settings(state, "File download preference saved for your account in this profile.")
            return
        if action.startswith("download-threshold:"):
            from gateway.telegram_personal_settings import ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES
            try:
                selected_mib = int(action.partition(":")[2])
            except ValueError:
                selected_mib = -1
            selected_bytes = selected_mib * 1024 * 1024
            if selected_bytes not in ATTACHMENT_DOWNLOAD_THRESHOLDS_BYTES:
                await self._answer(event, "That download threshold is unavailable.")
                return
            save_personal_settings(home, user_id, {"attachment_auto_download_threshold_bytes": selected_bytes})
            await self._show_attachment_settings(state, "Auto-download threshold saved.")
            return
        if action.startswith("explicit-limit:"):
            from gateway.telegram_personal_settings import ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES
            try:
                selected_mib = int(action.partition(":")[2])
            except ValueError:
                selected_mib = -1
            selected_bytes = selected_mib * 1024 * 1024
            if selected_bytes not in ATTACHMENT_EXPLICIT_DOWNLOAD_LIMITS_BYTES:
                await self._answer(event, "That explicit download limit is unavailable.")
                return
            save_personal_settings(home, user_id, {"attachment_explicit_download_limit_bytes": selected_bytes})
            await self._show_attachment_settings(state, "Explicit receive limit saved for your account in this profile.")
            return
        if action in {"memory-on", "memory-off", "learning-on", "learning-off",
                      "memory-toggle", "learning-toggle", "reactions-toggle"}:
            field = ("memory_enabled" if action.startswith("memory-") else
                     "self_learning_enabled" if action.startswith("learning-") else "reactions_enabled")
            current = load_personal_settings(home, user_id)
            enabled = (not bool(current.get(field, True)) if action.endswith("toggle")
                       else action.endswith("on"))
            save_personal_settings(home, user_id, {field: enabled})
            await self._render_home(state)
            return
        if action in {"remove-general", "remove-search"}:
            mode = "general_instructions" if action == "remove-general" else "search_instructions"
            title = "General instructions" if mode == "general_instructions" else "Search instructions"
            save_personal_settings(home, user_id, {mode: ""})
            await self._render_home(state, note=f"{title} removed.")
            return
        if action in {"edit-general", "edit-search"}:
            self._push_control_frame(source, state)
            mode = "general_instructions" if action == "edit-general" else "search_instructions"
            state["pending_mode"] = mode
            self._pending[self._pending_key(Path(home), source)] = {"state_id": state["id"], "mode": mode}
            title = "General instructions" if mode == "general_instructions" else "Search instructions"
            scope = ("Used across your private Telegram turns." if mode == "general_instructions"
                     else "Used when you ask `/search`.")
            text = (f"# {title}\n\n*{scope}*\n\n"
                    "Send a message now to replace these instructions. Choose Cancel or send `/cancel` "
                    "to keep the current text.\n\nOnly this Telegram account in this profile is affected.")
            await self._edit(state, text, inline_markup([[("Cancel", f"ps:{state['id']}:home", "danger")]]))
            return
        if action == "overview":
            self._push_control_frame(source, state)
            from plugins.platforms.telegram.rich_ui import learning_overview_view
            state["control_action"] = "overview"
            state["control_page"] = 0
            text, markup = learning_overview_view(load_personal_settings(home, user_id), state["id"])
            await self._edit(state, text, markup)
            return
        if action == "learning":
            self._push_control_frame(source, state)
            from plugins.platforms.telegram.rich_ui import self_learning_view
            state["control_action"] = "learning"
            state["control_page"] = 0
            text, markup = self_learning_view(load_personal_settings(home, user_id), state["id"])
            await self._edit(state, text, markup)
            return
        if action == "model":
            self._push_control_frame(source, state)
            state["control_action"] = "model"
            state["control_page"] = 0
            await self._open_personal_model_picker(source, state)
            return
        if action == "skills":
            self._push_control_frame(source, state)
            self._cancel_pending(state, source)
            self._drop_state(state["id"])
            handoff = {"message_id": state.get("message_id")}
            if state.get("invocation_message_id") is not None:
                handoff["invocation_message_id"] = state["invocation_message_id"]
            await self.adapter._skills_ui().open(source, **handoff)
            return
        if action == "mcp":
            self._push_control_frame(source, state)
            await self._show_mcp(state)
            return
        if action == "mcp-inherit":
            save_personal_mcp_settings(home, user_id, {"mcp_mode": "inherit", "selected_mcp_servers": []})
            await self._show_mcp(state, "Using profile MCP defaults again.")
            return
        if action.startswith("mcp-toggle:"):
            await self._toggle_mcp(state, action.partition(":")[2])
            return
        if action.startswith("mcp-page:"):
            try:
                page = int(action.partition(":")[2])
            except ValueError:
                page = 0
            await self._show_mcp(state, page=page)
            return
        if action == "mcp-add":
            self._push_control_frame(source, state)
            await self._start_mcp_add(state, source)
            return
        if action == "mcp-add-confirm":
            try:
                await self._save_mcp(state, source)
            except ValueError as exc:
                state.pop("pending_mcp", None)
                await self._show_mcp(state, str(exc) or "That MCP configuration could not be saved.")
            return
        if action == "export":
            from plugins.platforms.telegram.rich_ui import export_menu_view
            text, markup = export_menu_view(state["id"])
            await self._edit(state, text, markup)
            return
        if action.startswith("export-preferences"):
            await self._show_export_confirm(state, "preferences")
            return
        if action.startswith("export-credentials"):
            await self._show_export_confirm(state, "credentials")
            return
        if action.startswith("export-all"):
            await self._show_export_confirm(state, "all")
            return
        if action.startswith("export-confirm:"):
            scope = action.partition(":")[2]
            if scope not in {"preferences", "credentials", "all"}:
                await self._answer(event, "Unknown export scope.")
                return
            await self._perform_export(state, source, scope)
            return
        if action == "import":
            state["pending_mode"] = "import-file"
            self._pending[self._pending_key(Path(home), source)] = {"state_id": state["id"], "mode": "import-file"}
            from plugins.platforms.telegram.rich_ui import import_prompt_view
            text, markup = import_prompt_view(state["id"])
            await self._edit(state, text, markup)
            return
        if action == "import-confirm":
            await self._apply_import(state, source)
            return
        if action == "import-cancel":
            self._cancel_pending(state, source)
            await self._render_home(state, note="Import cancelled; no data was changed.")
            return
        await self._answer(event, "This settings action is unavailable.")

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        """Consume the next private reply only when that same user started an edit/import."""
        if not self._is_private(source) or not source.user_id:
            return False
        self._prune()
        home = self._home(source)
        key = self._pending_key(home, source)
        pending = self._pending.get(key)
        if not pending:
            return False
        state = self._states.get(pending["state_id"])
        if not state:
            self._pending.pop(key, None)
            return False
        stripped = str(text or "").strip()
        if stripped.lower() == "/cancel":
            self._cancel_pending(state, source)
            await self._render_home(state, note="Cancelled; no changes were made.")
            return True
        if stripped.startswith("/") and pending["mode"] not in {"import-file"}:
            return False
        mode = pending["mode"]
        if mode in {"general_instructions", "search_instructions"}:
            save_personal_settings(home, source.user_id, {mode: stripped})
            self._cancel_pending(state, source)
            await self._render_home(state, note="Instructions saved for this Telegram account.")
            return True
        if mode == "mcp-add-json":
            if len(stripped) > 12000:
                await self._edit(state, "# MCP configuration too large\n\nSend a JSON object under 12,000 characters or `/cancel`.",
                                 inline_markup([[("Cancel", f"ps:{state['id']}:home", "danger")]]))
                return True
            try:
                payload = json.loads(stripped)
                name, config = self._validate_mcp_payload(payload)
                existing = dict(self._load_profile_mcp(state["home"]))
                existing.update(load_personal_settings(state["home"], source.user_id).get("personal_mcp_servers") or {})
                if name in existing:
                    raise ValueError("That server name already exists in profile or personal settings; use a new name.")
            except Exception as exc:
                await self._edit(state, f"# MCP configuration not accepted\n\n{self._safe_error(exc)}\n\nCorrect the JSON or send `/cancel`.",
                                 inline_markup([[("Cancel", f"ps:{state['id']}:home", "danger")]]))
                return True
            self._cancel_pending(state, source, keep_payload=True)
            state["pending_mode"] = "mcp-add-confirm"
            state["pending_mcp"] = {"name": name, "config": config}
            summary = self._mcp_summary(name, config)
            text = (
                "# Confirm MCP configuration\n\n"
                + summary
                + "\n\n*Saved only to this account. The server may start a local process or contact a remote endpoint when used; it is selected only for your turns.*"
            )
            await self._edit(state, text, inline_markup([[("Save MCP configuration", f"ps:{state['id']}:mcp-add-confirm", "primary"),
                                                          ("Cancel", f"ps:{state['id']}:home", "danger")]]))
            return True
        if mode == "import-file":
            return await self._receive_import(event, source, state, stripped)
        return False

    def _cancel_pending(self, state: dict[str, Any], source: Any, *, keep_payload: bool = False) -> None:
        self._pending.pop(self._pending_key(Path(state["home"]), source), None)
        state["pending_mode"] = None
        if not keep_payload:
            state.pop("pending_mcp", None)
            pending_import = state.pop("pending_import", None) or {}
            if pending_import.get("path"):
                try:
                    Path(pending_import["path"]).unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not clean cancelled personal import archive", exc_info=True)

    async def _show_mcp(self, state: dict[str, Any], note: str = "", *, page: Optional[int] = None) -> None:
        from plugins.platforms.telegram.rich_ui import mcp_servers_view
        settings = load_personal_settings(state["home"], state["user_id"])
        selected = set(settings.get("selected_mcp_servers") or [])
        servers = dict(self._load_profile_mcp(state["home"]))
        servers.update(settings.get("personal_mcp_servers") or {})
        names = sorted(servers)
        state["mcp_names"] = names
        if page is not None:
            state["mcp_page"] = page
        state["control_action"] = "mcp"
        state["control_page"] = max(0, int(state.get("mcp_page", 0) or 0))
        complete = getattr(self.adapter, "_complete_control_frame", None)
        if callable(complete):
            complete({"kind": "personal", "action": "mcp", "page": state["control_page"]})
        text, markup = mcp_servers_view(
            [(name, name in selected) for name in names], state["id"],
            note=note, page=state.get("mcp_page", 0),
        )
        await self._edit(state, text, markup)

    async def open_mcp(self, source: Any, *, message_id: Any = None, reply_to: Any = None) -> bool:
        """Open this Telegram user's existing private MCP selection view."""
        if not self._is_private(source) or not source.user_id:
            return False
        await self.open(source, message_id=message_id, reply_to=reply_to)
        state_id = self._owner_state.get(self._owner_key(self._home(source), source))
        state = self._states.get(state_id) if state_id is not None else None
        if not state:
            return False
        self._push_control_frame(source, state)
        await self._show_mcp(state)
        return True

    async def open_custom_mcp(self, source: Any, *, message_id: Any = None) -> bool:
        """Open this Telegram user's existing private custom-MCP setup flow."""
        if not self._is_private(source) or not source.user_id:
            return False
        await self.open(source, message_id=message_id)
        state_id = self._owner_state.get(self._owner_key(self._home(source), source))
        state = self._states.get(state_id) if state_id is not None else None
        if not state:
            return False
        self._push_control_frame(source, state)
        await self._start_mcp_add(state, source)
        return True

    async def _start_mcp_add(self, state: dict[str, Any], source: Any) -> None:
        state["pending_mode"] = "mcp-add-json"
        self._pending[self._pending_key(Path(state["home"]), source)] = {
            "state_id": state["id"], "mode": "mcp-add-json",
        }
        text = (
            "# Add a custom MCP server\n\n"
            "**Remote**\n`{\"name\":\"server-name\",\"config\":{\"url\":\"https://…\"}}`\n\n"
            "**Local**\nInclude `command` and `args` for a stdio server.\n\n"
            "*Use `${ENV_NAME}` for profile secrets; never paste tokens here.*\n"
            "*Private to this account. Exact non-secret details are reviewed before saving; "
            "the server runs only when selected and used.*"
        )
        await self._edit(state, text, inline_markup([[('Cancel', f"ps:{state['id']}:home", "danger")]]))

    @staticmethod
    def _load_profile_mcp(home: str) -> dict[str, Any]:
        from hermes_cli.config import read_user_config_raw
        cfg = read_user_config_raw(Path(home) / "config.yaml")
        servers = cfg.get("mcp_servers", {}) if isinstance(cfg, dict) else {}
        return servers if isinstance(servers, dict) else {}

    async def _toggle_mcp(self, state: dict[str, Any], raw_index: str) -> None:
        try:
            index = int(raw_index)
            name = state["mcp_names"][index]
        except (TypeError, ValueError, IndexError, KeyError):
            await self._show_mcp(state, "That MCP selection expired. Open the list again.")
            return
        settings = load_personal_settings(state["home"], state["user_id"])
        selected = set(settings.get("selected_mcp_servers") or [])
        if name in selected:
            selected.remove(name)
        else:
            selected.add(name)
        save_personal_mcp_settings(state["home"], state["user_id"], {
            "mcp_mode": "selected", "selected_mcp_servers": sorted(selected),
        })
        await self._show_mcp(state)

    async def _open_personal_model_picker(self, source: Any, state: dict[str, Any]) -> None:
        try:
            from gateway.run import _load_gateway_config
            from hermes_cli.model_switch_providers import list_picker_providers
            config = _load_gateway_config(config_path=Path(state["home"]) / "config.yaml")
            config = config if isinstance(config, dict) else {}
            model_cfg = config.get("model", {})
            if not isinstance(model_cfg, dict):
                model_cfg = {}
            model_catalog = config.get("model_catalog", {})
            if not isinstance(model_catalog, dict):
                model_catalog = {}
            user_providers = config.get("providers", {})
            if not isinstance(user_providers, dict):
                user_providers = {}
            custom_providers = config.get("custom_providers", [])
            if not isinstance(custom_providers, list):
                custom_providers = []
            personal = load_personal_settings(state["home"], state["user_id"])
            provider = str(personal.get("default_provider") or model_cfg.get("provider") or "openrouter").strip()
            current_model = str(personal.get("default_model") or model_cfg.get("default") or "").strip()
            providers = await asyncio.to_thread(
                list_picker_providers, max_models=50, include_moa=True,
                current_provider=provider, current_base_url=str(model_cfg.get("base_url") or ""),
                current_model=current_model, user_providers=user_providers,
                custom_providers=custom_providers,
                excluded_providers=model_catalog.get("excluded_providers", []),
            )
            if not providers:
                await self._edit(state, "# Default model\n\nNo authenticated model providers are available in this profile. The current setting is unchanged.",
                                 inline_markup([[("Back to settings", f"ps:{state['id']}:home", "primary")]]))
                return

            async def _save_personal_model(_chat_id: str, model_id: str, provider_slug: str) -> str:
                save_personal_settings(state["home"], state["user_id"], {
                    "default_model": str(model_id), "default_provider": str(provider_slug),
                })
                return (f"Personal default saved: `{model_id}` · provider `{provider_slug}`. "
                        "The shared profile default is unchanged; an explicit session `/model` choice still takes precedence.")

            result = await self.adapter.send_model_picker(
                chat_id=source.chat_id, providers=providers,
                current_model=current_model, current_provider=provider,
                session_key=f"personal-settings:{state['home']}:{state['user_id']}",
                on_model_selected=_save_personal_model,
                metadata={"user_id": source.user_id, "chat_type": "dm", "thread_id": source.thread_id},
            )
            if not result.success:
                await self._edit(state, "# Default model\n\nModel options could not be sent. No personal setting was changed.",
                                 inline_markup([[("Back to settings", f"ps:{state['id']}:home", "primary")]]))
        except Exception as exc:
            logger.warning("Could not open personal Telegram model picker", exc_info=True)
            await self._edit(state, f"# Default model\n\nModel options could not be loaded ({type(exc).__name__}). No personal setting was changed.",
                             inline_markup([[("Back to settings", f"ps:{state['id']}:home", "primary")]]))

    async def _show_export_confirm(self, state: dict[str, Any], scope: str) -> None:
        from plugins.platforms.telegram.rich_ui import export_confirm_view
        text, markup = export_confirm_view(state["id"], scope)
        await self._edit(state, text, markup)

    async def _perform_export(self, state: dict[str, Any], source: Any, scope: str) -> None:
        if not self._is_private(source):
            await self._render_home(state, note="Exports can only be sent in a private chat.")
            return
        tmpdir = Path(tempfile.mkdtemp(prefix="hermes-telegram-export-"))
        try:
            try:
                tmpdir.chmod(0o700)
            except OSError:
                pass
            out = tmpdir / ("personal-preferences.json" if scope == "preferences" else f"hermes-{scope}.zip")
            if scope == "preferences":
                payload = {
                    "kind": "hermes.telegram.personal-settings",
                    "version": 1,
                    "settings": public_personal_settings(load_personal_settings(state["home"], state["user_id"])),
                }
                out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                out.chmod(0o600)
            elif scope == "credentials":
                self._create_credentials_archive(state, out)
            else:
                await asyncio.to_thread(self._create_full_backup, state["home"], out)
            client = self.adapter._client
            if client is None:
                raise RuntimeError("Telegram is disconnected")
            caption = {
                "preferences": "Your Telegram personal settings only (no credentials).",
                "credentials": "Credential archive — plaintext secret material. Keep it secure.",
                "all": "Full Hermes profile backup — may contain credentials and Telegram session data.",
            }[scope]
            thread_id = source.thread_id
            await client.send_file(
                self.adapter._peer_for(source.chat_id), str(out), caption=caption,
                reply_to=int(thread_id) if thread_id else None,
            )
            await self._render_home(state, note="Export sent to this private chat. The temporary archive was removed.")
        except Exception as exc:
            logger.warning("Telegram personal export failed (%s)", type(exc).__name__, exc_info=True)
            await self._render_home(state, note="Export failed safely; no other user's data was changed.")
        finally:
            try:
                for child in tmpdir.iterdir():
                    child.unlink(missing_ok=True)
                tmpdir.rmdir()
            except OSError:
                logger.debug("Could not remove temporary Telegram export directory", exc_info=True)

    def _create_credentials_archive(self, state: dict[str, Any], out: Path) -> None:
        home = Path(state["home"]).resolve()
        candidates = [home / ".env", home / "auth.json", home / "vault.key", home / "vault.json.enc"]
        try:
            session_path = self.adapter._session_file().resolve()
            if session_path.is_relative_to(home):
                candidates.append(session_path)
        except Exception:
            logger.debug("Could not resolve profile Telegram session for credential export", exc_info=True)
        files = []
        for item in candidates:
            try:
                resolved = item.resolve(strict=True)
                if resolved.is_relative_to(home) and resolved.is_file():
                    files.append((resolved, resolved.relative_to(home)))
            except (OSError, ValueError):
                continue
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, relative in files:
                archive.write(path, arcname=relative.as_posix())
        out.chmod(0o600)

    @staticmethod
    def _create_full_backup(home: str, out: Path) -> None:
        from gateway.run import _profile_runtime_scope
        from hermes_cli.backup import run_backup
        with _profile_runtime_scope(Path(home)):
            ok = run_backup(Namespace(output=str(out)))
        if not ok or not out.is_file():
            raise RuntimeError("Profile backup was incomplete")
        try:
            out.chmod(0o600)
        except OSError:
            pass

    async def _receive_import(self, event: Any, source: Any, state: dict[str, Any], text: str) -> bool:
        message = getattr(event, "message", None)
        file_path: Optional[str] = None
        if getattr(message, "document", None) is not None:
            paths, _types, note = await self.adapter._cache_inbound_media(message)
            if note or not paths:
                await self._edit(state, "# Import not received\n\nThe attachment could not be cached. Try another file or cancel.",
                                 inline_markup([[("Cancel", f"ps:{state['id']}:import-cancel", "danger")]]))
                return True
            file_path = paths[0]
        elif text.startswith("{"):
            file_path = None
        else:
            return False
        try:
            if file_path and Path(file_path).suffix.lower() == ".zip":
                preview = self._preview_backup(Path(file_path))
                state["pending_import"] = {"kind": "backup", "path": file_path, "preview": preview}
            else:
                if file_path:
                    raw = Path(file_path).read_text(encoding="utf-8")
                else:
                    raw = text
                payload = json.loads(raw)
                if not isinstance(payload, dict) or payload.get("kind") != "hermes.telegram.personal-settings" or payload.get("version") != 1:
                    raise ValueError("This JSON is not a supported personal-settings export.")
                changes = _validated_changes(payload.get("settings", {}))
                current = load_personal_settings(state["home"], state["user_id"])
                changed = {key: value for key, value in changes.items() if current.get(key) != value}
                state["pending_import"] = {"kind": "preferences", "changes": changed, "preview": self._preference_import_summary(changed)}
                if file_path:
                    Path(file_path).unlink(missing_ok=True)
        except Exception as exc:
            if file_path:
                Path(file_path).unlink(missing_ok=True)
            await self._edit(state, f"# Import not accepted\n\n{self._safe_error(exc)}\n\nNo data was changed. Send a supported file or `/cancel`.",
                             inline_markup([[("Cancel", f"ps:{state['id']}:import-cancel", "danger")]]))
            return True
        state["pending_mode"] = "import-confirm"
        details = state["pending_import"]["preview"]
        self._pending[self._pending_key(Path(state["home"]), source)] = {"state_id": state["id"], "mode": "import-confirm"}
        await self._edit(state, "# Import preview\n\n" + details + "\n\n*Nothing is imported until you confirm.*",
                         inline_markup([[("Confirm import", f"ps:{state['id']}:import-confirm", "primary"),
                                         ("Cancel", f"ps:{state['id']}:import-cancel", "danger")]]))
        return True

    @staticmethod
    def _preview_backup(path: Path) -> str:
        from hermes_cli import backup
        from plugins.platforms.telegram.rich_ui import _code_cell, rich_table
        if not zipfile.is_zipfile(path):
            raise ValueError("The attachment is not a ZIP archive.")
        with zipfile.ZipFile(path, "r") as archive:
            ok, reason = backup._validate_backup_zip(archive)
            if not ok:
                raise ValueError(reason)
            prefix = backup._detect_prefix(archive)
            names = [name for name in archive.namelist() if not name.endswith("/")]
        display = [name[len(prefix):] if prefix and name.startswith(prefix) else name for name in names]
        credentials = sum(1 for name in display if Path(name).name in {".env", "auth.json", "vault.key", "vault.json.enc"} or name.endswith(".session"))
        telegram = sum(1 for name in display if "telegram" in name.lower())
        rows = [
            ("Target", "Active profile"),
            ("Files", str(len(display))),
            ("Credential/session files", str(credentials)),
            ("Telegram-related paths", str(telegram)),
            ("Overwrite scope", "Files with matching paths"),
        ]
        table = rich_table(("Field", "Value"), rows)
        sample = "\n".join(f"- `{_code_cell(name)}`" for name in display[:12]) or "_No files in the archive._"
        if len(display) > 12:
            sample += f"\n- … and {len(display) - 12} more files"
        return (table + "\n\n**Sample paths**\n\n" + sample
                + "\n\n*The gateway will not be restarted by this import flow.*")

    @staticmethod
    def _preference_import_summary(changes: dict[str, Any]) -> str:
        from plugins.platforms.telegram.rich_ui import rich_table
        if not changes:
            return "_No preference values differ from your current settings._"
        rows = []
        for key, value in changes.items():
            label = key.replace("_", " ").title()
            if "instruction" in key:
                summary = f"{len(str(value))} characters"
            elif key in {"default_model", "default_provider", "mcp_mode"}:
                summary = str(value)
            elif isinstance(value, list):
                summary = f"{len(value)} items"
            elif isinstance(value, bool):
                summary = "On" if value else "Off"
            else:
                summary = str(value)
            rows.append((label, summary))
        return ("*Target: this Telegram account's personal settings only.*\n\n"
                + rich_table(("Field", "Incoming value"), rows))

    async def _apply_import(self, state: dict[str, Any], source: Any) -> None:
        pending = state.get("pending_import")
        if not pending:
            await self._render_home(state, note="Import preview expired. Please upload the file again.")
            return
        try:
            if pending["kind"] == "preferences":
                changes = pending["changes"]
                if changes:
                    save_personal_settings(state["home"], state["user_id"], changes)
                summary = f"Imported {len(changes)} personal preference field(s)."
            else:
                restored = await asyncio.to_thread(self._apply_backup_without_restart, state["home"], pending["path"])
                summary = f"Imported {restored} file(s) into this profile. The gateway was not restarted."
            self._cancel_pending(state, source)
            state.pop("pending_import", None)
            path = pending.get("path")
            if path:
                Path(path).unlink(missing_ok=True)
            await self._render_home(state, note=summary)
        except Exception as exc:
            logger.warning("Telegram personal import failed (%s)", type(exc).__name__, exc_info=True)
            self._cancel_pending(state, source)
            await self._render_home(state, note=f"Import failed safely: {self._safe_error(exc)}")

    @staticmethod
    def _apply_backup_without_restart(home: str, archive_path: str) -> int:
        from hermes_cli import backup
        target = Path(home).resolve()
        archive_path = Path(archive_path).resolve()
        if not archive_path.is_file() or not zipfile.is_zipfile(archive_path):
            raise ValueError("The backup archive is missing or invalid.")
        with backup._backup_operation_lock(target):
            with zipfile.ZipFile(archive_path, "r") as archive:
                ok, reason = backup._validate_backup_zip(archive)
                if not ok:
                    raise ValueError(reason)
                prefix = backup._detect_prefix(archive)
                members = [name for name in archive.namelist() if not name.endswith("/")]
                restored, _external, errors, _runtime, _shrunk = backup._import_members(
                    archive, members, prefix, target, len(members),
                )
        if errors:
            raise RuntimeError(f"{len(errors)} file(s) could not be restored")
        return int(restored)

    def _validate_mcp_payload(self, payload: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object with `name` and `config`.")
        name = str(payload.get("name") or "").strip()
        config = payload.get("config")
        if not _MCP_NAME_RE.fullmatch(name):
            raise ValueError("MCP name must start with a letter and contain only letters, numbers, dot, underscore, or hyphen (max 64 characters).")
        if not isinstance(config, dict):
            raise ValueError("`config` must be an object.")
        validate_personal_mcp_config(name, config)
        return name, json.loads(json.dumps(config))

    @staticmethod
    def _safe_error(exc: BaseException) -> str:
        try:
            from hermes_cli.mcp_config import redact_mcp_probe_text
            return redact_mcp_probe_text(str(exc))[:800]
        except Exception:
            return str(exc).replace("\n", " ")[:800]

    @staticmethod
    def _mcp_summary(name: str, config: dict[str, Any]) -> str:
        transport = "HTTP/SSE" if config.get("url") else "stdio"
        lines = [f"**Name:** `{name}`", f"**Transport:** {transport}"]
        if config.get("url"):
            lines.append(f"**Endpoint:** `{str(config['url'])[:500]}`")
        else:
            lines.append(f"**Command:** `{str(config.get('command') or '')[:300]}`")
            args = config.get("args") or []
            lines.append(f"**Arguments:** `{json.dumps(args, ensure_ascii=False)[:800]}`")
        env_keys = sorted((config.get("env") or {}).keys()) if isinstance(config.get("env"), dict) else []
        if env_keys:
            lines.append("**Environment names:** " + ", ".join(f"`{name}`" for name in env_keys))
        headers = sorted((config.get("headers") or {}).keys()) if isinstance(config.get("headers"), dict) else []
        if headers:
            lines.append("**Headers:** " + ", ".join(f"`{name}`" for name in headers) + " (values hidden)")
        return "\n".join(lines)

    async def _save_mcp(self, state: dict[str, Any], source: Any) -> None:
        pending = state.get("pending_mcp")
        if not pending:
            await self._show_mcp(state, "MCP confirmation expired; nothing was saved.")
            return
        name, config = pending["name"], dict(pending["config"])
        config.setdefault("enabled", True)
        settings = load_personal_settings(state["home"], state["user_id"])
        personal_servers = dict(settings.get("personal_mcp_servers") or {})
        if name in personal_servers or name in self._load_profile_mcp(state["home"]):
            raise ValueError("An MCP with that name already exists in profile or personal settings")
        personal_servers[name] = config
        selected = set(settings.get("selected_mcp_servers") or [])
        selected.add(name)
        from hermes_cli import mcp_catalog
        if mcp_catalog.get_entry(name) is not None:
            raise ValueError("This MCP name is reserved by the curated catalog")
        save_personal_mcp_settings(state["home"], state["user_id"], {
            "mcp_mode": "selected", "selected_mcp_servers": sorted(selected),
            "personal_mcp_servers": personal_servers,
        })
        self._cancel_pending(state, source)
        await self._show_mcp(state, f"Saved `{name}` privately and selected it for your Telegram account. It will be available on your next turn.")
