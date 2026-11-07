"""Owner-only Telegram controls for the existing dashboard password provider.

This module configures ``dashboard.basic_auth`` only; it does not implement a
second authentication scheme or start/rebind the dashboard. Secrets are accepted
only in an authorized private chat, deleted before processing, hashed in memory,
and never rendered in a card or logged.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any

from .mtproto_rich import edit_request, inline_markup, send_request

logger = logging.getLogger(__name__)
_PREFIX = "hm:webauth:"
_STATE_TTL = 15 * 60
_INPUT_TTL = 5 * 60
_RECEIPT_TTL = 5 * 60
_EXPIRED_TTL = _STATE_TTL
_MAX_RECEIPTS = 256
_MAX_EXPIRED = 128
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 1024


class TelegramWebAuthUI:
    """Private owner controls for per-profile dashboard username/password auth."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._pending: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._expired: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._receipts: dict[tuple[tuple[str, str, str, str, str], str], float] = {}
        self._lock = asyncio.Lock()

    def _home(self, source: Any) -> Path:
        """Resolve only the adapter's source-specific settings home; never fall back."""
        resolver = getattr(self.adapter, "_settings_home", None)
        if not callable(resolver):
            raise RuntimeError("The source profile resolver is unavailable")
        home = resolver(source)
        if home is None:
            raise RuntimeError("The active profile could not be resolved")
        return Path(home).expanduser().resolve()

    def _allowed(self, source: Any) -> bool:
        if (str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}
                or not getattr(source, "chat_id", None)
                or not str(getattr(source, "user_id", "") or "").isdecimal()):
            return False
        authorized = getattr(self.adapter, "_authorized", None)
        is_owner = getattr(self.adapter, "_is_owner_user", None)
        if not callable(authorized) or not callable(is_owner):
            return False
        try:
            return bool(is_owner(source.user_id, source=source)) and bool(authorized(source))
        except Exception:
            logger.warning("Telegram Web UI auth owner check failed closed")
            return False

    @contextmanager
    def _profile_scope(self, home: str | Path):
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(Path(home)):
            yield

    @staticmethod
    def _identity(home: Path, source: Any) -> tuple[str, str, str, str, str]:
        return (str(home), str(getattr(source, "profile", "") or ""),
                str(getattr(source, "user_id", "") or ""),
                str(getattr(source, "chat_id", "") or ""),
                str(getattr(source, "thread_id", "") or ""))

    def _prune(self) -> None:
        now = time.monotonic()
        for key, pending in list(self._pending.items()):
            if now >= pending["expires_at"]:
                self._pending.pop(key, None)
                self._expired[key] = {"expires_at": now + _EXPIRED_TTL,
                                      "state_id": pending.get("state_id")}
                state = self._states.get(pending.get("state_id"))
                if state:
                    state["stage"] = "idle"
                    state.pop("draft_hash", None)
                    state.pop("draft_username", None)
                    state.pop("hash_before", None)
                    state.pop("plain_before_hash", None)
        for key, tombstone in list(self._expired.items()):
            if now >= tombstone["expires_at"]:
                self._expired.pop(key, None)
        while len(self._expired) > _MAX_EXPIRED:
            self._expired.pop(next(iter(self._expired)))
        for key, expires_at in list(self._receipts.items()):
            if now >= expires_at:
                self._receipts.pop(key, None)
        while len(self._receipts) > _MAX_RECEIPTS:
            self._receipts.pop(next(iter(self._receipts)))
        for state_id, state in list(self._states.items()):
            if now - state["created_at"] > _STATE_TTL:
                if state["identity"] in self._pending:
                    self._expired[state["identity"]] = {
                        "expires_at": now + _EXPIRED_TTL, "state_id": None,
                    }
                self._drop(state_id)

    def _drop(self, state_id: int) -> None:
        state = self._states.pop(state_id, None)
        if state:
            self._pending.pop(state["identity"], None)
            state["stage"] = "idle"
            state.pop("draft_hash", None)
            state.pop("draft_username", None)
            state.pop("hash_before", None)
            state.pop("plain_before_hash", None)

    def _new_state(self, source: Any, home: Path) -> dict[str, Any]:
        self._prune()
        identity = self._identity(home, source)
        for prior_id, prior in list(self._states.items()):
            if prior["identity"] == identity:
                self._drop(prior_id)
        self._counter += 1
        state = {
            "id": self._counter,
            "identity": identity,
            "home": str(home),
            "profile": str(getattr(source, "profile", "") or ""),
            "user_id": str(source.user_id),
            "chat_id": str(source.chat_id),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "message_id": None,
            "created_at": time.monotonic(),
            "stage": "idle",
            "username_before": "",
            "hash_before": "",
            "plain_before_hash": "",
        }
        self._states[state["id"]] = state
        return state

    def _load_config(self, home: str) -> dict[str, Any]:
        from hermes_cli.config_effective import load_user_config_effective
        with self._profile_scope(home):
            config = load_user_config_effective(fail_closed=True)
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _basic(config: dict[str, Any]) -> dict[str, Any]:
        dashboard = config.get("dashboard")
        basic = dashboard.get("basic_auth") if isinstance(dashboard, dict) else None
        return basic if isinstance(basic, dict) else {}

    @staticmethod
    def _table(rows: list[tuple[str, str]]) -> str:
        body = "".join(f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
                       for label, value in rows)
        return f"<table><tbody>{body}</tbody></table>"

    @staticmethod
    def _footer(total: int) -> str:
        return f"<hr/><p><i>Page 1 of 1 • total {total}</i></p>"

    @staticmethod
    def _button(state: dict[str, Any], label: str, action: str, style: str | None = None):
        return (label, f"{_PREFIX}{state['id']}:{action}", style)

    def _home_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        config = self._load_config(state["home"])
        basic = self._basic(config)
        dashboard = config.get("dashboard")
        dashboard = dashboard if isinstance(dashboard, dict) else {}
        configured = bool(basic.get("username") and (basic.get("password_hash") or basic.get("password")))
        with self._profile_scope(state["home"]):
            managed = self._has_profile_auth_env()
        plugin_state = "Environment-managed" if managed else ("Configured" if configured else "Not configured")
        username = "Environment-managed" if managed else (str(basic.get("username")) if configured else "Not shown")
        rows = [("Active profile", state["profile"] or "default"),
                ("Dashboard password authentication", plugin_state),
                ("Loopback login", "Required" if dashboard.get("require_auth_on_loopback") else "Not required"),
                ("Username", username)]
        text = ("<h1>Web UI authentication</h1>"
                "<p>Configure the existing dashboard username/password provider for this private profile. "
                "The password is hashed before it is saved; passwords and password hashes are never shown. "
                "This is Hermes password-form/session authentication, not HTTP Basic. Saving credentials here also "
                "enables the loopback login gate for this profile. It does not change the dashboard bind or expose it publicly. "
                "The signing secret is preserved; existing sessions are not forcibly revoked. Restart the dashboard to apply changes.</p>"
                "<hr/><h2>Current configuration</h2>" + self._table(rows))
        if note:
            text += f"<hr/><p><b>Status:</b> {escape(note)}</p>"
        text += self._footer(len(rows))
        return text, inline_markup([[self._button(state, "Configure credentials", "configure", "primary")]])

    def _prompt(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        stage = state.get("stage")
        descriptions = {
            "current_password": "Enter the current password to authorize changing existing credentials.",
            "username": "Send the username to use for dashboard sign-in.",
            "new_password": "Send the new password. Use at least 12 characters.",
            "confirm_password": "Send the new password again to confirm it.",
            "confirm_save": "Review the action and confirm to save credentials and enable required login on loopback for this profile.",
            "working": "Saving credentials and enabling required login on loopback for this profile…",
        }
        title = "Confirm change" if stage == "confirm_save" else "Configure credentials"
        text = (f"<h1>Web UI authentication</h1><hr/><h2>{escape(title)}</h2>"
                f"<p>{escape(descriptions.get(stage, ''))}</p>")
        if stage in {"current_password", "new_password", "confirm_password"}:
            text += "<p>Send the value as a message in this private chat. The message is deleted before it is checked. Input expires in five minutes.</p>"
        elif stage == "confirm_save":
            text += "<p>Do not type credentials here. Any message is deleted; use the Save button below to confirm.</p>"
        if stage == "confirm_save":
            text += self._table([
                ("Profile", state["profile"] or "default"),
                ("Username", str(state.get("draft_username") or "")),
                ("Loopback login", "Required after dashboard restart"),
            ])
            text += ("<p>Save updates the existing dashboard password provider and enables "
                     "<code>require_auth_on_loopback</code> for this profile. This is password-form/session "
                     "authentication, not HTTP Basic; it does not change the bind or make the dashboard public. "
                     "The signing secret is preserved and existing sessions are not forcibly revoked. "
                     "Restart the dashboard to apply the change.</p>")
        elif stage == "working":
            text += "<p>Working… Do not submit this action again.</p>"
        if note:
            text += f"<p><b>Status:</b> {escape(note)}</p>"
        text += self._footer(3 if stage == "confirm_save" else 1)
        buttons = []
        if stage == "confirm_save":
            buttons.append([self._button(state, "Save credentials", "save", "success")])
        if stage != "working":
            buttons.append([self._button(state, "Cancel", "cancel", "danger")])
        return text, inline_markup(buttons)

    @staticmethod
    def _event_message_id(event: Any) -> Any:
        """Read Telethon NewMessage IDs and callback IDs without conflating their shapes."""
        message = getattr(event, "message", None)
        return (getattr(message, "id", None) or getattr(event, "id", None)
                or getattr(event, "message_id", None))

    def _rotate_state_id(self, state: dict[str, Any]) -> None:
        """Invalidate the callback just used while keeping input-stage references live."""
        old_id = state["id"]
        self._states.pop(old_id, None)
        self._counter += 1
        state["id"] = self._counter
        self._states[state["id"]] = state
        pending = self._pending.get(state["identity"])
        if pending and pending.get("state_id") == old_id:
            pending["state_id"] = state["id"]

    async def _send(self, source: Any, state: dict[str, Any], text: str, markup: Any,
                    reply_to: Any = None) -> None:
        if getattr(self.adapter, "_client", None) is None:
            return
        peer = self.adapter._peer_for(source.chat_id)
        thread = str(getattr(source, "thread_id", "") or "")
        topic_id = int(thread) if thread.isdecimal() else None
        request = send_request(peer, text,
                               reply_to=int(reply_to) if reply_to is not None else None,
                               top_msg_id=topic_id, reply_markup=markup, rich_format="html")
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
            reply_markup=markup, rich_format="html"))

    async def _answer(self, event: Any, text: str = "", alert: bool = False) -> None:
        answer = getattr(event, "answer", None)
        if callable(answer):
            try:
                result = answer(text, alert=alert) if text or alert else answer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("Unable to acknowledge Telegram Web UI auth callback")

    def _state_for_callback(self, event: Any, source: Any, state_id: int) -> tuple[dict[str, Any] | None, Any]:
        self._prune()
        state = self._states.get(state_id)
        if not state or not self._allowed(source):
            return None, source
        if (state["profile"] != str(getattr(source, "profile", "") or "")
                or state["user_id"] != str(getattr(source, "user_id", "") or "")
                or state["chat_id"] != str(getattr(source, "chat_id", "") or "")
                or state["message_id"] != str(getattr(event, "message_id", "") or "")):
            return None, source
        bound_source = source
        incoming_thread = str(getattr(source, "thread_id", "") or "")
        if incoming_thread and incoming_thread != state["thread_id"]:
            return None, source
        if not incoming_thread and state["thread_id"]:
            try:
                from gateway.session_identity import replace_source
                bound_source = replace_source(source, thread_id=state["thread_id"])
            except (TypeError, ValueError):
                return None, source
        try:
            if str(self._home(bound_source)) != state["home"]:
                return None, source
        except Exception:
            return None, source
        return state, bound_source

    def _set_stage(self, state: dict[str, Any], stage: str) -> None:
        self._pending.pop(state["identity"], None)
        state["stage"] = stage
        if stage in {"current_password", "username", "new_password", "confirm_password", "confirm_save", "working"}:
            self._pending[state["identity"]] = {
                "state_id": state["id"], "stage": stage,
                "expires_at": time.monotonic() + _INPUT_TTL,
            }

    @staticmethod
    def _has_profile_auth_env() -> bool:
        """Check profile overrides through Hermes' scoped secret reader."""
        from agent.secret_scope import current_secret_scope, get_secret_str, serves_routed_profile
        names = (
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
        )
        if serves_routed_profile():
            scope = current_secret_scope()
            if scope is None:
                raise RuntimeError("The profile secret scope is unavailable")
            return any(str(scope.get(name, "") or "").strip() for name in names)
        return any(get_secret_str(name).strip() for name in names)

    async def handle_command(self, source: Any, arguments: str = "", reply_to: Any = None) -> None:
        if not self._allowed(source):
            await self.adapter.send(source.chat_id, "Web UI authentication setup is available only to the authorized owner in a private chat.", reply_to=reply_to)
            return
        if str(arguments or "").strip():
            await self.adapter.send(source.chat_id, "For privacy, use this command without arguments. Never include usernames or passwords in a slash command; send credential inputs only when prompted.", reply_to=reply_to)
            return
        try:
            home = self._home(source)
            state = self._new_state(source, home)
            self._expired.pop(state["identity"], None)
            text, markup = self._home_view(state)
            await self._send(source, state, text, markup, reply_to=reply_to)
        except Exception as exc:
            logger.warning("Telegram Web UI auth configuration could not be displayed (%s)", type(exc).__name__)
            await self.adapter.send(source.chat_id, "Web UI authentication setup could not safely resolve this profile. No settings were changed.", reply_to=reply_to)

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not str(data).startswith(_PREFIX):
            return False
        async with self._lock:
            pieces = str(data)[len(_PREFIX):].split(":", 1)
            if len(pieces) != 2 or not pieces[0].isdigit():
                await self._answer(event, "This action is unavailable or expired.", True)
                return True
            state, bound_source = self._state_for_callback(event, source, int(pieces[0]))
            if state is None:
                await self._answer(event, "This action is expired or not authorized for this account.", True)
                return True
            action = pieces[1]
            if action == "configure" and state["stage"] == "idle":
                self._rotate_state_id(state)
                try:
                    with self._profile_scope(state["home"]):
                        if self._has_profile_auth_env():
                            text, markup = self._home_view(state, "Dashboard auth is environment-managed; no profile credentials were changed.")
                            await self._edit(state, text, markup)
                            await self._answer(event, "Dashboard auth is environment-managed; no profile credentials were changed.", True)
                            return True
                    setup_ui = getattr(self.adapter, "_telegram_setup_ui", None)
                    if setup_ui is not None:
                        setup_ui.cancel_for_source(bound_source)
                    config = self._load_config(state["home"])
                    basic = self._basic(config)
                    old_username = str(basic.get("username") or "")
                    old_hash = str(basic.get("password_hash") or "")
                    old_plain = str(basic.get("password") or "")
                    state["username_before"] = old_username
                    state["hash_before"] = old_hash
                    state.pop("plain_before_hash", None)
                    if old_plain:
                        from plugins.dashboard_auth.basic import hash_password
                        state["plain_before_hash"] = hash_password(old_plain)
                    if old_username and (old_hash or old_plain):
                        self._set_stage(state, "current_password")
                        note = "Existing credentials detected. Verify the current password before replacing them."
                    else:
                        self._set_stage(state, "username")
                        note = "No existing complete credentials were found."
                    text, markup = self._prompt(state, note)
                    await self._edit(state, text, markup)
                    await self._answer(event)
                except Exception as exc:
                    logger.warning("Telegram Web UI auth setup could not start (%s)", type(exc).__name__)
                    await self._answer(event, "Could not start profile auth setup. No settings were changed.", True)
                return True
            if action == "save" and state["stage"] == "confirm_save":
                self._rotate_state_id(state)
                self._set_stage(state, "working")
                try:
                    text, markup = self._prompt(state)
                    await self._edit(state, text, markup)
                    await self._answer(event, "Saving credentials…")
                    await asyncio.to_thread(self._save_credentials, state)
                    self._pending.pop(state["identity"], None)
                    state["stage"] = "idle"
                    state.pop("draft_hash", None)
                    state.pop("draft_username", None)
                    state.pop("hash_before", None)
                    state.pop("plain_before_hash", None)
                    text, markup = self._home_view(state, "Credentials saved as a password hash; login is now required on loopback for this profile. Restart the dashboard to apply the change. Existing sessions are not forcibly revoked.")
                    await self._edit(state, text, markup)
                    await self._answer(event, "Credentials saved.")
                except Exception as exc:
                    logger.warning("Telegram Web UI auth config save failed (%s)", type(exc).__name__)
                    self._pending.pop(state["identity"], None)
                    state["stage"] = "idle"
                    state.pop("draft_hash", None)
                    state.pop("draft_username", None)
                    state.pop("hash_before", None)
                    state.pop("plain_before_hash", None)
                    try:
                        text, markup = self._home_view(state, "Save could not be confirmed. Reopen this screen and read back the profile configuration before deciding whether to try again.")
                        await self._edit(state, text, markup)
                    except Exception:
                        logger.warning("Telegram Web UI auth save outcome warning could not be displayed")
                    await self._answer(event, "Save outcome could not be confirmed. Reopen and read back this profile's configuration; do not retry automatically.", True)
                return True
            if action == "cancel" and state["stage"] != "idle":
                self._rotate_state_id(state)
                self._set_stage(state, "idle")
                state.pop("draft_hash", None)
                state.pop("draft_username", None)
                state.pop("hash_before", None)
                state.pop("plain_before_hash", None)
                text, markup = self._home_view(state, "Credential setup cancelled. No settings were changed.")
                await self._edit(state, text, markup)
                await self._answer(event)
                return True
            await self._answer(event, "This action is unavailable or already used.", True)
            return True

    async def _delete_input(self, source: Any, event: Any) -> bool:
        message_id = self._event_message_id(event)
        client = getattr(self.adapter, "_client", None)
        delete_messages = getattr(client, "delete_messages", None)
        if not message_id or not callable(delete_messages):
            return False
        try:
            result = delete_messages(self.adapter._peer_for(source.chat_id), [int(message_id)])
            if asyncio.iscoroutine(result):
                await result
            return True
        except Exception as exc:
            logger.warning("Telegram credential input deletion failed (%s)", type(exc).__name__)
            return False

    async def _visible_warning(self, source: Any, message: str,
                               state: dict[str, Any] | None = None) -> None:
        if state and self._states.get(state["id"]) is state:
            try:
                renderer = self._home_view if state["stage"] == "idle" else self._prompt
                text, markup = renderer(state, message)
                await self._edit(state, text, markup)
                return
            except Exception:
                logger.warning("Telegram Web UI auth warning could not update its card")
        try:
            text = ("<h1>Web UI authentication</h1><hr/>"
                    f"<p>{escape(message)}</p>" + self._footer(1))
            await self._send(source, {}, text, None)
        except Exception:
            logger.warning("Telegram Web UI auth warning could not be delivered")

    @staticmethod
    def _is_gateway_command(raw: str) -> bool:
        raw = raw.lstrip()
        if not raw.startswith("/"):
            return False
        token = raw.split(maxsplit=1)[0][1:].lower().split("@", 1)[0]
        if not token:
            return False
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        from hermes_cli.commands_platforms import TELEGRAM_CONTROL_COMMANDS
        known = set(GATEWAY_KNOWN_COMMANDS)
        known.update(name.lower() for name, _description, _args, _category in TELEGRAM_CONTROL_COMMANDS)
        return token.replace("_", "-") in known

    def _accept_current_password(self, state: dict[str, Any], raw: str) -> str:
        from plugins.dashboard_auth.basic import _verify_password
        encoded = state.get("hash_before") or state.get("plain_before_hash", "")
        if not _verify_password(raw, encoded):
            return "Current password was not accepted. Try again or /cancel."
        self._set_stage(state, "username")
        return ""

    def _accept_username(self, state: dict[str, Any], raw: str) -> str:
        username = raw.strip()
        if (not username or len(username) > 128 or ":" in username
                or any(ord(char) < 32 or ord(char) == 127 for char in username)):
            return "Use a non-empty username up to 128 characters; colons and control characters are not allowed."
        state["draft_username"] = username
        self._set_stage(state, "new_password")
        return ""

    def _accept_new_password(self, state: dict[str, Any], raw: str) -> str:
        if len(raw) < _MIN_PASSWORD_LENGTH or len(raw) > _MAX_PASSWORD_LENGTH:
            return "Use a password between 12 and 1024 characters."
        from plugins.dashboard_auth.basic import hash_password
        state["draft_hash"] = hash_password(raw)
        self._set_stage(state, "confirm_password")
        return ""

    def _accept_password_confirmation(self, state: dict[str, Any], raw: str) -> str:
        from plugins.dashboard_auth.basic import _verify_password
        if not _verify_password(raw, state.get("draft_hash", "")):
            state.pop("draft_hash", None)
            self._set_stage(state, "new_password")
            return "Those entries did not match. Enter a new password again."
        self._set_stage(state, "confirm_save")
        return ""

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        """Consume only exact-identity pending inputs; secret messages are deleted first."""
        raw = "" if text is None else str(text)
        if not self._allowed(source):
            return False
        try:
            home = self._home(source)
            identity = self._identity(home, source)
        except Exception:
            # If a pending secret's profile vanished, consume rather than forwarding it to the agent.
            matches = [key for key in self._pending if key[1:] == self._identity(Path("/"), source)[1:]]
            if matches:
                deleted = await self._delete_input(source, event)
                for key in matches:
                    pending = self._pending.pop(key, None)
                    if pending:
                        self._drop(pending["state_id"])
                if not deleted:
                    await self._visible_warning(source, "Credential input could not be deleted from Telegram. It was not processed; delete it manually.")
                return True
            return False
        self._prune()
        message_id = self._event_message_id(event)
        receipt_key = (identity, str(message_id)) if message_id is not None else None
        if receipt_key is not None and receipt_key in self._receipts:
            if not await self._delete_input(source, event):
                await self._visible_warning(source, "This duplicate message could not be deleted. It was not processed; delete it manually.")
            return True
        command = raw.strip().split(maxsplit=1)[0].lower() if raw.strip().startswith("/") else ""
        cancel = command == "/cancel" or command.startswith("/cancel@")
        tombstone = self._expired.get(identity)
        if tombstone:
            if cancel:
                self._expired.pop(identity, None)
                state = self._states.get(tombstone.get("state_id"))
                await self._visible_warning(source, "Expired credential setup cancelled. No settings were changed.", state)
                return True
            if self._is_gateway_command(raw):
                self._expired.pop(identity, None)
                return False
            if not await self._delete_input(source, event):
                await self._visible_warning(source, "Credential input expired, but Telegram could not delete this message. Delete it manually; it was not processed.")
            if receipt_key is not None:
                self._receipts[receipt_key] = time.monotonic() + _RECEIPT_TTL
            state = self._states.get(tombstone.get("state_id"))
            await self._visible_warning(source, "Credential input expired. It was not processed; reopen Web UI authentication to start again.", state)
            return True
        pending = self._pending.get(identity)
        if not pending:
            # Reject, delete, and warn on inputs arriving in a sibling/missing thread;
            # never repair their identity to the pending topic.
            sibling = next((key for key in self._pending
                            if key[:4] == identity[:4] and key[4] != identity[4]), None)
            if sibling:
                if not await self._delete_input(source, event):
                    await self._visible_warning(source, "Credential input came from a different thread and could not be deleted. It was not processed; delete it manually.")
                else:
                    await self._visible_warning(source, "Credential input came from a different thread. It was deleted and not processed; continue in the original thread.")
                return True
            return False
        if command:
            if cancel:
                state = self._states.get(pending["state_id"])
                if state:
                    self._set_stage(state, "idle")
                    state.pop("draft_hash", None)
                    state.pop("draft_username", None)
                    state.pop("hash_before", None)
                    state.pop("plain_before_hash", None)
                    await self._visible_warning(source, "Credential setup cancelled. No settings were changed.", state)
                return True
            if self._is_gateway_command(raw):
                state = self._states.get(pending["state_id"])
                if state:
                    self._drop(state["id"])
                return False
        if not await self._delete_input(source, event):
            state = self._states.get(pending["state_id"])
            if state:
                self._drop(state["id"])
            await self._visible_warning(source, "Credential input could not be deleted from Telegram. Setup was cancelled and the message was not processed; delete it manually.", state)
            return True
        if receipt_key is not None:
            self._receipts[receipt_key] = time.monotonic() + _RECEIPT_TTL
        async with self._lock:
            pending = self._pending.get(identity)
            state = self._states.get(pending["state_id"]) if pending else None
            if not pending or not state or state["identity"] != identity:
                return True
            if time.monotonic() >= pending["expires_at"]:
                self._pending.pop(identity, None)
                self._expired[identity] = {"expires_at": time.monotonic() + _EXPIRED_TTL,
                                           "state_id": state["id"]}
                state["stage"] = "idle"
                state.pop("draft_hash", None)
                state.pop("draft_username", None)
                await self._visible_warning(source, "Credential input expired. It was not processed; reopen Web UI authentication to start again.", state)
                return True
            stage = pending["stage"]
            try:
                handlers = {
                    "current_password": self._accept_current_password,
                    "username": self._accept_username,
                    "new_password": self._accept_new_password,
                    "confirm_password": self._accept_password_confirmation,
                }
                handler = handlers.get(stage)
                note = (handler(state, raw) if handler is not None else
                        "Please use the Save button to confirm these credentials; typed messages are deleted and cannot confirm the save.")
                text_out, markup = self._prompt(state, note)
                await self._edit(state, text_out, markup)
                return True
            except Exception as exc:
                logger.warning("Telegram Web UI auth input processing failed (%s)", type(exc).__name__)
                self._drop(state["id"])
                return True

    def _save_credentials(self, state: dict[str, Any]) -> None:
        """Persist only after current-password and new-password confirmations."""
        from hermes_cli.config import load_config, save_config
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.plugins_cmd import ensure_basic_auth_plugin_enabled_in_config
        username = str(state.get("draft_username") or "")
        password_hash = str(state.get("draft_hash") or "")
        if not username or not password_hash:
            raise RuntimeError("Confirmed credentials are missing")
        with self._profile_scope(state["home"]):
            if self._has_profile_auth_env():
                raise RuntimeError("Dashboard auth is environment-managed")
            cfg = load_config()
            dashboard = cfg.setdefault("dashboard", {})
            if not isinstance(dashboard, dict):
                raise RuntimeError("Invalid dashboard configuration")
            basic = dashboard.setdefault("basic_auth", {})
            if not isinstance(basic, dict):
                raise RuntimeError("Invalid basic auth configuration")
            current_username = str(basic.get("username") or "")
            current_hash = str(basic.get("password_hash") or "")
            current_plain = str(basic.get("password") or "")
            # A separate, one-shot current-password verification happened before drafting.
            # Detect edits/races so that confirmation cannot overwrite credentials changed meanwhile.
            if (current_username != state.get("username_before", "")
                    or current_hash != state.get("hash_before", "")):
                raise RuntimeError("Credentials changed during setup")
            if state.get("plain_before_hash"):
                from plugins.dashboard_auth.basic import _verify_password
                if not _verify_password(current_plain, state["plain_before_hash"]):
                    raise RuntimeError("Credentials changed during setup")
            elif current_plain:
                raise RuntimeError("Credentials changed during setup")
            basic["username"] = username
            basic["password_hash"] = password_hash
            basic["password"] = ""
            if not str(basic.get("secret", "") or "").strip():
                basic["secret"] = secrets.token_urlsafe(32)
            ensure_basic_auth_plugin_enabled_in_config(cfg)
            dashboard["require_auth_on_loopback"] = True
            save_config(cfg)
            reread = load_user_config_effective(fail_closed=True)
            actual = self._basic(reread if isinstance(reread, dict) else {})
            reread_dashboard = (reread.get("dashboard") if isinstance(reread, dict) else None)
            reread_dashboard = reread_dashboard if isinstance(reread_dashboard, dict) else {}
            if (actual.get("username") != username or actual.get("password_hash") != password_hash
                    or actual.get("password") or reread_dashboard.get("require_auth_on_loopback") is not True):
                raise RuntimeError("Saved dashboard auth configuration did not read back")
