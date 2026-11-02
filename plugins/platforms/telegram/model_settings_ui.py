"""Native, conversation-scoped Telegram model dashboard and request settings UI."""
from __future__ import annotations

import asyncio
import logging
import contextvars
from html import escape
import time
from pathlib import Path
from typing import Any

from gateway.model_ui import MODEL_SETTINGS, SETTINGS_PAGES, parse_setting_value
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.telegram.mtproto_rich import (
    edit_request, inline_markup, rich_button, rich_button_row, send_request,
)

logger = logging.getLogger(__name__)
_STATE_TTL = 3600
_INPUT_TTL = 600
_PARAMETER_HELP = {
    "temperature": "Controls randomness when choosing the next token. Lower values make choices more predictable; higher values make them more varied.",
    "top_p": "Limits choices to the smallest set of tokens whose combined probability reaches this value. Lower values narrow the choices; 1 keeps the full set.",
    "frequency_penalty": "Adjusts repetition based on how often a token has already appeared. Positive values discourage frequent repeats; negative values encourage them.",
    "presence_penalty": "Adjusts repetition based on whether a token has appeared at all. Positive values encourage new wording or topics; negative values favor reuse.",
    "max_tokens": "Requests a ceiling on generated output tokens, not words. The provider may enforce a smaller allowance, and reasoning tokens may share the budget.",
}
_RANGE_NOTE = "Input ranges are UI validation, not guaranteed provider/model limits. The API may reject or ignore unsupported settings."


def _model_value_pill(label: Any, callback_data: str, tone: str = "primary") -> Any:
    """Create a Layer-232 RichMessage callback pill for a read-only dashboard value."""
    from telethon import types

    style = {
        "success": types.RichButtonStyle(bg_success=True),
        "danger": types.RichButtonStyle(bg_danger=True),
        "primary": types.RichButtonStyle(bg_primary=True),
    }.get(tone, types.RichButtonStyle(link=True))
    return types.TextButton(
        text=types.TextPlain(text=str(label or "unknown")[:120]),
        type=types.InlineButtonTypeCallback(data=str(callback_data).encode("utf-8")),
        style=style,
    )


class TelegramModelSettingsUI:
    """Render model controls as native RichMessages; callbacks never create an AI turn."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = 0
        self._states: dict[int, dict[str, Any]] = {}
        self._owners: dict[tuple[str, str, str, str, str, str], int] = {}
        self._pending: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        self._expiry_tasks: set[asyncio.Task] = set()
        self._callback_target: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
            f"telegram_model_ui_target_{id(self)}", default=None,
        )
        self._seen_callbacks: dict[str, float] = {}

    def _is_owner(self, source: Any) -> bool:
        checker = getattr(self.adapter, "_is_owner_user", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(getattr(source, "user_id", None), source))
        except Exception:
            logger.debug("Could not verify Telegram model-settings owner", exc_info=True)
            return False

    @staticmethod
    def _is_dm(source: Any) -> bool:
        return getattr(source, "chat_type", None) in {"dm", "private"} and bool(
            getattr(source, "chat_id", None)
        )

    def _callback_context(self, event: Any) -> dict[str, Any] | None:
        """Return the validated in-process callback capability for a command being dispatched."""
        target = self._callback_target.get()
        metadata = getattr(event, "metadata", None) or {}
        if (not target or metadata.get("_telegram_model_ui_token") is not target.get("callback_token")
                or metadata.get("_telegram_model_ui_state_id") != target.get("id")):
            return None
        return target

    def callback_context(self, event: Any) -> dict[str, Any] | None:
        """Expose only the currently executing native UI callback to gateway handlers."""
        state = self._callback_context(event)
        if state is None:
            return None
        context = dict(state.get("context") or {})
        context["_callback_session_key"] = state.get("session_key")
        context["_callback_profile_home"] = state.get("home")
        return context

    def _reuse_target(self, source: Any, session_key: str) -> dict[str, Any] | None:
        target = self._callback_target.get()
        if (target and self._source_matches(target, source)
                and str(target.get("session_key")) == str(session_key)):
            return target
        return None

    def _home(self, source: Any, profile_home: Any = None) -> str:
        if profile_home is not None:
            return str(Path(profile_home).expanduser().resolve())
        resolver = getattr(getattr(self.adapter, "gateway_runner", None), "_resolve_profile_home_for_source", None)
        home = resolver(source) if callable(resolver) else None
        if home is None:
            from gateway.run import _hermes_home
            home = _hermes_home
        return str(Path(home).expanduser().resolve())

    @staticmethod
    def _identity(home: str, source: Any, session_key: str) -> tuple[str, str, str, str, str, str]:
        return (home, str(getattr(source, "profile", "") or ""), str(getattr(source, "user_id", "") or ""),
                str(getattr(source, "chat_id", "") or ""), str(getattr(source, "thread_id", "") or ""),
                str(session_key or ""))

    def _prune(self) -> None:
        now = time.monotonic()
        for key, pending in list(self._pending.items()):
            if now >= pending["expires_at"]:
                self._expire_pending(key, pending)
        for sid, state in list(self._states.items()):
            if now - state["created"] > _STATE_TTL:
                self._drop(sid)

    def _clear_pending(self, key: tuple) -> None:
        pending = self._pending.pop(key, None)
        if pending:
            pending["timer"].cancel()
            state = self._states.get(pending["state_id"])
            if state:
                state.pop("pending_field", None)

    def _expire_pending(self, key: tuple, pending: dict[str, Any]) -> None:
        # An old timer must never cancel a replacement parameter prompt.
        if self._pending.get(key) is pending:
            state = self._states.get(pending["state_id"])
            self._clear_pending(key)
            if state:
                state["input_expired"] = True
                task = asyncio.get_running_loop().create_task(self._refresh_expired(state))
                self._expiry_tasks.add(task)
                task.add_done_callback(self._expiry_tasks.discard)

    async def _refresh_expired(self, state: dict[str, Any]) -> None:
        if (self._states.get(state["id"]) is not state or not state.get("input_expired")
                or state.get("_callback_busy")):
            return
        state["_callback_busy"] = True
        try:
            await self._refresh(state)
        except Exception:
            logger.warning("Could not update the expired model settings prompt", exc_info=True)
        finally:
            state.pop("_callback_busy", None)

    def _drop(self, sid: int) -> None:
        state = self._states.get(sid)
        if state:
            self._clear_pending(state["identity"])
        state = self._states.pop(sid, None)
        if state:
            key = state["identity"]
            if self._owners.get(key) == sid:
                self._owners.pop(key, None)

    async def _answer(self, event: Any, text: str = "", alert: bool = False) -> None:
        try:
            await event.answer(text, alert=alert) if text or alert else await event.answer()
        except Exception:
            logger.debug("Unable to acknowledge model settings callback", exc_info=True)

    async def _send(self, source: Any, text: str, markup: Any, reply_to: Any = None) -> Any:
        if getattr(self.adapter, "_client", None) is None:
            return None
        request = send_request(self.adapter._peer_for(source.chat_id), text,
                               reply_to=int(reply_to) if reply_to else None,
                               top_msg_id=(int(source.thread_id) if source.thread_id else None),
                               reply_markup=markup, rich_format="html")
        return await self.adapter._mtproto_rich_request(request)

    async def _edit(self, state: dict[str, Any], text: str, markup: Any) -> None:
        message_id = state.get("message_id")
        if not message_id or getattr(self.adapter, "_client", None) is None:
            return
        await self.adapter._mtproto_rich_request(edit_request(
            self.adapter._peer_for(state["chat_id"]), int(message_id), text,
            reply_markup=markup, rich_format="html"))

    def _new_state(self, source: Any, session_key: str, profile_home: Any, view: str,
                   message_id: Any = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self._prune()
        self._counter += 1
        home = self._home(source, profile_home)
        identity = self._identity(home, source, session_key)
        old = self._owners.get(identity)
        if old is not None:
            self._drop(old)
        state = {"id": self._counter, "created": time.monotonic(), "identity": identity,
                 "home": home, "profile": str(getattr(source, "profile", "") or ""),
                 "user_id": str(source.user_id or ""), "chat_id": str(source.chat_id or ""),
                 "thread_id": str(source.thread_id or ""), "source": source, "session_key": session_key,
                 "view": view, "page": "sampling", "message_id": str(message_id) if message_id else None,
                 "callback_token": object(), "history": [],
                 "context": dict(context or {})}
        self._states[state["id"]] = state
        self._owners[identity] = state["id"]
        peek = getattr(getattr(self.adapter, "gateway_runner", None), "_peek_session_state", None)
        if callable(peek):
            current = peek(session_key)
            state["conversation"] = getattr(current, "conversation", None)
        state["route_stamp"] = self._route_stamp(home, session_key)
        return state

    async def open_dashboard(self, source: Any, dashboard: dict[str, Any], providers: list[dict[str, Any]],
                             current_model: str, current_provider: str, session_key: str,
                             profile_home: Any, reply_to: Any = None,
                             invocation_message_id: Any = None) -> bool:
        context = {
            "dashboard": dict(dashboard), "providers": list(providers), "current_model": current_model,
            "current_provider": current_provider, "profile_home": str(profile_home or "")}
        state = self._reuse_target(source, session_key)
        provider_ui = None
        provider_state_id = None
        provider_ui_factory = getattr(self.adapter, "_provider_ui", None)
        if callable(provider_ui_factory):
            provider_ui = provider_ui_factory()
            prepare = getattr(provider_ui, "prepare_model_catalog_state", None)
            if callable(prepare):
                provider_state_id = prepare(
                    source, providers, current_model=current_model, current_provider=current_provider,
                    session_key=session_key, profile_home=profile_home,
                    invocation_message_id=(state.get("invocation_message_id") if state
                                           else invocation_message_id),
                )
        old_provider_state_id = ((state.get("context") or {}).get("provider_catalog_state_id")
                                 if state else None)
        if state:
            state["view"] = "dashboard"
            state["context"].update(context)
        else:
            state = self._new_state(source, session_key, profile_home, "dashboard", context=context)
            state["invocation_message_id"] = self._invocation_id(invocation_message_id)
        if provider_state_id is not None:
            state["context"]["provider_catalog_state_id"] = provider_state_id
            state["context"]["model_catalog_callback_data"] = f"pv:{provider_state_id}:models:0"
            discard = getattr(provider_ui, "discard_prepared_model_catalog_state", None)
            if old_provider_state_id is not None and callable(discard):
                discard(old_provider_state_id)
        text, markup = self._dashboard_view(state)
        if self._reuse_target(source, session_key):
            await self._edit(state, text, markup)
        else:
            result = await self._send(source, text, markup, reply_to)
            state["message_id"] = self._message_id(result)
        if provider_state_id is not None:
            bind = getattr(provider_ui, "bind_prepared_model_catalog_state", None)
            if state.get("message_id") and callable(bind):
                bind(provider_state_id, state["message_id"])
            else:
                discard = getattr(provider_ui, "discard_prepared_model_catalog_state", None)
                if callable(discard):
                    discard(provider_state_id)
        return True

    async def open_settings(self, source: Any, settings: dict[str, Any], session_key: str,
                            reply_to: Any = None, invocation_message_id: Any = None) -> bool:
        if not self._is_owner(source) or not self._is_dm(source):
            return False
        state = self._reuse_target(source, session_key)
        if state:
            if state.get("view") != "settings":
                state["history"] = []
            state["view"] = "settings"
            state["context"]["settings"] = dict(settings)
        else:
            state = self._new_state(source, session_key, None, "settings", context={"settings": dict(settings)})
            state["invocation_message_id"] = self._invocation_id(invocation_message_id)
        state["page"] = str(settings.get("page") or "sampling")
        text, markup = self._settings_view(state)
        if self._reuse_target(source, session_key):
            await self._edit(state, text, markup)
        else:
            result = await self._send(source, text, markup, reply_to)
            state["message_id"] = self._message_id(result)
        state["settings_revision"] = state.get("settings_revision", 0) + 1
        return True

    async def restore_dashboard(self, source: Any, session_key: str,
                                profile_home: Any = None, message_id: Any = None) -> bool:
        """Re-render the dashboard card this UI still owns, for a provider-UI Back.

        Only the exact card (same owner, session and message) is restored; anything
        else leaves the caller's card alone.
        """
        if str(message_id or "") == "":
            return False
        home = self._home(source, profile_home or None)
        identity = self._identity(home, source, session_key)
        sid = self._owners.get(identity)
        state = self._states.get(sid) if sid is not None else None
        if (state is None or state.get("view") != "dashboard"
                or str(state.get("message_id") or "") != str(message_id)):
            return False
        await self._refresh(state)
        return True

    @staticmethod
    def _invocation_id(value: Any) -> int | None:
        # Reply anchors can point at a task or topic, not the invoking command.
        try:
            message_id = int(value)
        except (TypeError, ValueError):
            return None
        return message_id if message_id > 0 else None

    async def _close(self, state: dict[str, Any], event: Any) -> None:
        ids = [int(state["message_id"])]
        invocation = state.get("invocation_message_id")
        if invocation and invocation not in ids:
            ids.append(invocation)
        try:
            await self.adapter._client.delete_messages(
                self.adapter._peer_for(state["chat_id"]), ids, revoke=True)
        except Exception:
            logger.warning("Could not delete model controls and their invocation", exc_info=True)
            await self._answer(event, "Could not remove this menu. Try Close again.", True)
            return
        self._discard_provider_catalog_state(state)
        self._drop(state["id"])

    def _discard_provider_catalog_state(self, state: dict[str, Any]) -> None:
        state_context = state.get("context") or {}
        provider_state_id = state_context.pop("provider_catalog_state_id", None)
        state_context.pop("model_catalog_callback_data", None)
        factory = getattr(self.adapter, "_provider_ui", None)
        try:
            provider_ui = factory() if callable(factory) else None
        except Exception:
            logger.debug("Could not access prepared provider model state during cleanup")
            return
        discard = getattr(provider_ui, "discard_prepared_model_catalog_state", None)
        if provider_state_id is not None and callable(discard):
            discard(provider_state_id)

    def _message_id(self, result: Any) -> str | None:
        extractor = getattr(self.adapter, "_sent_message_id", None)
        if callable(extractor):
            value = extractor(result)
            if value is not None:
                return str(value)
        message = getattr(result, "message", None)
        value = getattr(message, "id", None) or getattr(result, "id", None)
        return str(value) if value is not None else None

    def _dashboard_view(self, state: dict[str, Any]) -> tuple[Any, Any]:
        from telethon import types

        data = state["context"]["dashboard"]
        s = state["id"]
        configured = "configured" if data.get("provider_configured") else "not configured"
        count = data.get("model_catalog_count") or "not yet catalogued"
        reasoning = "provider reports support" if data.get("reasoning_supported") is True else (
            "provider reports no support" if data.get("reasoning_supported") is False else "not reported")
        overrides = data.get("session_settings") or {}
        overrides_text = ", ".join(f"{k}={v}" for k, v in overrides.items()) or "provider defaults"
        provider = str(data.get("provider_name", data.get("current_provider", "Unknown")))
        model = str(data.get("current_model", "unknown"))
        reasoning_value = str(data.get("reasoning_effort") or "provider default")
        provider_style = "success" if data.get("provider_configured") else "danger"
        reasoning_style = ("success" if data.get("reasoning_supported") is True else
                           "danger" if data.get("reasoning_supported") is False else "primary")
        text = types.InputRichMessage(blocks=[
            types.PageBlockHeading1(text=types.TextPlain(text="Model dashboard")),
            types.PageBlockParagraph(text=types.TextPlain(
                text="This conversation's active model and API request settings. One active model is shown.",
            )),
            types.PageBlockDivider(),
            types.PageBlockHeading2(text=types.TextPlain(text="Provider")),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Currently "),
                _model_value_pill(provider, f"ms:{s}:value:provider"),
                types.TextPlain(text=" · "),
                _model_value_pill(configured, f"ms:{s}:value:configured", provider_style),
            ])),
            types.PageBlockParagraph(text=types.TextPlain(
                text="Providers opens the live provider list; List/change opens model selection.",
            )),
            types.PageBlockHeading2(text=types.TextPlain(text="Model")),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Currently "),
                _model_value_pill(model, f"ms:{s}:value:model"),
            ])),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Live catalog: "),
                _model_value_pill(count, f"ms:{s}:value:catalog"),
                types.TextPlain(text=" models."),
            ])),
            types.PageBlockHeading2(text=types.TextPlain(text="Reasoning effort")),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Currently "),
                _model_value_pill(reasoning_value, f"ms:{s}:value:reasoning"),
            ])),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Support: "),
                _model_value_pill(reasoning, f"ms:{s}:value:support", reasoning_style),
                types.TextPlain(text="; model-specific support is unconfirmed."),
            ])),
            types.PageBlockHeading2(text=types.TextPlain(text="Conversation request settings")),
            types.PageBlockParagraph(text=types.TextConcat(texts=[
                types.TextPlain(text="Currently "),
                _model_value_pill(overrides_text, f"ms:{s}:value:settings"),
            ])),
            types.PageBlockParagraph(text=types.TextPlain(
                text="Model settings changes only this conversation's API requests. Close dismisses these controls.",
            )),
            types.PageBlockDivider(),
            types.PageBlockParagraph(text=types.TextItalic(text=types.TextConcat(texts=[
                types.TextPlain(text="Page 1 of 1 • total "),
                types.TextBold(text=types.TextPlain(text="1")),
            ]))),
        ])
        catalog_callback = state["context"].get("model_catalog_callback_data")
        models_callback = catalog_callback or f"ms:{s}:models"
        rows = [[("Providers", f"ms:{s}:providers", "primary"),
                 ("List/change", models_callback, "primary")],
                [("Model settings", f"ms:{s}:settings", None)],
                [("Close", f"ms:{s}:close", "danger")]]
        return text, inline_markup(rows)

    def _settings_view(self, state: dict[str, Any]) -> tuple[str, Any]:
        from gateway.model_ui import settings_payload
        data = state["context"]["settings"]
        current_settings = {field.get("key"): field.get("value") for field in data.get("fields", [])
                            if field.get("key")}
        payload = settings_payload(settings=current_settings, page=state["page"],
                                   reasoning_effort=str(data.get("reasoning_effort") or ""),
                                   supported_reasoning_efforts=data.get("reasoning_options"),
                                   reasoning_supported=data.get("reasoning_supported"))
        lines = [
            f"<h1>Model settings — {escape(str(payload['page_label']))}</h1>",
            "<p>Changes apply only to this conversation's API requests.</p>",
            f"<ul><li>{_RANGE_NOTE}</li></ul>",
            "<hr/>",
        ]
        rows: list[list[tuple[str, str, str | None]]] = []
        for field in payload["fields"]:
            value = field["value"]
            shown = "provider default" if value is None else str(value)
            lines.extend([
                f"<h2>{escape(str(field['label']))}</h2>",
                f"<p>Currently <b>{escape(shown)}</b></p>",
                f"<ul><li>Input range "
                f"{field['minimum']:g}–{field['maximum']:g}. "
                f"{escape(_PARAMETER_HELP[field['key']])}</li>"
                "</ul>",
            ])
            key = field["key"]
            # The adjust controls live in the message, under their setting —
            # not in a keyboard row below it, where the five-button rows
            # truncated the setting names. Same callback payloads as before,
            # so the handlers are unchanged: + / − / clear (×, reset to the
            # provider default) / type a value (Type, Enter value). No emojis:
            # × is U+00D7, not the Emoji=Yes U+2715.
            lines.append(rich_button_row([
                rich_button("+", kind="callback", value=f"ms:{state['id']}:inc:{key}"),
                rich_button("−", kind="callback", value=f"ms:{state['id']}:dec:{key}"),
                rich_button("×", kind="callback", value=f"ms:{state['id']}:reset:{key}", style="danger"),
                rich_button("Type", kind="callback", value=f"ms:{state['id']}:input:{key}", style="primary"),
            ]))
        if payload["page"] == "capabilities":
            supported = payload.get("reasoning_supported")
            status = ("provider metadata reports configurable reasoning; model support unconfirmed"
                      if supported is True else ("provider metadata reports no configurable reasoning"
                      if supported is False else "provider metadata has no support information"))
            lines.extend([
                "<h2>Reasoning effort</h2>",
                f"<p>Currently <b>{escape(str(payload['reasoning_effort']))}</b></p>",
                "<ul><li>Requests how much reasoning effort the model should use. Higher levels may use more tokens or time; effects depend on the provider and model.</li></ul>",
                f"<blockquote>{escape(status)}. Provider metadata does not guarantee support by the selected model.</blockquote>",
            ])
            # Never advertise generic reasoning levels as provider support. Only render
            # selectable levels when the live inventory explicitly says they are supported.
            opts = payload.get("reasoning_options", []) if supported is True else []
            if opts:
                lines.append(
                    "<ul><li>Provider-reported values (not model-confirmed): "
                    + escape(", ".join(opts)) + "</li></ul>"
                )
                rows.extend([[(f"Reasoning: {value}", f"ms:{state['id']}:reason:{value}", None)
                              for value in opts[index:index + 2]] for index in range(0, len(opts), 2)])
            else:
                lines.append("<p>No configurable reasoning values are reported for the current model.</p>")
            lines.append("<ul><li>Reset reasoning restores the provider default for this conversation.</li></ul>")
            rows.append([("Reset reasoning", f"ms:{state['id']}:reason:reset", "danger")])
        lines.append("<hr/>")
        rows.append([(f"{idx}. {label}", f"ms:{state['id']}:page:{key}", "primary" if key == state["page"] else None)
                     for idx, (key, label) in enumerate(SETTINGS_PAGES, 1)])
        rows.append([("Back", f"ms:{state['id']}:back", None), ("Close", f"ms:{state['id']}:close", "danger")])
        pending = self._pending.get(state["identity"])
        if pending and time.monotonic() < pending["expires_at"]:
            field = next(f for f in MODEL_SETTINGS if f.key == pending["field"])
            lines.append(
                f"<blockquote><b>Enter {escape(field.label)}</b><br>"
                f"{escape(_PARAMETER_HELP[field.key])}<br>"
                f"Send a number ({field.minimum:g}–{field.maximum:g}). {_RANGE_NOTE}<br>"
                "Expires 10 minutes after pressing Enter value. Use /cancel or another control to cancel. "
                "A valid accepted reply is deleted; other messages are kept. No AI turn is started.</blockquote>"
            )
        elif state.get("input_expired"):
            lines.append("<blockquote>Value prompt expired. Press Enter value to try again.</blockquote>")
        total = len(payload["fields"]) if payload["page"] != "capabilities" else 1
        lines.append(f"<p><i>Page {payload['page_index']} of {payload['page_count']} • total {total}</i></p>")
        return "\n\n".join(lines), inline_markup(rows)

    def _source_matches(self, state: dict[str, Any], source: Any) -> bool:
        return (str(getattr(source, "profile", "") or "") == state["profile"]
                and str(getattr(source, "user_id", "") or "") == state["user_id"]
                and str(getattr(source, "chat_id", "") or "") == state["chat_id"]
                and str(getattr(source, "thread_id", "") or "") == state["thread_id"])

    def _source_for_callback(self, state: dict[str, Any] | None, source: Any) -> Any | None:
        """Validate the exact card identity and restore a topic omitted by Telegram callbacks."""
        if not state or (
            str(getattr(source, "profile", "") or "") != state["profile"]
            or str(getattr(source, "user_id", "") or "") != state["user_id"]
            or str(getattr(source, "chat_id", "") or "") != state["chat_id"]
        ):
            return None
        expected_thread = state["thread_id"]
        incoming_thread = str(getattr(source, "thread_id", "") or "")
        if incoming_thread and incoming_thread != expected_thread:
            return None
        if not incoming_thread and expected_thread:
            try:
                from gateway.session_identity import replace_source
                source = replace_source(source, thread_id=expected_thread)
            except (TypeError, ValueError):
                return None
        return source

    def _route_stamp(self, home: str, session_key: str) -> tuple[Any, dict]:
        try:
            stat = (Path(home) / "config.yaml").stat()
            config_stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            config_stamp = None
        overrides = getattr(getattr(self.adapter, "gateway_runner", None), "_session_model_overrides", {}) or {}
        return config_stamp, dict(overrides.get(session_key) or {})

    async def _session_matches(self, state: dict[str, Any], source: Any) -> bool:
        runner = getattr(self.adapter, "gateway_runner", None)
        # Metadata reuse is safe only for the same route/configuration that was
        # checked when opening this card. A new /model refreshes its inventory.
        if self._route_stamp(state["home"], state["session_key"]) != state.get("route_stamp"):
            return False
        peek = getattr(runner, "_peek_session_state", None)
        if state.get("conversation") is not None and callable(peek):
            current = peek(state["session_key"])
            if getattr(current, "conversation", None) is not state["conversation"]:
                return False
        normalize = getattr(runner, "_normalize_source_for_session_key", None)
        key_for = getattr(runner, "_session_key_for_source", None)
        if not callable(normalize) or not callable(key_for):
            return True
        import asyncio
        normalized = await asyncio.to_thread(normalize, source)
        return str(key_for(normalized)) == str(state["session_key"])

    async def _dispatch(self, state: dict[str, Any], command: str) -> bool:
        runner = getattr(self.adapter, "gateway_runner", None)
        if runner is None:
            return False
        edit_context = command.startswith("/msettings") or command.split(maxsplit=1)[0] == "/model"
        metadata = {}
        if edit_context:
            metadata = {"_telegram_model_ui_state_id": state["id"],
                        "_telegram_model_ui_token": state["callback_token"]}
        event = MessageEvent(text=command, message_type=MessageType.COMMAND, source=state["source"],
                             message_id=state.get("message_id"), reply_to_message_id=state.get("message_id"),
                             metadata=metadata)
        handler = getattr(runner, "_handle_msettings_command" if command.startswith("/msettings") else "_handle_model_command", None)
        if not callable(handler):
            return False
        token = self._callback_target.set(state) if edit_context else None
        try:
            response = await handler(event)
            if response:
                if edit_context:
                    await self._refresh(state, note=str(response))
                else:
                    await self._send(state["source"], str(response), None, state.get("message_id"))
            return response is None
        finally:
            if token is not None:
                self._callback_target.reset(token)

    @staticmethod
    def _callback_id(event: Any) -> str | None:
        """Read Telethon's unique callback-query ID across event-wrapper versions."""
        value = getattr(event, "query_id", None) or getattr(event, "id", None)
        if value is None:
            query = getattr(event, "query", None) or getattr(event, "original_update", None)
            value = getattr(query, "query_id", None) or getattr(query, "id", None)
        return str(value) if value is not None else None

    def _prune_callback_ids(self) -> None:
        cutoff = time.monotonic() - 600
        for key, seen_at in list(self._seen_callbacks.items()):
            if seen_at < cutoff:
                self._seen_callbacks.pop(key, None)
        while len(self._seen_callbacks) > 2048:
            oldest = min(self._seen_callbacks, key=self._seen_callbacks.get)
            self._seen_callbacks.pop(oldest, None)

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        self._prune()
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "ms":
            return False
        try:
            sid = int(parts[1])
        except ValueError:
            await self._answer(event, "This model control has expired.", True)
            return True
        state = self._states.get(sid)
        message = getattr(event, "message", None)
        callback_message_id = str(getattr(event, "message_id", None) or getattr(message, "id", "") or "")
        source = self._source_for_callback(state, source)
        if (not state or source is None
                or (state.get("message_id") and callback_message_id != state["message_id"])):
            await self._answer(event, "This model control is no longer available to this conversation.", True)
            return True
        action = parts[2]
        settings_control = state["view"] == "settings" or action in {
            "settings", "back", "page", "input", "inc", "dec", "reset", "reason",
        }
        if settings_control:
            if not self._is_owner(source):
                await self._answer(event, "Only the configured profile owner can change model settings.", True)
                return True
            if not self._is_dm(source):
                await self._answer(
                    event,
                    "Model settings are available only in the owner's Telegram DM. Open a private chat with the bot to continue.",
                    True,
                )
                return True
        if not await self._session_matches(state, source):
            await self._answer(event, "This session or model configuration changed. Send /model to open fresh controls.", True)
            return True
        if action == "value" and len(parts) == 4:
            data = state["context"]["dashboard"]
            provider = str(data.get("provider_name", data.get("current_provider", "Unknown")))
            model = str(data.get("current_model", "unknown"))
            configured = "configured" if data.get("provider_configured") else "not configured"
            catalog = data.get("model_catalog_count") or "not yet catalogued"
            reasoning = str(data.get("reasoning_effort") or "provider default")
            support = "provider reports support" if data.get("reasoning_supported") is True else (
                "provider reports no support" if data.get("reasoning_supported") is False else "not reported")
            overrides = data.get("session_settings") or {}
            overrides_text = ", ".join(f"{key}={value}" for key, value in overrides.items()) or "provider defaults"
            details = {
                "provider": f"Provider: {provider}",
                "configured": f"Provider credential status: {configured}. Local presence only; no live authentication check was run.",
                "model": f"Current model: {model}",
                "catalog": f"Local model catalog: {catalog} models. Freshness is not implied.",
                "reasoning": f"Reasoning effort: {reasoning}",
                "support": f"Reasoning support: {support}; model-specific support is unconfirmed.",
                "settings": f"Conversation request settings: {overrides_text}",
            }
            detail = details.get(parts[3])
            if detail is None:
                await self._answer(event, "This model value is unavailable.", True)
            else:
                await self._answer(event, detail[:190], True)
            return True
        # Parameter names are labels only. Acknowledge without touching the card,
        # pending input/timer, callback bookkeeping, or conversation settings.
        if action == "noop":
            await self._answer(event)
            return True
        self._prune_callback_ids()
        callback_id = self._callback_id(event)
        dedupe_key = f"{sid}:{callback_id}" if callback_id else None
        if dedupe_key and dedupe_key in self._seen_callbacks:
            await self._answer(event)
            return True
        if state.get("_callback_busy"):
            await self._answer(event, "This model control is updating. Please try again shortly.", True)
            return True
        state["_callback_busy"] = True
        if dedupe_key:
            self._seen_callbacks[dedupe_key] = time.monotonic()
        try:
            self._clear_pending(state["identity"])
            state.pop("input_expired", None)
            await self._answer(event)
            mutating = action in {"inc", "dec", "reset", "reason"} and len(parts) == 4
            if mutating:
                try:
                    await self._edit(
                        state,
                        "<h2>Working…</h2><p>Updating this conversation's model settings. Please wait for the result below.</p>",
                        None,
                    )
                except Exception:
                    logger.debug("Could not render model-settings progress", exc_info=True)
            if action == "close":
                await self._close(state, event)
                return True
            if action == "providers":
                self._discard_provider_catalog_state(state)
                await self._dispatch(state, "/model providers")
                return True
            if action == "models":
                await self._dispatch(state, "/model list")
                return True
            if action == "settings":
                await self._dispatch(state, "/msettings page 1")
                return True
            if action == "back":
                history = state.get("history") or []
                if history:
                    previous = history.pop()
                    page_number = next(
                        (str(i) for i, (key, _label) in enumerate(SETTINGS_PAGES, 1)
                         if key == previous), None)
                    if page_number:
                        await self._dispatch(state, f"/msettings page {page_number}")
                        return True
                await self._dispatch(state, "/model")
                return True
            if action == "page" and len(parts) == 4:
                page = parts[3]
                if page != state.get("page"):
                    state.setdefault("history", []).append(state.get("page"))
                page_number = next((str(i) for i, (key, _label) in enumerate(SETTINGS_PAGES, 1) if key == page), None)
                if page_number:
                    await self._dispatch(state, f"/msettings page {page_number}")
                return True
            if action == "input" and len(parts) == 4:
                field = next((f for f in MODEL_SETTINGS if f.key == parts[3]), None)
                if field and state["view"] == "settings" and field.page == state["page"]:
                    state["pending_field"] = field.key
                    pending = {"state_id": sid, "field": field.key,
                               "expires_at": time.monotonic() + _INPUT_TTL,
                               "started_at": time.time()}
                    pending["timer"] = asyncio.get_running_loop().call_later(
                        _INPUT_TTL, self._expire_pending, state["identity"], pending)
                    self._pending[state["identity"]] = pending
                    await self._refresh(state)
                return True
            if action in {"inc", "dec", "reset"} and len(parts) == 4:
                key = parts[3]
                field = next((f for f in MODEL_SETTINGS if f.key == key), None)
                if not field:
                    return True
                state_data = {item["key"]: item.get("value")
                              for item in state["context"]["settings"].get("fields", [])}
                try:
                    if action == "reset":
                        succeeded = await self._dispatch(state, f"/msettings reset {key}")
                        result_note = f"Reset {field.label} to the provider default."
                    else:
                        current = state_data.get(key, field.minimum)
                        if current is None:
                            current = field.minimum
                        step = field.step or 1
                        value = min(field.maximum, max(field.minimum, float(current) + (step if action == "inc" else -step)))
                        if field.kind == "int":
                            value = round(value)
                        succeeded = await self._dispatch(state, f"/msettings set {key} {value:g}")
                        result_note = f"Set {field.label} to {value:g}."
                    if succeeded:
                        await self._refresh(state, note=result_note)
                except Exception:
                    logger.warning("Telegram model-settings update failed", exc_info=True)
                    await self._refresh(state, note=f"Could not update {field.label}; the setting was not confirmed.")
                return True
            if action == "reason" and len(parts) == 4:
                try:
                    succeeded = await self._dispatch(state, f"/msettings reasoning {parts[3]}")
                    if succeeded:
                        await self._dispatch(state, "/msettings page 3")
                        await self._refresh(state, note=f"Reasoning effort set to {parts[3]}.")
                except Exception:
                    logger.warning("Telegram reasoning selection failed", exc_info=True)
                    await self._refresh(state, note="Could not update reasoning effort; the change was not confirmed.")
                return True
            await self._answer(event, "Unknown model control.", True)
            return True
        finally:
            state.pop("_callback_busy", None)

    async def _refresh(self, state: dict[str, Any], note: str = "") -> None:
        text, markup = self._settings_view(state) if state["view"] == "settings" else self._dashboard_view(state)
        if note:
            # Keep feedback separate from content, and the page index last.
            footer = text.rfind("<p><i>Page ")
            text = text[:footer] + f"<blockquote>{escape(note)}</blockquote>\n\n" + text[footer:]
        await self._edit(state, text, markup)

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        self._prune()
        source_prefix = (str(getattr(source, "profile", "") or ""),
                         str(getattr(source, "user_id", "") or ""),
                         str(getattr(source, "chat_id", "") or ""),
                         str(getattr(source, "thread_id", "") or ""))
        entry = next(((key, value) for key, value in self._pending.items()
                      if key[1:5] == source_prefix), None)
        if entry is None:
            message = getattr(event, "message", None)
            reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
            expired = next((state for state in self._states.values()
                            if state.get("input_expired") and self._source_matches(state, source)
                            and reply_id is not None and str(reply_id) == state.get("message_id")), None)
            if (expired and self._is_owner(source) and self._is_dm(source)
                    and not expired.get("_callback_busy") and await self._session_matches(expired, source)):
                await self._refresh(expired)
                return True
            return False
        key, pending = entry
        state = self._states.get(pending["state_id"])
        if (not state or not self._source_matches(state, source)
                or self._owners.get(key) != state["id"]
                or state.get("pending_field") != pending["field"]
                or state["view"] != "settings"
                or not any(f.key == pending["field"] and f.page == state["page"] for f in MODEL_SETTINGS)):
            self._clear_pending(key)
            return False
        if not self._is_owner(source) or not self._is_dm(source):
            self._clear_pending(key)
            return False
        if state.get("_callback_busy"):
            return False
        state["_callback_busy"] = True
        try:
            if not await self._session_matches(state, source):
                self._clear_pending(key)
                return False
            # Session resolution can yield while a timer/cancellation replaces this prompt.
            if self._pending.get(key) is not pending or time.monotonic() >= pending["expires_at"]:
                if self._pending.get(key) is pending:
                    self._expire_pending(key, pending)
                if state.get("input_expired"):
                    await self._refresh(state)
                return False
            message = getattr(event, "message", None)
            date = getattr(message, "date", None)
            if date is not None and date.timestamp() + 1 < pending["started_at"]:
                return False
            reply_header = getattr(message, "reply_to", None)
            reply_id = getattr(reply_header, "reply_to_msg_id", None)
            if reply_id is not None and str(reply_id) != state.get("message_id"):
                return False
            raw = (text or "").strip()
            if raw.startswith("/"):
                self._clear_pending(key)
                await self._refresh(state)
                return raw.casefold() == "/cancel"
            try:
                value = parse_setting_value(pending["field"], raw)
            except ValueError as exc:
                await self._refresh(state, note=str(exc))
                return True
            self._clear_pending(key)
            revision = state.get("settings_revision", 0)
            accepted = await self._dispatch(state, f"/msettings set {pending['field']} {value}")
            fields = state["context"].get("settings", {}).get("fields", [])
            confirmed = (state.get("settings_revision", 0) > revision and
                         any(f.get("key") == pending["field"] and f.get("value") == value for f in fields))
            message_id = getattr(message, "id", None)
            if accepted and confirmed and message_id is not None:
                try:
                    await self.adapter._client.delete_messages(
                        self.adapter._peer_for(source.chat_id), [int(message_id)], revoke=True)
                except Exception:
                    logger.warning("Accepted model setting, but could not delete its input reply", exc_info=True)
            return True
        finally:
            state.pop("_callback_busy", None)
