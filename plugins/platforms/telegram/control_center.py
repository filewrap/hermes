"""Telegram-native control routing; every action uses an existing Hermes surface."""
from __future__ import annotations

import logging
import contextvars
import secrets
import time
from typing import Any

from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger(__name__)
_CONTROL_TARGET = contextvars.ContextVar("telegram_control_target", default=None)
_CONTROL_ROUTE_TTL = 15 * 60
_CONTROL_ROUTE_LIMIT = 256


class TelegramControlCenterMixin:
    """Keep setup orchestration outside the transport and agent turn loop."""

    def _setup_ui(self):
        ui = getattr(self, "_telegram_setup_ui", None)
        if ui is None:
            from .setup_ui import TelegramSetupUI
            ui = self._telegram_setup_ui = TelegramSetupUI(self)
        return ui

    def _standalone_ui(self):
        ui = getattr(self, "_telegram_standalone_ui", None)
        if ui is None:
            from .standalone_ui import TelegramStandaloneUI
            ui = self._telegram_standalone_ui = TelegramStandaloneUI(self)
        return ui

    def _thread_ui(self):
        ui = getattr(self, "_telegram_thread_ui", None)
        if ui is None:
            from .thread_ui import TelegramThreadUI
            ui = self._telegram_thread_ui = TelegramThreadUI(self)
        return ui

    def _web_auth_ui(self):
        ui = getattr(self, "_telegram_web_auth_ui", None)
        if ui is None:
            from .web_auth_ui import TelegramWebAuthUI
            ui = self._telegram_web_auth_ui = TelegramWebAuthUI(self)
        return ui

    def _setup_tools_ui(self):
        ui = getattr(self, "_telegram_setup_tools_ui", None)
        if ui is None:
            from .setup_tools_ui import TelegramSetupToolsUI
            ui = self._telegram_setup_tools_ui = TelegramSetupToolsUI(self)
        return ui

    def _control_routes(self) -> dict[tuple[Any, ...], dict[str, Any]]:
        """Keep transient card capabilities local to this adapter instance."""
        routes = self.__dict__.setdefault("_control_route_store", {})
        now = time.monotonic()
        for key, route in list(routes.items()):
            if now - route.get("created", 0) > _CONTROL_ROUTE_TTL:
                routes.pop(key, None)
        while len(routes) > _CONTROL_ROUTE_LIMIT:
            oldest = min(routes, key=lambda key: routes[key].get("created", 0))
            routes.pop(oldest, None)
        return routes

    async def _handle_control_command(
        self, source: Any, command: str, arguments: str = "", *, reply_to=None,
    ) -> bool:
        handlers = {
            "setup": lambda: self._setup_ui().handle_command(source, arguments, reply_to=reply_to),
            "browser": lambda: self._standalone_ui().handle_command(source, command, arguments, reply_to=reply_to),
            "dashboard": lambda: self._standalone_ui().handle_command(source, command, arguments, reply_to=reply_to),
            "webui": lambda: self._standalone_ui().handle_command(source, command, arguments, reply_to=reply_to),
            "webauth": lambda: self._web_auth_ui().handle_command(source, arguments, reply_to=reply_to),
            "thread": lambda: self._thread_ui().handle_command(source, arguments, reply_to=reply_to),
            "setuptools": lambda: self._setup_tools_ui().handle_command(source, arguments, reply_to=reply_to),
        }
        handler = handlers.get(command)
        if handler is None:
            return False
        if not self._authorized(source) or not self._is_owner_user(source.user_id, source=source):
            await self._send_rich_ui(
                source, "<p>Only the configured owner can use these controls.</p>", reply_to=reply_to,
            )
            return True
        if command != "thread" and source.chat_type not in {"dm", "private", "direct"}:
            await self._send_rich_ui(
                source, f"<p>Open a private chat and send <code>/{command}</code> to use this control.</p>",
                reply_to=reply_to,
            )
            return True
        try:
            await handler()
        except Exception as exc:
            # Exception messages may include endpoints or credentials; only log the type.
            logger.warning("Telegram /%s control failed (%s)", command, type(exc).__name__)
            await self._send_rich_ui(
                source, "<h1>Control unavailable</h1><p>The result could not be confirmed. "
                "Reopen the control to check its current state before trying again.</p>", reply_to=reply_to,
            )
        return True

    async def _dispatch_control_command(self, source: Any, command: str, *, reply_to=None, event=None) -> None:
        """Open real controls, retaining normal slash authorization and busy policies."""
        if not self._authorized(source) or not self._is_owner_user(source.user_id, source=source):
            await self._send_rich_ui(source, "<p>Only the configured owner can use the setup controls.</p>")
            return
        message_id = getattr(event, "message_id", None) if event is not None else None
        if message_id is None:
            message_id = getattr(getattr(getattr(event, "query", None), "message", None), "id", None)
        target = None
        if message_id is not None:
            route = self._setup_route_for_card(source, message_id)
            if route and command != "setup":
                previous = route.get("current_command")
                if previous != command:
                    if previous:
                        route["navigation"].push({"kind": "command", "command": previous})
                    else:
                        state = self._setup_ui()._states.get(route.get("setup_id"))
                        if state:
                            from .control_navigation import setup_frame
                            route["navigation"].push(setup_frame(state))
                route["current_command"] = command
            target = {"source": source, "message_id": str(message_id),
                      "return_route": (None if command == "setup" else route)}
        token = _CONTROL_TARGET.set(target) if target else None
        try:
            if await self._handle_control_command(source, command, reply_to=reply_to):
                return
            direct = {
                "settings": lambda: self._settings_ui().open(source, reply_to=reply_to),
                "mcp": lambda: self._settings_ui().open_mcp(source, reply_to=reply_to),
                "skills": lambda: self._skills_ui().open(source, reply_to=reply_to),
                "logs": lambda: self._diagnostics_ui().open(source, "logs", reply_to=reply_to),
                "help": lambda: self._show_help(source, reply_to=reply_to),
            }
            if command in direct:
                await direct[command]()
                return
            if command not in {"model", "models", "providers", "msettings", "sessions", "status"}:
                await self._send_rich_ui(source, "<p>This setup action is unavailable.</p>", reply_to=reply_to)
                return
            await self.handle_message(MessageEvent(
                text=f"/{command}", message_type=MessageType.COMMAND,
                source=source, user_id=source.user_id, user_name=getattr(source, "user_name", None),
                message_id=str(reply_to) if reply_to is not None else None,
                metadata={"thread_id": source.thread_id} if getattr(source, "thread_id", None) else {},
            ))
        finally:
            if token is not None:
                _CONTROL_TARGET.reset(token)

    async def _handle_control_callback(self, event: Any, source: Any, data: str) -> bool:
        if data.startswith("hm:setup-back:"):
            return await self._handle_setup_back_callback(event, source, data)
        prefixes = {
            "hm:setup:": self._setup_ui,
            "hm:standalone:": self._standalone_ui,
            "hm:thread:": self._thread_ui,
            "hm:webauth:": self._web_auth_ui,
            "hm:setuptools:": self._setup_tools_ui,
            "hm:logs:": self._diagnostics_ui,
        }
        getter = next((getter for prefix, getter in prefixes.items() if data.startswith(prefix)), None)
        if getter is None:
            return False
        if not self._authorized(source) or not self._is_owner_user(source.user_id, source=source):
            await event.answer("Only the configured owner can use this control.", alert=True)
            return True
        if not data.startswith("hm:thread:") and source.chat_type not in {"dm", "private", "direct"}:
            await event.answer("Open this control in a private chat.", alert=True)
            return True
        target = None if data.startswith("hm:setup:") else self._target_for_callback(event, source)
        token = _CONTROL_TARGET.set(target) if target else None
        try:
            callback_data = ("dg:" + data.rsplit(":", 1)[-1]
                             if data.startswith("hm:logs:") else data)
            await getter().handle_callback(event, source, callback_data)
        except Exception as exc:
            logger.warning("Telegram control callback failed (%s)", type(exc).__name__)
            text = ("<h1>Control unavailable</h1><p>The result could not be confirmed. "
                    "Reopen the menu to check its state before retrying.</p>")
            try:
                await self._edit_rich_ui_message(event, source, text)
            except Exception:
                logger.debug("Could not edit failed Telegram control card", exc_info=True)
        finally:
            if token is not None:
                _CONTROL_TARGET.reset(token)
        return True

    def _control_message_id(self, event: Any) -> Any:
        return (getattr(event, "message_id", None)
                or getattr(getattr(getattr(event, "query", None), "message", None), "id", None))

    def _route_identity(self, source: Any, message_id: Any) -> tuple[Any, ...]:
        resolver = getattr(self, "_settings_home", None)
        try:
            if callable(resolver):
                resolved = resolver(source)
                if resolved is None:
                    raise RuntimeError("The active Telegram profile could not be resolved")
                home = str(resolved)
            else:
                runner = getattr(self, "gateway_runner", None)
                profile_resolver = getattr(runner, "_resolve_profile_home_for_source", None)
                if callable(profile_resolver):
                    resolved = profile_resolver(source)
                    if resolved is None:
                        raise RuntimeError("The active Telegram profile could not be resolved")
                    home = str(resolved)
                else:
                    from hermes_constants import get_hermes_home
                    home = str(get_hermes_home())
        except Exception as exc:
            raise RuntimeError("The active Telegram profile could not be resolved") from exc
        return (id(self), home, str(getattr(source, "profile", "") or ""),
                str(getattr(source, "user_id", "") or ""),
                str(getattr(source, "chat_id", "") or ""),
                str(getattr(source, "thread_id", "") or ""), str(message_id))

    def _setup_route_for_card(self, source: Any, message_id: Any) -> dict[str, Any] | None:
        """Bind delegated setup navigation to the live setup state for this exact card."""
        from .control_navigation import ControlNavigation, setup_frame
        routes = self._control_routes()
        try:
            key = self._route_identity(source, message_id)
        except RuntimeError:
            return None
        existing = routes.get(key)
        if existing:
            states = getattr(getattr(self, "_telegram_setup_ui", None), "_states", {})
            for state in states.values():
                if str(state.get("message_id") or "") == str(message_id):
                    existing["setup_id"] = state["id"]
            return existing
        ui = getattr(self, "_telegram_setup_ui", None)
        states = getattr(ui, "_states", None)
        if not isinstance(states, dict):
            return None
        try:
            _adapter_id, home, profile, user, chat, thread, msg = self._route_identity(source, message_id)
        except RuntimeError:
            return None
        for state in states.values():
            identity = tuple(state.get("identity", ()))
            if (str(state.get("message_id") or "") == msg
                    and str(state.get("profile", "") or "") == profile
                    and str(state.get("user_id", "") or "") == user
                    and str(state.get("chat_id", "") or "") == chat
                    and (not thread or str(state.get("thread_id", "") or "") == thread)
                    and (not home or not identity or str(identity[0]) == home)):
                route = {"setup_id": int(state["id"]), "created": time.monotonic(),
                         "source": source, "home": home, "profile": profile,
                         "user_id": user, "chat_id": chat,
                         "thread_id": str(state.get("thread_id", "") or thread),
                         "message_id": msg, "current_command": None,
                         "navigation": state.setdefault("navigation", ControlNavigation())}
                route["navigation"].push(setup_frame(state))
                routes[self._route_identity(source, message_id)] = route
                self._control_routes()
                return route
        return None

    def _push_control_frame(self, source: Any, message_id: Any, frame: dict[str, Any]) -> bool:
        route = self._setup_route_for_card(source, message_id)
        if route:
            if not route.get("restoring"):
                route["navigation"].push(frame)
            route["managed_back"] = True
            return True
        return False

    def _complete_control_frame(self, frame: dict[str, Any]) -> None:
        target = _CONTROL_TARGET.get()
        route = target.get("return_route") if target else None
        if route and not route.get("restoring"):
            history = route["navigation"]
            if history.frames and history.frames[-1] == frame:
                history.pop()

    def _target_for_callback(self, event: Any, source: Any) -> dict[str, Any] | None:
        message_id = self._control_message_id(event)
        if message_id is None:
            return None
        try:
            key = self._route_identity(source, message_id)
        except RuntimeError:
            return None
        routes = self._control_routes()
        route = routes.get(key)
        if route is None:
            # Callback sources may omit a topic id; the message id and all other identity
            # dimensions remain exact, and the recorded thread is restored for this card.
            # A non-empty but different thread is a mismatch, never a fallback.
            if not key[5]:
                key = next((candidate for candidate in routes
                            if candidate[:5] == key[:5] and candidate[6] == key[6]), key)
            route = routes.get(key)
        if not route or time.monotonic() - route.get("created", 0) > _CONTROL_ROUTE_TTL:
            routes.pop(key, None)
            return None
        try:
            current = self._route_identity(source, message_id)
        except RuntimeError:
            return None
        if (route["profile"] != current[2] or route["user_id"] != current[3]
                or route["chat_id"] != current[4]
                or (current[5] and route["thread_id"] != current[5])):
            return None
        return {"source": source, "message_id": str(message_id), "return_route": route}

    async def _invoke_control_ui_callback(self, event: Any, source: Any, handler: Any, *args) -> Any:
        """Run a subordinate UI callback under its setup-card edit/return scope, if any."""
        target = self._target_for_callback(event, source)
        if target is None:
            message_id = self._control_message_id(event)
            if any(str(route.get("message_id")) == str(message_id)
                   and str(route.get("chat_id")) == str(getattr(source, "chat_id", ""))
                   for route in self._control_routes().values()):
                await event.answer("This control belongs to another profile or conversation.", alert=True)
                return True
        token = _CONTROL_TARGET.set(target) if target else None
        try:
            return await handler(event, source, *args)
        finally:
            if token is not None:
                _CONTROL_TARGET.reset(token)

    def _pending_control_target(self, source: Any, ui: Any) -> dict[str, Any] | None:
        """Return a route only when this exact UI has an active typed-input flow."""
        if not self._control_routes():
            return None
        try:
            home = self._route_identity(source, "")[1]
        except RuntimeError:
            return None
        pending_map = getattr(ui, "_pending", None)
        pending = None
        if isinstance(getattr(ui, "_pending_inputs", None), dict):
            key_fn = getattr(ui, "_user_key", None)
            if callable(key_fn):
                try:
                    pending = ui._pending_inputs.get(key_fn(source))
                except (AttributeError, TypeError, ValueError):
                    return None
        elif isinstance(pending_map, dict):
            key_fn = getattr(ui, "_pending_key", None)
            if callable(key_fn):
                try:
                    from pathlib import Path
                    pending = pending_map.get(key_fn(Path(home), source))
                except Exception:
                    pending = None
            if pending is None and getattr(ui, "_states", None):
                prefix = (home, str(getattr(source, "profile", "") or ""),
                          str(getattr(source, "user_id", "") or ""),
                          str(getattr(source, "chat_id", "") or ""),
                          str(getattr(source, "thread_id", "") or ""))
                pending = next((value for key, value in pending_map.items()
                                if isinstance(key, tuple) and key[:5] == prefix), None)
        if not isinstance(pending, dict):
            return None
        states = getattr(ui, "_states", {})
        state = states.get(pending.get("state_id")) if isinstance(states, dict) else None
        message_id = state.get("message_id") if isinstance(state, dict) else None
        if message_id in (None, ""):
            return None
        routes = self._control_routes()
        try:
            key = self._route_identity(source, message_id)
        except RuntimeError:
            return None
        route = routes.get(key)
        if not route or time.monotonic() - route.get("created", 0) > _CONTROL_ROUTE_TTL:
            routes.pop(key, None)
            return None
        if (route.get("home") != home
                or route.get("profile") != str(getattr(source, "profile", "") or "")
                or route.get("user_id") != str(getattr(source, "user_id", "") or "")
                or route.get("chat_id") != str(getattr(source, "chat_id", "") or "")
                or route.get("thread_id", "") != str(getattr(source, "thread_id", "") or "")):
            return None
        return {"source": source, "message_id": str(message_id), "return_route": route}

    async def _invoke_control_ui_message(self, event: Any, source: Any, ui: Any,
                                         handler: Any, text: str) -> Any:
        """Scope only a controller-confirmed pending input; never redirect ordinary text."""
        target = self._pending_control_target(source, ui)
        token = _CONTROL_TARGET.set(target) if target else None
        try:
            return await handler(event, source, text)
        finally:
            if token is not None:
                _CONTROL_TARGET.reset(token)

    async def _handle_setup_back_callback(self, event: Any, source: Any, data: str) -> bool:
        if (not self._authorized(source) or not self._is_owner_user(source.user_id, source=source)
                or source.chat_type not in {"dm", "private", "direct"}):
            await event.answer("This setup route is not authorized.", alert=True)
            return True
        message_id = self._control_message_id(event)
        route = self._target_for_callback(event, source) if message_id is not None else None
        token = data.removeprefix("hm:setup-back:")
        expected = route.get("return_route") if route else None
        if not expected or not token or token != expected.get("token"):
            await event.answer("This setup route is expired or not authorized for this card.", alert=True)
            return True
        expected["token"] = None  # consume before any await; a double tap cannot pop twice
        frame = expected["navigation"].pop()
        if not frame:
            await event.answer("This setup route has expired.", alert=True)
            return True
        if expected.get("thread_id") and not getattr(source, "thread_id", None):
            from gateway.session_identity import replace_source
            source = replace_source(source, thread_id=expected["thread_id"])
        self._cancel_card_editors(source, message_id)
        await event.answer()
        if frame["kind"] == "setup":
            ui = self._setup_ui()
            state = ui._new_state(source, ui._home(source), frame["section"])
            state.update(message_id=str(message_id), page=frame["page"], navigation=expected["navigation"],
                         inventory_node=frame.get("inventory_node", 0))
            expected.update(setup_id=state["id"], current_command=None)
            await ui._refresh(state)
        elif frame["kind"] == "standalone":
            ui = self._standalone_ui()
            with ui._profile_scope(ui._home(source)):
                state = ui._new_state(source, frame["view"], message_id)
                expected["current_command"] = "browser" if frame["view"].startswith("browser") else "dashboard"
                scope = _CONTROL_TARGET.set(route)
                try:
                    text, markup = ui._render(state)
                    await ui._edit(event, source, state, text, markup)
                finally:
                    _CONTROL_TARGET.reset(scope)
        elif frame["kind"] in {"personal", "skills"}:
            ui = self._settings_ui() if frame["kind"] == "personal" else self._skills_ui()
            expected["current_command"] = "settings" if frame["kind"] == "personal" else "skills"
            expected["restoring"] = True
            scope = _CONTROL_TARGET.set(route)
            try:
                await ui.restore_control_frame(source, message_id, frame)
            finally:
                expected["restoring"] = False
                _CONTROL_TARGET.reset(scope)
        else:
            expected["current_command"] = frame["command"]
            await self._dispatch_control_command(source, frame["command"], event=event)
        return True

    def _cancel_card_editors(self, source: Any, message_id: Any) -> None:
        """Invalidate departed card capabilities; controllers may retain scoped drafts."""
        home = self._route_identity(source, message_id)[1]
        for name, ui in list(self.__dict__.items()):
            if not name.startswith("_") or not name.endswith("_ui"):
                continue
            states = getattr(ui, "_states", {})
            if not isinstance(states, dict):
                continue
            for sid, state in list(states.items()):
                if (str(state.get("message_id") or "") != str(message_id)
                        or str(state.get("home") or "") != home
                        or str(state.get("chat_id") or "") != str(source.chat_id)
                        or str(state.get("user_id") or "") != str(source.user_id)):
                    continue
                drop = getattr(ui, "_drop_state", None)
                if callable(drop):
                    drop(sid)
                else:
                    states.pop(sid, None)
                for attr in ("_pending", "_pending_inputs"):
                    pending = getattr(ui, attr, {})
                    if isinstance(pending, dict):
                        for key, value in list(pending.items()):
                            if isinstance(value, dict) and value.get("state_id") == sid:
                                pending.pop(key, None)

        pickers = getattr(self, "_model_picker_state", {})
        for picker_id, state in list(pickers.items()):
            if (str(state.get("message_id") or "") == str(message_id)
                    and str(state.get("chat_id") or "") == str(source.chat_id)
                    and str(state.get("user_id") or "") == str(source.user_id)):
                pickers.pop(picker_id, None)

    def _route_control_request(self, request: Any) -> tuple[Any, str | None]:
        """Convert delegated RichMessage sends/edits into edits of the scoped source card."""
        target = _CONTROL_TARGET.get()
        if not target:
            return request, None
        try:
            from telethon import Button, functions, types
            if isinstance(request, (functions.messages.SendMessageRequest, functions.messages.EditMessageRequest)):
                message, rich, entities = request.message, getattr(request, "rich_message", None), getattr(request, "entities", None)
            else:
                return request, None
            route = target.get("return_route")
            markup = getattr(request, "reply_markup", None)
            if route:
                route["token"] = secrets.token_hex(8)
                data = f"hm:setup-back:{route['token']}"
                rows = []
                native_back = None
                for existing_row in (getattr(markup, "rows", ()) or ()):
                    buttons = []
                    for button in existing_row.buttons:
                        if button.text.casefold() == "close":
                            continue
                        if button.text.casefold() == "cancel" and route.get("managed_back"):
                            payload = getattr(getattr(button, "type", None), "data", b"")
                            payload = payload.decode() if isinstance(payload, bytes) else str(payload)
                            if payload.startswith(("ps:", "sk:")):
                                continue
                        label = button.text.lstrip("←‹ ").casefold()
                        if label.startswith("back") or (button.text.startswith("‹") and not label.startswith("previous")):
                            payload = getattr(getattr(button, "type", None), "data", b"")
                            payload = payload.decode() if isinstance(payload, bytes) else str(payload)
                            if ("setup" not in button.text.casefold()
                                and "help" not in button.text.casefold()
                                and not (payload.startswith("ms:") and route.get("current_command") is None)
                                and not (payload.startswith("sk:") and payload.endswith(":personal"))
                                    and not payload.startswith(("hm:standalone:", "hm:setup-back:"))
                                    and not (route.get("managed_back")
                                             and payload.startswith(("ps:", "sk:")))):
                                native_back = button
                            continue
                        buttons.append(button)
                    if buttons:
                        rows.append(types.KeyboardButtonRow(buttons=buttons))
                label = "← Back to setup" if len(route["navigation"].frames) == 1 else "← Back"
                if native_back:
                    native_back.text = "← " + native_back.text.lstrip("← ")
                    back = native_back
                    route["token"] = None
                else:
                    back = Button.inline(label, data=data, style="primary")
                has_keyboard = bool(getattr(markup, "rows", None))
                if has_keyboard:
                    rows.append(types.KeyboardButtonRow(buttons=[back]))
                    markup = types.ReplyInlineMarkup(rows=rows)
                else:
                    route["token"] = None
                from .control_html import setup_card_html
                total = sum(len(row.buttons) for row in (rows[:-1] if has_keyboard else rows))
                rich, message = setup_card_html(rich, message, total)
                route["created"] = time.monotonic()
            edited = functions.messages.EditMessageRequest(
                peer=self._peer_for(target["source"].chat_id), id=int(target["message_id"]),
                message=message, entities=entities, no_webpage=getattr(request, "no_webpage", None),
                reply_markup=markup, rich_message=rich,
            )
            return edited, str(target["message_id"])
        except Exception:
            logger.warning("Could not scope Telegram control RichMessage to its originating card", exc_info=True)
            raise

    async def _edit_rich_ui_message(self, event: Any, source: Any, text: str, markup=None):
        """Edit only the exact originating card, preserving callback message bindings."""
        from .mtproto_rich import edit_request
        message_id = getattr(event, "message_id", None)
        if message_id is None:
            raise ValueError("The originating control card is unavailable")
        try:
            return await self._mtproto_rich_request(edit_request(
                self._peer_for(source.chat_id), int(message_id), text, reply_markup=markup,
            ))
        except Exception as exc:
            if type(exc).__name__ == "MessageNotModifiedError":
                return None
            raise
