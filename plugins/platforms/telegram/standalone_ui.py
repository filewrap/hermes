"""Owner-only Telegram controls for the existing browser and dashboard services.

This is a thin UI over Hermes' existing CDP, config and dashboard lifecycle APIs. It
never starts a browser or exposes credentials. Callback payloads are short-lived,
one-shot capabilities bound to the owner, private chat, profile, thread and exact
RichMessage.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import contextmanager
import logging
import os
import subprocess
import sys
import threading
import time
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from plugins.platforms.telegram.mtproto_rich import inline_markup

logger = logging.getLogger(__name__)
_PREFIX = "hm:standalone:"
_TTL = 300


class TelegramStandaloneUI:
    """Private, owner-gated controls over supported existing Hermes service APIs."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._next_id = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._service_lock = threading.Lock()

    def _home(self, source: Any) -> Path:
        settings_home = getattr(self.adapter, "_settings_home", None)
        if callable(settings_home):
            home = settings_home(source)
            if home is None:
                raise RuntimeError("The active profile could not be resolved")
            return Path(home).expanduser().resolve()
        runner = getattr(self.adapter, "gateway_runner", None)
        resolver = getattr(runner, "_resolve_profile_home_for_source", None)
        if callable(resolver):
            home = resolver(source)
            if home is None:
                raise RuntimeError("The active profile could not be resolved")
        else:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        return Path(home).expanduser().resolve()

    def _authorized(self, source: Any) -> bool:
        if (str(getattr(source, "chat_type", "")) not in {"dm", "private", "direct"}
                or not getattr(source, "chat_id", None)
                or not str(getattr(source, "user_id", "") or "").isdecimal()):
                return False
        checker = getattr(self.adapter, "_is_owner_user", None)
        admission = getattr(self.adapter, "_authorized", None)
        try:
            return bool(callable(checker) and checker(source.user_id, source)
                        and callable(admission) and admission(source))
        except Exception:
            logger.warning("Telegram standalone owner check failed closed", exc_info=True)
            return False

    @contextmanager
    def _profile_scope(self, home: Path):
        """Re-enter the callback's owning profile for every config/service operation."""
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(home):
            yield

    def _prune(self) -> None:
        now = time.monotonic()
        for sid, state in list(self._states.items()):
            if now - state["created"] > _TTL:
                self._states.pop(sid, None)

    def _new_state(self, source: Any, view: str, message_id: Any = None) -> dict[str, Any]:
        self._prune()
        self._next_id += 1
        state = {
            "id": self._next_id,
            "created": time.monotonic(),
            "home": str(self._home(source)),
            "profile": str(getattr(source, "profile", "") or ""),
            "user_id": str(getattr(source, "user_id", "") or ""),
            "chat_id": str(getattr(source, "chat_id", "") or ""),
            "thread_id": str(getattr(source, "thread_id", "") or ""),
            "message_id": str(message_id) if message_id is not None else None,
            "view": view,
        }
        self._states[state["id"]] = state
        return state

    def _matches(self, state: dict[str, Any], source: Any, event: Any) -> bool:
        if not self._authorized(source):
            return False
        try:
            identity_matches = (
                time.monotonic() - state["created"] <= _TTL
                and
                state["home"] == str(self._home(source))
                and state["profile"] == str(getattr(source, "profile", "") or "")
                and state["user_id"] == str(getattr(source, "user_id", "") or "")
                and state["chat_id"] == str(getattr(source, "chat_id", "") or "")
                and (getattr(source, "thread_id", None) is None
                     or state["thread_id"] == str(source.thread_id))
            )
            query = getattr(event, "query", None)
            message = getattr(query, "message", None)
            event_message_id = getattr(message, "id", None) or getattr(event, "message_id", None)
            return bool(identity_matches and state.get("message_id") and event_message_id is not None
                        and str(state["message_id"]) == str(event_message_id))
        except Exception:
            return False

    @staticmethod
    def _message_id(adapter: Any, result: Any) -> str | None:
        getter = getattr(adapter, "_sent_message_id", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                value = getter(result)
                if value is not None:
                    return str(value)
        for obj in (getattr(result, "message", None), result):
            value = getattr(obj, "id", None)
            if value is not None:
                return str(value)
        return None

    async def _send(self, source: Any, state: dict[str, Any], text: str, markup: Any, reply_to=None) -> None:
        sender = getattr(self.adapter, "_send_rich_ui", None)
        if not callable(sender):
            raise RuntimeError("Telegram RichMessage sender is unavailable")
        result = await sender(source, text, markup, reply_to=reply_to)
        state["message_id"] = self._message_id(self.adapter, result)

    async def _edit(self, event: Any, source: Any, state: dict[str, Any], text: str, markup: Any) -> None:
        editor = getattr(self.adapter, "_edit_rich_ui_message", None)
        if not callable(editor):
            raise RuntimeError("Telegram RichMessage editor is unavailable")
        if event is None:
            from types import SimpleNamespace
            event = SimpleNamespace(message_id=state.get("message_id"))
        result = await editor(event, source, text, markup)
        if state.get("message_id") is None:
            state["message_id"] = self._message_id(self.adapter, result)

    @staticmethod
    def _button(state: dict[str, Any], action: str) -> str:
        return f"{_PREFIX}{state['id']}:{action}"

    def _push_control_frame(self, source: Any, state: dict[str, Any]) -> None:
        """Let the platform's shared control-card router restore nested native views."""
        push = getattr(self.adapter, "_push_control_frame", None)
        if callable(push):
            push(source, state.get("message_id"), {"kind": "standalone", "view": state["view"]})

    def _home_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        text = "<h1>Hermes setup</h1><p>Open the setup centre to choose a control.</p>"
        if note:
            text += f"<p>{escape(note)}</p>"
        text += "<hr/><p><i>Page 1 of 1 • total 1</i></p>"
        markup = inline_markup([[('Open setup', self._button(state, "setup"), "primary")]])
        return text, markup

    def _browser_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        global_override = bool(os.environ.get("BROWSER_CDP_URL", "").strip())
        cfg = self._load_config()
        browser_cfg = cfg.get("browser") if isinstance(cfg.get("browser"), dict) else {}
        backend = str(browser_cfg.get("backend") or "").strip().casefold()
        if browser_cfg.get("backend") is False:
            backend = "off"  # YAML 1.1 parses an unquoted `off` as false; runtime treats it as disabled.
        provider = str(browser_cfg.get("cloud_provider") or "").strip() or "auto"
        engine = str(browser_cfg.get("engine") or "auto").strip().casefold()
        profile_endpoint = str(browser_cfg.get("cdp_url") or "").strip()
        override_endpoint = os.environ.get("BROWSER_CDP_URL", "").strip()
        active_endpoint = override_endpoint or profile_endpoint
        if backend == "browser-use":
            mode = "Browser Use CLI (explicit)"
            try:
                from tools.browser_use_cli import _find_cli
                if _find_cli() is None:
                    mode += " — not found; install via `hermes tools` → Browser Automation → Browser Use"
            except Exception:
                mode += " (installation status unavailable)"
        elif backend == "off":
            mode = "Built-in browser tools (explicit)"
        elif backend:
            mode = f"Unsupported configured backend ({backend}); runtime fallback applies"
        else:
            mode = "Unset — runtime default"
            try:
                from tools.browser_use_cli import _find_cli
                mode += (" (built-in fallback; Browser Use CLI not found)" if _find_cli() is None
                         else " (Browser Use CLI when available)")
            except Exception:
                mode += " (runtime decides)"
        if global_override:
            mode += " • process CDP override takes precedence"
        endpoint_display = "Configured (value hidden)" if active_endpoint else "Not configured"
        rows = [
            ("Saved backend", mode),
            ("Saved cloud provider", provider),
            ("Saved local engine", engine),
            ("CDP attachment", endpoint_display),
            ("Endpoint source", "Process-wide override" if global_override else ("This profile" if profile_endpoint else "None")),
        ]
        lines = [
            "<h1>Browser service</h1><p>Backend selects Browser Use CLI vs built-in tools; cloud provider selects a registered cloud session provider; engine selects the local browser engine; CDP attachment connects to an already-running browser. These are separate controls.</p><p>Selections save to this profile. Existing sessions and runtime-cached choices do not change until the gateway restarts.</p><hr/>",
            self._html_table(rows),
            "<p>These rows show saved preferences, not a probe of an existing browser session. Process overrides, Camofox and provider availability can affect actual runtime routing. No restart is performed here.</p>",
            "<p>Connect accepts only a credential-free loopback URL, such as <code>http://127.0.0.1:9222</code>. The endpoint must be reachable from the Hermes gateway host; <code>localhost</code> on your Telegram phone or PC is a different machine and will not attach.</p>",
            "<p>On Linux, start Chromium on the Hermes host with remote debugging enabled, for example: <code>chromium --remote-debugging-port=9222 --user-data-dir=\"$HOME/.cache/hermes-chromium\"</code>. Keep debugging bound to loopback. If Browser Use CLI is not installed, run <code>hermes tools</code> → Browser Automation → Browser Use. Configure provider credentials/sign-in through its supported setup; Telegram does not collect secrets or perform OAuth.</p>",
        ]
        if note:
            lines.append(f"<p>Status: {escape(note)}</p>")
        lines.append(f"<hr/><p><i>Page 1 of 1 • total {len(rows)}</i></p>")
        buttons = [("Refresh", self._button(state, "browser"), None),
                   ("Connect local CDP", self._button(state, "browser-connect"), "success"),
                   ("Disconnect CDP", self._button(state, "browser-disconnect"), "danger"),
                   ("Backend…", self._button(state, "browser-backends"), None),
                   ("Engine…", self._button(state, "browser-engines"), None),
                   ("Provider…", self._button(state, "browser-providers"), None),
                   ("← Back to setup", self._button(state, "setup"), "primary")]
        return "".join(lines), inline_markup([[button] for button in buttons])

    @staticmethod
    def _sanitize_cdp_display(endpoint: str) -> str:
        """Show a CDP endpoint only after the shared URL credential redactor."""
        from agent.redact import redact_cdp_url
        return redact_cdp_url(endpoint)

    @staticmethod
    def _browser_provider_options() -> tuple[tuple[str, str], ...]:
        """List provider names from the profile-scoped registry, not a UI copy."""
        try:
            from hermes_cli.plugins import _ensure_plugins_discovered
            _ensure_plugins_discovered()
            from agent.browser_registry import list_providers
            providers = tuple((str(provider.name).strip(), str(provider.name).strip())
                              for provider in list_providers())
        except Exception:
            logger.warning("Could not enumerate Telegram browser providers", exc_info=True)
            providers = ()
        options = [("auto", "Automatic (runtime default)"), ("local", "Local browser (no cloud provider)")]
        options.extend((key, label) for key, label in providers if key not in {"auto", "local"})
        return tuple(options)

    def _browser_selector_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        view = state.get("view")
        cfg = self._load_config()
        browser_cfg = cfg.get("browser") if isinstance(cfg.get("browser"), dict) else {}
        if view == "browser-backends":
            key, title, options = "backend", "Browser backend", (
                ("auto", "Unset (runtime default)"),
                ("browser-use", "Browser Use CLI"),
                ("off", "Built-in browser tools"),
            )
            raw_backend = browser_cfg.get(key)
            current = "off" if raw_backend is False else str(raw_backend or "auto").strip().casefold()
        elif view == "browser-engines":
            key, title, options = "engine", "Local browser engine", (
                ("auto", "Automatic"), ("chrome", "Chrome / Chromium"),
                ("lightpanda", "Lightpanda"),
            )
            current = str(browser_cfg.get(key) or "auto").strip().casefold()
        else:
            key, title, options = "cloud_provider", "Cloud browser provider", self._browser_provider_options()
            current = str(browser_cfg.get(key) or "auto").strip()
        choices = []
        for value, label in options:
            selected = value.casefold() == current
            choices.append([(f"{label}{' (current)' if selected else ''}",
                             self._button(state, f"browser-select:{key}:{value}"),
                             "success" if selected else None)])
        choices.append([("← Back", self._button(state, "browser"), None)])
        text = f"<h1>{escape(title)}</h1><p>Choose an explicit profile setting. Changes require confirmation and preserve unrelated config.</p>"
        if view == "browser-backends":
            text += "<p>Install Browser Use CLI via <code>hermes tools</code> → Browser Automation → Browser Use. Unset preserves the runtime default (CLI when runnable, otherwise built-in fallback); <code>off</code> explicitly selects built-in browser tools.</p>"
        elif view == "browser-engines":
            text += "<p>This selects the local browser engine. Lightpanda must already be installed and runnable; this screen does not install it or claim OAuth/sign-in completion.</p>"
        else:
            text += "<p>Only providers registered for this profile are listed. Selecting one does not create credentials or sign in; follow its supported provider/plugin setup, then restart the gateway for runtime-cached choices.</p>"
        if note:
            text += f"<p>Status: {escape(note)}</p>"
        text += f"<hr/><p><i>Page 1 of 1 • total {len(options)}</i></p>"
        return text, inline_markup(choices)

    def _browser_confirmation_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        pending = state.get("pending_browser_setting") or {}
        key = str(pending.get("key") or "")
        value = str(pending.get("value") or "")
        label = {"backend": "browser backend", "engine": "local browser engine",
                 "cloud_provider": "cloud browser provider"}.get(key, "browser setting")
        text = (f"<h1>Confirm browser change</h1><p>Set this profile's {escape(label)} to "
                f"<b>{escape(value or 'automatic runtime default')}</b>?</p>"
                "<p>This saves config only. It does not relaunch the gateway or close existing sessions; runtime-cached choices take effect after the gateway is restarted.</p>")
        if key == "backend" and value == "browser-use":
            text += "<p>If the CLI is missing, install it with <code>hermes tools</code> → Browser Automation → Browser Use. Telegram does not install it.</p>"
        elif key == "engine" and value == "lightpanda":
            text += "<p>Lightpanda must already be installed and runnable; this selection does not install it.</p>"
        elif key == "cloud_provider" and value not in {"", "auto", "local"}:
            text += "<p>Selection does not create credentials or sign in or complete OAuth. Use that provider's supported setup before running browser tasks.</p>"
        if note:
            text += f"<p>Status: {escape(note)}</p>"
        text += "<hr/><p><i>Page 1 of 1 • total 2</i></p>"
        return text, inline_markup([[("Confirm change", self._button(state, "browser-apply"), "danger"),
                                     ("← Back", self._button(state, "browser-cancel"), None)]])

    def _dashboard_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        cfg = self._load_config()
        dash = cfg.get("dashboard") if isinstance(cfg.get("dashboard"), dict) else {}
        theme = str(dash.get("theme") or "default")
        running = self._dashboard_status(state.get("home"))
        process_status = "Unavailable" if running is None else str(len(running))
        basic = dash.get("basic_auth") if isinstance(dash.get("basic_auth"), dict) else {}
        configured = bool(basic.get("username") and (basic.get("password_hash") or basic.get("password")))
        rows = [("Theme", theme), ("Live processes for this profile", process_status),
                ("Configuration", "Saved to this profile's config.yaml"),
                ("Password login", "Configured; open Authentication for details" if configured else "Not configured"),
                ("Loopback login gate", "Required on next startup" if dash.get("require_auth_on_loopback") is True else "Not required"),
                ("Start bind", "Loopback only; public bind unavailable")]
        lines = ["<h1>Dashboard / WebUI</h1><p>Start or stop this profile's local service. Stop includes its Desktop/serve backends.</p><hr/>", self._html_table(rows)]
        if note:
            lines.append(f"<p>Status: {escape(note)}</p>")
        lines.append(f"<hr/><p><i>Page 1 of 1 • total {len(rows)}</i></p>")
        rows: list[list[tuple[str, str, str | None]]] = []
        rows.append([("Theme: " + theme, self._button(state, "themes"), "primary")])
        rows.append([("Authentication", self._button(state, "auth"), "primary")])
        rows.append([("Refresh status", self._button(state, "dashboard"), None),
                     ("Start local WebUI", self._button(state, "dashboard-start"), "success")])
        rows.append([("Stop this profile's WebUI", self._button(state, "dashboard-stop"), "danger")])
        rows.append([("← Back to setup", self._button(state, "setup"), "primary")])
        return "".join(lines), inline_markup(rows)

    def _dashboard_confirmation_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        operation = str((state.get("pending_dashboard_action") or {}).get("operation") or "")
        if operation == "dashboard-start":
            title = "Confirm WebUI start"
            detail = "Start this profile's isolated WebUI bound to loopback (127.0.0.1:9119)?"
            label = "Start WebUI"
        elif operation == "dashboard-stop":
            title = "Confirm WebUI stop"
            detail = "Stop dashboard/serve processes owned by this profile? Other profiles and unrelated processes are left alone."
            label = "Stop WebUI"
        else:
            title, detail, label = "Confirm WebUI action", "No valid operation is pending.", "Confirm"
        text = f"<h1>{escape(title)}</h1><p>{escape(detail)}</p><p>This action runs only after you confirm.</p>"
        if note:
            text += f"<p>Status: {escape(note)}</p>"
        text += "<hr/><p><i>Page 1 of 1 • total 2</i></p>"
        action = "dashboard-confirm-start" if operation == "dashboard-start" else "dashboard-confirm-stop"
        return text, inline_markup([[(label, self._button(state, action), "danger"),
                                     ("← Back", self._button(state, "dashboard-cancel"), None)]])

    def _themes_view(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        current = str((self._load_config().get("dashboard") or {}).get("theme") or "default")
        themes = self._theme_names()
        buttons = [[(f"{theme}{' (current)' if theme == current else ''}",
                     self._button(state, f"theme:{theme}"), "success" if theme == current else None)]
                   for theme in themes]
        buttons.append([("← Back", self._button(state, "dashboard"), None)])
        text = "<h1>Dashboard theme</h1><p>Choose a bundled theme. This saves the setting; a running WebUI may need a refresh.</p><hr/>"
        text += self._html_table([("Available theme", theme) for theme in themes])
        if note:
            text += f"<p>Status: {escape(note)}</p>"
        text += f"<hr/><p><i>Page 1 of 1 • total {len(themes)}</i></p>"
        return text, inline_markup(buttons)

    @staticmethod
    def _html_table(rows: list[tuple[str, str]]) -> str:
        body = "".join(f"<tr><td>{escape(key)}</td><td>{escape(value)}</td></tr>" for key, value in rows)
        return f"<table><thead><tr><th>Setting</th><th>Value</th></tr></thead><tbody>{body}</tbody></table>"

    @staticmethod
    def _theme_names() -> tuple[str, ...]:
        from hermes_cli.web_server_dashboard import _BUILTIN_DASHBOARD_THEMES
        return tuple(theme["name"] for theme in _BUILTIN_DASHBOARD_THEMES)

    @staticmethod
    def _usage(name: str) -> str:
        if name == "browser":
            return "Usage: /browser [status|connect [loopback-url]|disconnect]. Connect never launches a browser."
        return f"Usage: /{name} [status|start|stop|theme [name]|auth]. Services bind to loopback only."

    def _render(self, state: dict[str, Any], note: str = "") -> tuple[str, Any]:
        return {
            "browser": self._browser_view,
            "browser-backends": self._browser_selector_view,
            "browser-engines": self._browser_selector_view,
            "browser-providers": self._browser_selector_view,
            "browser-confirm": self._browser_confirmation_view,
            "dashboard": self._dashboard_view,
            "dashboard-confirm": self._dashboard_confirmation_view,
            "themes": self._themes_view,
            "home": self._home_view,
        }.get(state.get("view"), self._home_view)(state, note)

    async def handle_command(self, source: Any, command: str, arguments: str,
                             reply_to: Any = None) -> bool:
        """Open the native controls, or run an explicit ``browser connect [url]`` action."""
        if not self._authorized(source):
            return False
        with self._profile_scope(self._home(source)):
            return await self._handle_command_scoped(source, command, arguments, reply_to)

    async def _handle_command_scoped(self, source: Any, command: str, arguments: str,
                                    reply_to: Any = None) -> bool:
        if not self._authorized(source):
            return False
        name = str(command or "").strip().lstrip("/").casefold()
        args = str(arguments or "").strip()
        first, _, rest = args.partition(" ")
        verb = first.casefold()
        service = "dashboard" if name == "webui" else name
        verbs = {
            "browser": {"", "status", "help", "connect", "disconnect"},
            "dashboard": {"", "status", "help", "start", "stop", "theme", "auth"},
        }
        invalid = (service in verbs and (
            verb not in verbs[service]
            or (rest.strip() and verb not in {"connect", "theme"})
            or (verb == "theme" and rest.strip() and rest.strip().casefold() not in self._theme_names())
        ))
        if invalid or verb == "help":
            state = self._new_state(source, service)
            text, markup = self._render(state, self._usage(name))
            await self._send(source, state, text, markup, reply_to=reply_to)
            return True
        if service == "dashboard" and verb == "auth":
            await self.adapter._dispatch_control_command(source, "webauth", reply_to=reply_to)
            return True
        if name == "browser" and verb in {"connect", "disconnect"}:
            action = "browser-connect" if verb == "connect" else "browser-disconnect"
            endpoint = rest.strip() or None
            if verb == "disconnect" and endpoint:
                return False
            state = self._new_state(source, "browser")
            text, _ = self._render(state, "Working…")
            await self._send(source, state, text, inline_markup([]), reply_to=reply_to)
            try:
                outcome = await asyncio.to_thread(self._run_action, action, state, endpoint)
            except Exception:
                logger.warning("Telegram browser action failed (%s)", action)
                outcome = "The result could not be confirmed. Only reachable loopback endpoints without credentials are accepted; refresh before retrying."
            state["view"] = "browser"
            text, markup = self._render(state, outcome)
            await self._edit(None, source, state, text, markup)
            return True
        direct_actions = {
            "start": "dashboard-start", "stop": "dashboard-stop", "theme": "themes",
        }
        if name in {"dashboard", "webui"} and verb in {"start", "stop"}:
            if rest.strip():
                return False
            action = direct_actions[verb]
            state = self._new_state(source, "dashboard")
            text, _ = self._render(state, "Working…")
            await self._send(source, state, text, inline_markup([]), reply_to=reply_to)
            try:
                outcome = await asyncio.to_thread(self._run_action, action, state)
            except Exception:
                logger.warning("Telegram dashboard action failed (%s)", action)
                outcome = "The result could not be confirmed; refresh the service status before retrying."
            text, markup = self._render(state, outcome)
            await self._edit(None, source, state, text, markup)
            return True
        if name in {"dashboard", "webui"} and verb == "theme" and rest.strip():
            theme = rest.strip().casefold()
            if theme not in self._theme_names():
                return False
            state = self._new_state(source, "dashboard")
            text, _ = self._render(state, "Working…")
            await self._send(source, state, text, inline_markup([]), reply_to=reply_to)
            try:
                outcome = await asyncio.to_thread(self._run_action, f"theme:{theme}", state)
            except Exception as exc:
                logger.warning("Telegram dashboard theme update failed (%s)", type(exc).__name__)
                outcome = "Theme update failed; no configuration details are shown."
            text, markup = self._render(state, outcome)
            await self._edit(None, source, state, text, markup)
            return True
        view = {"browser": "browser", "dashboard": "dashboard", "webui": "dashboard"}.get(name, "home")
        if name in {"dashboard", "webui"} and verb == "theme":
            view = "themes"
        state = self._new_state(source, view)
        text, markup = self._render(state)
        await self._send(source, state, text, markup, reply_to=reply_to)
        if state.get("message_id") is None:
            logger.warning("Telegram standalone control card could not bind a message ID")
        return True

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        """Dispatch an owner-only, exact-message-bound, one-shot callback."""
        payload = str(data or "")
        if not payload.startswith(_PREFIX):
            return False
        if not self._authorized(source):
            await self._answer(event, "This control is unavailable.")
            return True
        with self._profile_scope(self._home(source)):
            return await self._handle_callback_scoped(event, source, payload)

    async def _handle_callback_scoped(self, event: Any, source: Any, payload: str) -> bool:
        parts = payload[len(_PREFIX):].split(":", 1)
        try:
            sid = int(parts[0])
            action = parts[1]
        except (ValueError, IndexError):
            return True
        state = self._states.get(sid)
        if state is None or not self._matches(state, source, event) or not self._allowed_action(state, action):
            await self._answer(event, "This control expired or belongs to another chat.")
            return True
        if state["thread_id"] and getattr(source, "thread_id", None) is None:
            from gateway.session_identity import replace_source
            try:
                source = replace_source(source, thread_id=state["thread_id"])
            except TypeError:
                from types import SimpleNamespace
                source = SimpleNamespace(**{**vars(source), "thread_id": state["thread_id"]})
        # Consume before awaiting anything: replay and concurrent double taps fail closed.
        self._states.pop(sid, None)
        await self._answer(event)
        state["id"] = -1  # no callback remains usable while the operation is in flight
        if action == "setup":
            await self.adapter._dispatch_control_command(
                source, "setup", reply_to=state.get("message_id"), event=event,
            )
            return True
        if action == "auth":
            fresh = self._new_state(source, state["view"], state.get("message_id"))
            text, markup = self._render(fresh)
            await self._edit(event, source, fresh, text, markup)
            await self.adapter._dispatch_control_command(source, "webauth", reply_to=state["message_id"], event=event)
            return True
        work = action in {"browser-connect", "browser-disconnect", "browser-apply",
                          "dashboard-confirm-start", "dashboard-confirm-stop"} or action.startswith("theme:")
        if work:
            text, _ = self._render(state, "Working…")
            await self._edit(event, source, state, text, inline_markup([]))
            operation = action
            if action in {"dashboard-confirm-start", "dashboard-confirm-stop"}:
                operation = str((state.get("pending_dashboard_action") or {}).get("operation") or "")
            try:
                note = await asyncio.to_thread(self._run_action, operation, state)
            except Exception as exc:
                logger.warning("Telegram standalone control action failed (%s)", type(exc).__name__)
                note = "The result could not be confirmed; refresh this service before retrying."
            if action == "browser-apply":
                pending = state.pop("pending_browser_setting", {})
                state["view"] = str(pending.get("return_view") or "browser")
            elif action in {"dashboard-confirm-start", "dashboard-confirm-stop"}:
                state.pop("pending_dashboard_action", None)
                state["view"] = "dashboard"
            if action in {"browser-apply", "dashboard-confirm-start", "dashboard-confirm-stop"}:
                complete = getattr(self.adapter, "_complete_control_frame", None)
                if callable(complete):
                    complete({"kind": "standalone", "view": state["view"]})
        elif action in {"browser-backends", "browser-engines", "browser-providers"}:
            self._push_control_frame(source, state)
            state["view"] = action
            note = ""
        elif action in {"dashboard-start", "dashboard-stop"}:
            self._push_control_frame(source, state)
            state["pending_dashboard_action"] = {"operation": action}
            state["view"] = "dashboard-confirm"
            note = ""
        elif action.startswith("browser-select:"):
            pending = self._parse_browser_selection(state, action)
            if pending is None:
                note = "That browser option is no longer available."
            else:
                self._push_control_frame(source, state)
                state["pending_browser_setting"] = pending
                state["view"] = "browser-confirm"
                note = ""
        elif action == "browser-cancel":
            pending = state.get("pending_browser_setting") or {}
            state["view"] = str(pending.get("return_view") or "browser")
            note = "No browser setting was changed."
        elif action == "dashboard-cancel":
            state.pop("pending_dashboard_action", None)
            state["view"] = "dashboard"
            note = "No dashboard action was taken."
        elif action in {"browser", "dashboard", "themes"}:
            if action == "themes" and state["view"] != "themes":
                self._push_control_frame(source, state)
            state["view"] = {"browser": "browser", "dashboard": "dashboard",
                             "themes": "themes", "home": "home"}[action]
            note = ""
        else:
            note = "Unknown or expired action."
        # Rotate the callback ID and keep it bound to the very same card/message.
        fresh = self._new_state(source, state["view"], state.get("message_id"))
        # Nested confirmation data belongs to this one-shot card and must survive
        # callback rotation; identity, expiry and message binding come from fresh.
        fresh.update({key: value for key, value in state.items()
                      if key not in {"id", "created", "home", "profile", "user_id", "chat_id",
                                     "thread_id", "message_id", "view"}})
        text, markup = self._render(fresh, note)
        await self._edit(event, source, fresh, text, markup)
        return True

    @staticmethod
    def _allowed_action(state: dict[str, Any], action: str) -> bool:
        if state.get("view") == "browser-confirm":
            return action in {"browser-apply", "browser-cancel"} and bool(state.get("pending_browser_setting"))
        if state.get("view") == "dashboard-confirm":
            operation = str((state.get("pending_dashboard_action") or {}).get("operation") or "")
            expected = {"dashboard-start": "dashboard-confirm-start",
                        "dashboard-stop": "dashboard-confirm-stop"}.get(operation)
            return action in {expected, "dashboard-cancel"} and bool(expected)
        if action.startswith("browser-select:"):
            return TelegramStandaloneUI._parse_browser_selection(state, action) is not None
        if state.get("view") == "themes":
            return action == "dashboard" or action in {
                f"theme:{theme}" for theme in TelegramStandaloneUI._theme_names()
            }
        allowed = {
            "home": {"setup"},
            "browser": {"browser", "browser-connect", "browser-disconnect", "browser-backends",
                        "browser-engines", "browser-providers", "setup"},
            "browser-backends": {"browser"},
            "browser-engines": {"browser"},
            "browser-providers": {"browser"},
            "dashboard": {"themes", "dashboard", "dashboard-start", "dashboard-stop", "setup", "auth"},
            "dashboard-confirm": set(),
        }
        if state.get("view") in {"browser-backends", "browser-engines", "browser-providers"}:
            return action in allowed[state["view"]] or TelegramStandaloneUI._parse_browser_selection(state, action) is not None
        return action in allowed.get(state.get("view"), set())

    @staticmethod
    def _parse_browser_selection(state: dict[str, Any], action: str) -> dict[str, str] | None:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[0] != "browser-select":
            return None
        _, key, value = parts
        supported = {
            "browser-backends": {"backend": {"auto", "browser-use", "off"}},
            "browser-engines": {"engine": {"auto", "chrome", "lightpanda"}},
            "browser-providers": {"cloud_provider": {name for name, _label in TelegramStandaloneUI._browser_provider_options()}},
        }.get(str(state.get("view")), {})
        if value not in supported.get(key, set()):
            return None
        return {"key": key, "value": value, "return_view": str(state.get("view") or "browser")}

    async def _answer(self, event: Any, text: str = "") -> None:
        answer = getattr(event, "answer", None)
        if callable(answer):
            try:
                result = answer(text) if text else answer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.debug("Unable to acknowledge standalone control callback", exc_info=True)

    @staticmethod
    def _load_config() -> dict[str, Any]:
        from hermes_cli.config_effective import load_user_config_effective
        return load_user_config_effective(fail_closed=True)

    @staticmethod
    def _save_setting(section: str, key: str, value: str) -> None:
        from hermes_cli.config import save_config
        save_config({section: {key: value}}, merge_existing=True, preserve_keys={(section, key)})
        effective = TelegramStandaloneUI._load_config()
        if (effective.get(section) or {}).get(key, "") != value:
            raise RuntimeError("The setting could not be verified in the active profile")

    @staticmethod
    def _save_dashboard_theme(theme: str) -> None:
        if theme not in TelegramStandaloneUI._theme_names():
            raise ValueError("Unsupported dashboard theme")
        TelegramStandaloneUI._save_setting("dashboard", "theme", theme)

    @staticmethod
    def _unset_setting(section: str, key: str) -> None:
        from hermes_cli.config import read_raw_config, unset_config_value
        raw = read_raw_config()
        block = raw.get(section) if isinstance(raw, dict) else None
        if not isinstance(block, dict) or key not in block:
            return
        unset_config_value(f"{section}.{key}")
        saved = read_raw_config()
        if key in (saved.get(section) or {}):
            raise RuntimeError("The profile default could not be verified")

    @classmethod
    def _save_browser_selection(cls, pending: dict[str, Any]) -> str:
        key = str(pending.get("key") or "")
        value = str(pending.get("value") or "")
        allowed = {
            "backend": {"auto", "browser-use", "off"},
            "engine": {"auto", "chrome", "lightpanda"},
            "cloud_provider": {name for name, _label in cls._browser_provider_options()},
        }
        if key not in allowed or value not in allowed[key]:
            raise ValueError("Unsupported browser selection")
        if value == "auto":
            cls._unset_setting("browser", key)
            effect = "restored the runtime default"
        else:
            cls._save_setting("browser", key, value)
            effect = f"saved {value}"
        return (f"Browser {key.replace('_', ' ')} {effect} for this profile. Existing sessions are unchanged; "
                "runtime-cached choices take effect after the gateway is restarted.")

    def _dashboard_status(self, home: str | Path | None = None) -> list[tuple[int, str, int, str]] | None:
        """Return live process records for one profile, never raw command lines."""
        try:
            from hermes_cli.main_dashboard import _parse_dashboard_runtime
            from hermes_cli.dashboard_procs import _scan_dashboard_processes
            from gateway.status import _pid_exists
            from hermes_cli.dashboard_procs import _pids_owned_by_hermes_home
            raw = _scan_dashboard_processes()
            owned_pids = _pids_owned_by_hermes_home(
                [pid for pid, _ in raw], str(Path(home).resolve() if home else self._home_for_status()))
            owned = set(owned_pids)
            live = []
            for pid, cmd in raw:
                if pid not in owned:
                    continue
                runtime = _parse_dashboard_runtime(cmd)
                if runtime is None:
                    continue
                mode, host, port = runtime
                if port > 0 and _pid_exists(pid):
                    live.append((pid, mode, port, host))
            return live
        except Exception:
            logger.debug("Could not read dashboard process status", exc_info=True)
            return None

    def _home_for_status(self) -> Path:
        # Config helpers use the currently bound profile home. Callback/command handlers
        # execute inside the gateway's source profile scope when available.
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser().resolve()

    @staticmethod
    def _local_cdp_endpoint(value: str | None) -> str:
        if not value:
            return "http://127.0.0.1:9222"
        raw = value if "://" in value else "http://" + value
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError("Use a valid local CDP URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("CDP URLs containing credentials or query values are not accepted here")
        if parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Telegram controls only connect to loopback CDP endpoints")
        try:
            port = parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80)
        except ValueError as exc:
            raise ValueError("Invalid CDP port") from exc
        if not 1 <= port <= 65535:
            raise ValueError("Invalid CDP port")
        path = parsed.path if parsed.path.startswith("/devtools/browser/") else ""
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @classmethod
    def _browser_connect(cls, endpoint: str | None) -> str:
        """Persist a loopback endpoint using the existing profile config contract; never launches one."""
        if os.environ.get("BROWSER_CDP_URL", "").strip():
            return "A process-wide browser override is active; Telegram left it unchanged."
        from hermes_cli.browser_connect import is_browser_debug_ready
        config = cls._load_config()
        if str((config.get("browser") or {}).get("backend") or "").casefold() == "browser-use":
            return ("Browser Use mode is selected; it was not changed. CDP attachment was not attempted. "
                    "Choose Built-in browser tools under Backend first to use this attachment flow; "
                    "Telegram will not switch your backend automatically.")
        configured_endpoint = str((config.get("browser") or {}).get("cdp_url") or "").strip()
        url = cls._local_cdp_endpoint(endpoint or configured_endpoint or None)
        sanitized = cls._sanitize_cdp_display(url)
        try:
            ready = is_browser_debug_ready(url, timeout=1.0)
        except Exception:
            logger.debug("CDP reachability check failed for the configured endpoint", exc_info=True)
            return (f"Could not verify CDP endpoint {sanitized}. Check that Chromium is running with "
                    "remote debugging enabled on the Hermes gateway host, then retry.")
        if not ready:
            return (f"No Chromium CDP endpoint responded at {sanitized}. Start Chromium on the Hermes "
                    "gateway host with remote debugging enabled, then retry. Telegram-device localhost "
                    "is not the gateway host.")
        cls._save_setting("browser", "cdp_url", url)
        return "Saved the profile CDP endpoint; new browser work will use it. Its URL remains hidden."

    @staticmethod
    def _browser_disconnect() -> str:
        if os.environ.get("BROWSER_CDP_URL", "").strip():
            return "A process-wide browser override is active; Telegram cannot change it."
        config = TelegramStandaloneUI._load_config()
        browser_cfg = config.get("browser") if isinstance(config.get("browser"), dict) else {}
        if not str(browser_cfg.get("cdp_url") or "").strip():
            return "No profile CDP endpoint is configured."
        TelegramStandaloneUI._save_setting("browser", "cdp_url", "")
        return "Cleared the profile CDP setting; existing browser/provider configuration is unchanged."

    def _dashboard_start(self, state: dict[str, Any]) -> str:
        from hermes_cli.main_dashboard import _dashboard_listening
        from tools.environments.local import served_profile_child_env
        with self._service_lock:
            current = self._dashboard_status(state["home"])
            if current is None:
                return "Dashboard status is unavailable; no service was started."
            if current:
                return "A dashboard/serve process is already running or starting for this profile."
            if _dashboard_listening("127.0.0.1", 9119):
                return "Local port 9119 is in use by another service. It was left unchanged."
            home = Path(state["home"]).resolve()
            env = served_profile_child_env(target_home=home, inherit_credentials=True)
            from agent.secret_scope import current_secret_scope, serves_routed_profile
            if serves_routed_profile():
                scope = current_secret_scope()
                if scope is None:
                    raise RuntimeError("Dashboard profile credentials could not be resolved")
                # Shell-injected auth vars are outside the general provider blocklist.
                # Never inherit a sibling's dashboard credentials or authentication gates.
                env = {key: value for key, value in env.items()
                       if not key.startswith("HERMES_DASHBOARD_") or key in scope}
            profile = home.name if home.parent.name == "profiles" else "default"
            # Explicit profile + isolated prevents sticky-profile and machine-root rerouting.
            argv = [sys.executable, "-m", "hermes_cli.main", "-p", profile,
                    "dashboard", "--isolated", "--host", "127.0.0.1", "--port", "9119", "--no-open"]
            log_dir = home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            with open(log_dir / "dashboard-telegram-start.log", "ab") as log:
                process = subprocess.Popen(
                    argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    env=env, start_new_session=True, close_fds=True,  # detached-spawn: ok — dashboard server meant to outlive the Telegram command; tracked by pid via _dashboard_status
                )
            for _ in range(30):
                if process.poll() is not None:
                    return "The WebUI exited before readiness. Check this profile's dashboard log; no restart was attempted."
                owned = self._dashboard_status(str(home))
                if owned and any(pid == process.pid for pid, _mode, _port, _host in owned) and _dashboard_listening("127.0.0.1", 9119):
                    return "Started this profile's isolated WebUI on loopback using the normal authentication checks."
                time.sleep(0.2)
            return "Startup was requested on loopback but readiness is not confirmed. Refresh before retrying."

    def _dashboard_stop(self, state: dict[str, Any]) -> str:
        from hermes_cli.dashboard_procs import _kill_stale_dashboard_processes
        with self._service_lock:
            result = _kill_stale_dashboard_processes(
                reason="requested from private Telegram controls", scope_home=state["home"])
        killed = len(result.get("killed") or [])
        failed = len(result.get("failed") or [])
        if failed:
            return f"Stopped {killed} process(es); {failed} could not be stopped."
        return f"Stopped {killed} dashboard/serve process(es) for this profile."

    def _run_action(self, action: str, state: dict[str, Any], endpoint: str | None = None) -> str:
        if action == "browser-apply":
            pending = state.get("pending_browser_setting")
            if not isinstance(pending, dict):
                return "No browser setting is awaiting confirmation."
            return self._save_browser_selection(pending)
        actions = {
            "browser-connect": lambda: self._browser_connect(endpoint),
            "browser-disconnect": self._browser_disconnect,
            "dashboard-start": lambda: self._dashboard_start(state),
            "dashboard-stop": lambda: self._dashboard_stop(state),
        }
        action_fn = actions.get(action)
        if action_fn is not None:
            return action_fn()
        theme = action.partition(":")[2] if action.startswith("theme:") else None
        if theme is not None:
            self._save_dashboard_theme(theme)
            return f"Dashboard theme saved as {theme}; refresh the WebUI to apply it."
        return "Unknown action."
