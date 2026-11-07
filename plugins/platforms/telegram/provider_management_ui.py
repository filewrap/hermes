"""Private native-MTProto provider and model management for Telegram.

Credential inputs are accepted only in a direct chat, never included in UI state/logs, and
written through Hermes' secure credential pool. OAuth callbacks
relay device/browser instructions out-of-band while Hermes' existing auth implementations
perform token exchange and persistence. Every callback is bound to its opening account/chat/
thread/message and every operation re-enters its owning profile scope.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import re
import threading
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlsplit

from plugins.platforms.telegram.mtproto_rich import (
    MAX_RICH_MESSAGE_CHARS, edit_request, inline_markup, input_rich_message, rich_button,
    send_request,
)
logger = logging.getLogger(__name__)
_STATE_TTL_SECONDS = 900
_INPUT_TTL_SECONDS = 600
_PAGE_SIZE = 10
_PROVIDER_PAGE_SIZE = 8
_PROVIDER_FILTERS = {"all", "api_key", "login", "other", "custom"}
_ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_REASONING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_OAUTH_UI_PROVIDERS = {"anthropic", "nous", "openai-codex", "xai-oauth", "minimax-oauth", "openrouter"}


def _page_footer(page: int, pages: int, total: int) -> str:
    """Consistent page/total footer for Telegram catalogue cards."""
    return f"Page {int(page) + 1} of {int(pages)} • total {int(total)}"


_PROVIDER_HINTS = (
    "Use /model to configure model settings.",
    "Use /help to see all commands.",
)


def _provider_hints_block() -> str:
    """Hints footer for the providers card: both hints, professionally aligned."""
    return "<p><i>Hints: " + " · ".join(_PROVIDER_HINTS) + "</i></p>"


def _plain_rich_text(value: Any) -> str:
    """Flatten a native RichText node for locating a provider-status list item."""
    if isinstance(value, str):
        return value
    texts = getattr(value, "texts", None)
    if texts is not None:
        return "".join(_plain_rich_text(item) for item in texts)
    if hasattr(value, "text"):
        return _plain_rich_text(value.text)
    return ""


def _notice_segments(notice: str, limit: int = 240) -> list[tuple[str, bool]]:
    """Split a notice on `code` spans, truncated to ``limit`` plain chars.

    Backtick spans render monospace in both notice paths (TextFixed in
    blocks, <code> in HTML). A lone backtick — or one cut by truncation —
    stays literal text, and a span cut by the budget stays closed.
    """
    raw = str(notice or "").replace("\n", " ")
    segments: list[tuple[str, bool]] = []
    cursor = 0
    for match in _CODE_SPAN_RE.finditer(raw):
        if match.start() > cursor:
            segments.append((raw[cursor:match.start()], False))
        segments.append((match.group(1), True))
        cursor = match.end()
    if cursor < len(raw):
        segments.append((raw[cursor:], False))
    if not segments:
        return [("", False)]
    kept: list[tuple[str, bool]] = []
    remaining = limit
    for segment, is_code in segments:
        if remaining <= 0:
            break
        if len(segment) <= remaining:
            kept.append((segment, is_code))
            remaining -= len(segment)
        else:
            kept.append((segment[:remaining], is_code))
            remaining = 0
    if sum(len(segment) for segment, _ in segments) > limit and kept:
        text, is_code = kept[-1]
        kept[-1] = (text + "…", is_code)
    return kept


def _notice_insert_at(blocks: list[Any]) -> int:
    """Where the notice block goes: above the divider that precedes the
    page footer — never between the divider and the page indexation."""
    from telethon import types

    insert_at = len(blocks)
    if blocks and _plain_rich_text(blocks[-1]).startswith("Page "):
        insert_at -= 1
        if insert_at > 0 and isinstance(blocks[insert_at - 1], types.PageBlockDivider):
            insert_at -= 1
    return insert_at


def _credential_status(provider: dict[str, Any]) -> str:
    if provider.get("credential_load_error") and not provider.get("ready"):
        return "Status unavailable"
    return "Added" if provider.get("ready") else "Missing"


def _provider_detail_rich_message(markdown: str, provider: dict, state_id: int,
                                  credential_count: int) -> Any:
    """Render status as plain RichMessage text; actionable controls stay in the callback keyboard."""
    from telethon import types

    rich = input_rich_message(markdown, rich_format="blocks")
    if not isinstance(rich, types.InputRichMessage):
        raise ValueError("Provider detail status controls require native rich-message blocks")
    credential_status = _credential_status(provider)
    inventory_available = not bool(provider.get("credential_load_error"))
    if inventory_available:
        inventory = f"{credential_count} cred" if credential_count == 1 else f"{credential_count} creds"
    else:
        inventory = "Inventory unavailable"
    status_text = types.TextConcat(texts=[
        types.TextBold(text=types.TextPlain(text="Credential status: ")),
        _provider_status_button(provider, state_id, credential_status),
        types.TextPlain(text=f" · {inventory}"),
    ])
    if not provider.get("enabled", True):
        status_text.texts.append(types.TextPlain(text=" · Disabled in profile configuration"))

    for block_index, block in enumerate(rich.blocks):
        if not isinstance(block, types.PageBlockList):
            continue
        items = list(block.items)
        for item_index, item in enumerate(items):
            if (isinstance(item, types.PageListItemText)
                    and _plain_rich_text(item.text).startswith("Credential status:")):
                items[item_index] = types.PageListItemText(
                    text=status_text,
                    checkbox=getattr(item, "checkbox", None),
                    checked=getattr(item, "checked", None),
                )
                rich.blocks[block_index] = types.PageBlockList(items=items)
                return rich
    raise ValueError("Provider detail status line was not found in its rich-message list")

def _provider_status_button(provider: dict, state_id: int, label: str | None = None,
                            *, available: bool | None = None) -> Any:
    """Return an actionable native RichMessage status pill."""
    from telethon import types

    status = label or _credential_status(provider)
    style = (types.RichButtonStyle(bg_success=True) if status == "Added" else
             types.RichButtonStyle(bg_danger=True) if status == "Missing" else
             types.RichButtonStyle(link=True))
    return types.TextButton(
        text=types.TextPlain(text=status),
        type=types.InlineButtonTypeCallback(data=f"pv:{int(state_id)}:credential-status".encode()),
        style=style,
    )

def _provider_method_button(method: str) -> Any:
    from telethon import types
    return types.TextBold(text=types.TextPlain(text=method))


def _provider_console_button(url: str) -> Any | None:
    from telethon import types

    try:
        parsed = urlsplit(str(url or ""))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment):
            return None
    except (TypeError, ValueError):
        return None
    return types.TextButton(
        text=types.TextPlain(text="Open provider console / setup"),
        type=types.InlineButtonTypeUrl(url=str(url)),
    )


def _provider_setup_rich_message(provider: dict, state_id: int, method: str,
                                 instructions: list[str]) -> Any:
    """Render provider-specific setup instructions as native RichMessage paragraphs."""
    from telethon import types

    blocks = [
        types.PageBlockHeading1(text=types.TextPlain(
            text=f"Setup / connect · {TelegramProviderManagementUI._short(provider.get('name'), 64)}",
        )),
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            types.TextBold(text=types.TextPlain(text="Status: ")),
            _provider_status_button(provider, state_id),
        ])),
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            types.TextBold(text=types.TextPlain(text="Connection method: ")),
            _provider_method_button(method),
        ])),
        types.PageBlockDivider(),
        types.PageBlockHeading2(text=types.TextPlain(text="How to connect")),
    ]
    if provider.get("credential_load_error") and not provider.get("ready"):
        blocks.append(types.PageBlockParagraph(text=types.TextPlain(
            text="Hermes could not read the credential inventory. This does not prove that credentials are missing; retry from Credentials.",
        )))
    for instruction in instructions:
        clean_instruction = re.sub(r"`([^`]*)`", r"\1", str(instruction))
        blocks.append(types.PageBlockParagraph(text=types.TextPlain(text=clean_instruction)))
    console_button = _provider_console_button(str(provider.get("signup_url") or ""))
    if console_button is not None:
        blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[console_button])))
    return types.InputRichMessage(blocks=blocks)


def _credential_type_label(value: Any) -> str:
    raw = str(value or "credential").strip().casefold().replace("-", "_")
    return {
        "oauth": "OAuth",
        "api_key": "API key",
        "apikey": "API key",
        "device_code": "Device sign-in",
        "external_process": "Host sign-in",
    }.get(raw, raw.replace("_", " ").title()[:24] or "Credential")


def _provider_credentials_rich_message(provider: dict, state_id: int,
                                       credentials: list[dict[str, Any]]) -> Any:
    """Show credential metadata without secrets; actions are supplied as callback rows."""
    from telethon import types

    name = TelegramProviderManagementUI._short(provider.get("name"), 64)
    blocks = [
        types.PageBlockHeading1(text=types.TextPlain(text=f"Credentials · {name}")),
        types.PageBlockParagraph(text=types.TextConcat(texts=[
            types.TextBold(text=types.TextPlain(text="Status: ")),
            _provider_status_button(provider, state_id),
        ])),
    ]

    for index, entry in enumerate(credentials[:8]):
        auth_type = _credential_type_label(entry.get("auth_type"))
        raw_status = str(entry.get("status") or "ok").casefold()
        healthy = raw_status in {"ok", "ready", "active", ""}
        state_label = "Ready" if healthy else "Needs attention"
        active_label = "Active" if bool(entry.get("active")) else "Available"
        blocks.append(types.PageBlockParagraph(text=types.TextConcat(texts=[
            types.TextBold(text=types.TextPlain(text=f"Credential {index + 1}")),
            types.TextPlain(text=f" · {auth_type} · {active_label} · "),
            types.TextButton(
                text=types.TextPlain(text=state_label),
                type=types.InlineButtonTypeCallback(
                    data=f"pv:{int(state_id)}:credential-status:{index}".encode(),
                ),
                style=(types.RichButtonStyle(bg_success=True) if healthy
                       else types.RichButtonStyle(bg_danger=True)),
            ),
        ])))

    if not credentials:
        message = (
            "Hermes could not read the credential inventory just now. This does not mean credentials are missing."
            if provider.get("credential_load_error") else
            "No credentials were found in Hermes' secure auth pool for this provider."
        )
        blocks.append(types.PageBlockParagraph(text=types.TextPlain(text=message)))
        blocks.append(types.PageBlockParagraph(text=types.TextPlain(
            text="Use Setup / connect for provider-specific sign-in steps; secrets are never shown on this card.",
        )))

    if len(credentials) > 8:
        blocks.append(types.PageBlockParagraph(text=types.TextPlain(
            text=f"{len(credentials) - 8} additional credential entries.",
        )))
    return types.InputRichMessage(blocks=blocks)


class TelegramProviderManagementUI:
    """Buttoned provider suite; all visible cards use Telegram Rich Messages over MTProto."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = itertools.count(1)
        self._states: dict[int, dict[str, Any]] = {}
        self._pending_inputs: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._oauth_jobs: dict[tuple[str, str, str, str], asyncio.Task] = {}

    @staticmethod
    def _user_key(source: Any) -> tuple[str, str, str]:
        return (str(source.chat_id), str(source.user_id or ""),
                str(source.thread_id) if source.thread_id else "")

    @staticmethod
    def _slug(value: Any) -> str:
        return str(value or "").strip().casefold()

    def _new_state(
        self, source: Any, providers: list[dict], *, current_model: str,
        current_provider: str, session_key: str, profile_home: Any,
        view: str, selected_provider: Optional[str] = None,
    ) -> dict[str, Any]:
        state_id = next(self._counter)
        safe_providers = [self._safe_provider(row) for row in providers]
        safe_providers.sort(key=lambda row: (
            not row["is_current"], not row["configured"],
            int(row.get("catalog_order", 0)), row["name"].casefold(),
        ))
        state = {
            "id": state_id,
            "chat_id": str(source.chat_id),
            "user_id": str(source.user_id or ""),
            "thread_id": str(source.thread_id) if source.thread_id else None,
            "message_id": None,
            "created_at": time.monotonic(),
            # Captured at open: whether this actor may make profile-wide or
            # credential changes (owner-only actions hide/refuse otherwise).
            "is_owner": self._is_owner_actor(source, profile_home),
            "providers": safe_providers,
            "current_model": str(current_model or ""),
            "current_provider": str(current_provider or ""),
            "session_key": str(session_key or ""),
            "profile_home": str(profile_home) if profile_home else "",
            "view": view,
            "history": [],
            "model_flow": False,
            "selected_provider": selected_provider,
            "page": 0,
            "filter": "all",
            "search_query": "",
            "visible_model_indices": [],
            "custom_form": {},
            "oauth_status": "",
            "oauth_url": "",
            "oauth_code": "",
            "oauth_needs_code": False,
        }
        self._states[state_id] = state
        return state

    def prepare_model_catalog_state(
        self, source: Any, providers: list[dict], *, current_model: str,
        current_provider: str, session_key: str, profile_home: Any,
        invocation_message_id: Any = None,
    ) -> int:
        """Create a pv callback capability before the /model card is sent."""
        self._prune()
        state = self._new_state(
            source, providers, current_model=current_model, current_provider=current_provider,
            session_key=session_key, profile_home=profile_home, view="models",
        )
        try:
            invocation_id = int(invocation_message_id) if invocation_message_id is not None else None
        except (TypeError, ValueError):
            invocation_id = None
        if invocation_id is not None and invocation_id <= 0:
            invocation_id = None
        state["invocation_message_id"] = str(invocation_id) if invocation_id is not None else None
        state["invocation_chat_id"] = str(source.chat_id) if invocation_id is not None else None
        state["invocation_user_id"] = str(source.user_id or "") if invocation_id is not None else None
        state["invocation_thread_id"] = (
            str(source.thread_id) if source.thread_id and invocation_id is not None else None
        )
        state["model_flow"] = True
        return state["id"]

    def bind_prepared_model_catalog_state(self, state_id: Any, message_id: Any) -> bool:
        """Bind a prepared pv state to the exact native /model card after it is sent."""
        try:
            sid, mid = int(state_id), int(message_id)
        except (TypeError, ValueError):
            return False
        state = self._states.get(sid)
        if not state or state.get("view") != "models" or mid <= 0:
            return False
        state["message_id"] = str(mid)
        return True

    def discard_prepared_model_catalog_state(self, state_id: Any) -> None:
        try:
            self._states.pop(int(state_id), None)
        except (TypeError, ValueError):
            return

    @staticmethod
    def _safe_provider(row: dict) -> dict[str, Any]:
        """Whitelist display-safe fields. Never copy provider keys, headers, or auth state."""
        slug = str(row.get("slug") or "").strip()[:100]
        description = re.sub(r"^\s*about:\s*", "", str(row.get("description") or ""), flags=re.IGNORECASE)
        models = row.get("models") if isinstance(row.get("models"), list) else []
        default_model = str(row.get("default_model") or "").strip()[:200]
        raw_reasoning = row.get("default_reasoning")
        if raw_reasoning is False:
            default_reasoning = "none"
        else:
            default_reasoning = str(raw_reasoning or "").strip().lower()[:24]
        return {
            "slug": slug,
            "name": str(row.get("name") or slug or "Provider")[:100],
            "description": description[:300],
            "auth_type": str(row.get("auth_type") or "api_key")[:48],
            "api_key_env_vars": [
                value for value in dict.fromkeys(str(v) for v in (row.get("api_key_env_vars") or []))
                if _ENV_VAR_RE.fullmatch(value)
            ][:8],
            "base_url_env_var": (
                str(row.get("base_url_env_var") or "")
                if _ENV_VAR_RE.fullmatch(str(row.get("base_url_env_var") or "")) else ""
            ),
            "signup_url": (
                str(row.get("signup_url") or "")[:300]
                if str(row.get("signup_url") or "").startswith(("https://", "http://")) else ""
            ),
            "catalog_order": max(0, int(row.get("catalog_order") or 0)),
            "is_catalog_provider": bool(row.get("is_catalog_provider")),
            "configured": bool(row.get("configured")),
            "ready": bool(row.get("ready")),
            "is_profile_default": bool(row.get("is_profile_default")),
            "profile_default_model": str(row.get("profile_default_model") or "").strip()[:200],
            "enabled": bool(row.get("enabled", True)),
            "is_current": bool(row.get("is_current")),
            "is_user_defined": bool(row.get("is_user_defined")),
            "models": list(dict.fromkeys(str(model)[:200] for model in models if str(model).strip())),
            "total_models": max(int(row.get("total_models") or len(models)), len(models)),
            "reasoning_supported": row.get("reasoning_supported") is not False,
            "reasoning_efforts": [
                str(level).lower() for level in (row.get("reasoning_efforts") or [])
                if str(level).lower() in _REASONING_LEVELS
            ],
            "default_model": default_model,
            "default_reasoning": default_reasoning,
            "catalog_refreshed": bool(row.get("catalog_refreshed")),
            "catalog_status": str(row.get("catalog_status") or "not_configured")[:32],
            "credentials": [],
        }

    @contextlib.contextmanager
    def _profile_scope(self, state: dict):
        home = str(state.get("profile_home") or "").strip()
        if not home:
            yield
            return
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(Path(home)):
            yield

    @staticmethod
    def _source_for_callback(state: Optional[dict], event: Any, source: Any) -> Any:
        """Validate the exact card identity and restore its topic if Telegram omitted it."""
        if not state or (
            state["chat_id"] != str(getattr(source, "chat_id", "") or "")
            or state["user_id"] != str(getattr(source, "user_id", "") or "")
            or state["message_id"] != str(getattr(event, "message_id", "") or "")
            or str(getattr(source, "chat_type", "")) != "dm"
        ):
            return None

        expected_thread = str(state.get("thread_id") or "")
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

    def _prune(self) -> None:
        now = time.monotonic()
        expired_states: set[int] = set()
        for state_id, state in list(self._states.items()):
            if now - state["created_at"] > _STATE_TTL_SECONDS:
                self._states.pop(state_id, None)
                expired_states.add(state_id)
        for user_key, pending in list(self._pending_inputs.items()):
            if now - pending.get("created_at", now) > _INPUT_TTL_SECONDS:
                self._pending_inputs.pop(user_key, None)
                event = pending.get("input_event")
                if event is not None:
                    event.set()
                if str(pending.get("kind") or "").startswith("custom-") and pending.get("state"):
                    self._schedule_custom_input_cleanup(pending["state"], user_key[0])

    @staticmethod
    def _short(value: Any, limit: int = 72) -> str:
        text = str(value or "").replace("\n", " ").replace("`", "'").strip()
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _provider_button_label(item: dict, selected: bool = False) -> str:
        marker = "Current · " if item.get("is_current") else ("Selected · " if selected else "")
        return f"{marker}{item['name'][:20]} · {item['total_models'] or len(item['models'])}"

    def _current_route_buttons(self, state: dict) -> tuple[str, str]:
        """Current-route values as rich copy buttons: the provider (full slug)
        and the model — copying text has no regular-button equivalent."""
        provider = self._short(state["current_provider"], 48) or "unknown"
        model = self._short(state["current_model"], 72) or "unknown"
        full_provider = str(state["current_provider"] or "unknown")
        if full_provider and len(full_provider) <= 256:
            provider_button = rich_button(provider, kind="copy", value=full_provider, style="success")
        else:
            provider_button = f"<b>{escape(provider)}</b>"
        full_model = str(state["current_model"] or "unknown")
        if full_model and len(full_model) <= 256:
            model_button = rich_button(model, kind="copy", value=full_model, style="success")
        else:
            model_button = f"<b>{escape(model)}</b>"
        return provider_button, model_button

    def _provider_list_button_rows(self, state: dict, items: list) -> list[list[tuple[str, str, Optional[str]]]]:
        """Provider names as one full-width button per row. The current provider
        reads green, the selected one blue; the rest are plain. A full-width row
        keeps the ``Current ·`` / ``Selected ·`` prefix and the credential/setup
        suffix on one line instead of truncating mid-word. Share buttons live
        only on the individual provider page, never on this main list."""
        rows = []
        for index, item in items:
            selected = self._slug(item["slug"]) == self._slug(state.get("selected_provider"))
            prefix = ("Current · " if item["is_current"] else
                      ("Selected · " if selected else ""))
            label = f"{prefix}{item['name'][:20]} · {'credential' if item['ready'] else 'setup'}"
            style = ("success" if item["is_current"] else
                     ("primary" if selected else None))
            rows.append([(label, f"pv:{state['id']}:provider:{index}", style)])
        return rows

    @staticmethod
    def _history_frame(state: dict) -> dict[str, Any]:
        return {"view": state.get("view"), "selected_provider": state.get("selected_provider"),
                "page": int(state.get("page", 0) or 0)}

    def _push_history(self, state: dict) -> None:
        """Record the screen being left so Back returns to it, not to main."""
        history = state.setdefault("history", [])
        frame = self._history_frame(state)
        if not history or history[-1] != frame:
            history.append(frame)

    def _push_history_for(self, state: dict, view: str, selected_provider: Any) -> None:
        """Push the current screen before moving to ``view``, unless already there."""
        if (state.get("view") != view
                or self._slug(state.get("selected_provider")) != self._slug(selected_provider)):
            self._push_history(state)

    def _pop_history(self, state: dict) -> Optional[dict[str, Any]]:
        history = state.setdefault("history", [])
        return history.pop() if history else None

    def _go_back(self, state: dict) -> bool:
        """Restore the last-used screen; False when there is no history."""
        frame = self._pop_history(state)
        if frame is None:
            return False
        state.update(view=frame.get("view", "providers"),
                     selected_provider=frame.get("selected_provider"),
                     page=int(frame.get("page", 0) or 0), notice="")
        return True

    async def _restore_model_dashboard(self, state: dict, source: Any) -> bool:
        """Hand the shared card back to the /model dashboard this UI came from."""
        factory = getattr(self.adapter, "_model_settings_ui", None)
        ui = factory() if callable(factory) else None
        restore = getattr(ui, "restore_dashboard", None)
        if not callable(restore):
            return False
        try:
            restored = await restore(source, state.get("session_key"),
                                     state.get("profile_home") or None,
                                     state.get("message_id"))
        except Exception:
            logger.debug("Could not restore the Telegram model dashboard", exc_info=True)
            return False
        if restored:
            self._states.pop(state["id"], None)
            self._pending_inputs.pop(self._user_key(source), None)
        return bool(restored)

    def _provider(self, state: dict) -> Optional[dict]:
        wanted = self._slug(state.get("selected_provider"))
        return next((p for p in state["providers"] if self._slug(p["slug"]) == wanted), None)

    def _live_search_models(self, state: dict, provider: dict) -> Optional[list[str]]:
        """Live-pool results for this provider's active search, else None.

        The stored list is bound to the provider it was fetched for: switching
        providers falls back to the static list instead of showing stale rows.
        """
        live = state.get("live_search_models")
        if not isinstance(live, list) or not live:
            return None
        if self._slug(state.get("live_search_provider")) != self._slug(provider.get("slug")):
            return None
        return list(live)

    def _search_source_models(self, state: dict, provider: dict) -> list[str]:
        """The model list the models card buttons index into: live-search results
        while a live search for this provider is active, else the static list."""
        live = self._live_search_models(state, provider)
        return live if live is not None else list(provider.get("models") or [])

    @staticmethod
    def _search_prompt_notice(provider: Optional[dict]) -> str:
        """Search input prompt: a provider search names the live pool it queries."""
        if provider is None:
            return "Send a search term in this direct chat."
        return (
            "Send a search term in this direct chat. This searches the provider's "
            "live model list (hundreds of models for large providers), not just "
            "the curated list shown here."
        )

    @staticmethod
    def _clear_search(state: dict) -> None:
        """Drop the search term and any live-search results: back to the static list."""
        state.update(search_query="", page=0)
        state.pop("live_search_models", None)
        state.pop("live_search_provider", None)

    def _select_source_model(self, state: dict, provider: Optional[dict], model_index: int) -> str:
        """Resolve a models-card select index through the displayed list (live
        results while a live search is active, else the static list)."""
        if not provider:
            raise ValueError("Choose a provider before selecting a model")
        source_models = self._search_source_models(state, provider)
        if model_index < 0 or model_index >= len(source_models):
            raise ValueError("That model is no longer listed; search again.")
        return source_models[model_index]

    def _provider_index(self, state: dict) -> int:
        wanted = self._slug(state.get("selected_provider"))
        return next((i for i, item in enumerate(state["providers"])
                     if self._slug(item["slug"]) == wanted), -1)

    def _provider_navigation_rows(self, state: dict, active: str) -> list[list[tuple[str, str, Optional[str]]]]:
        state_id = state["id"]
        provider_index = self._provider_index(state)
        return [
            [
                ("Overview", f"pv:{state_id}:overview", "primary" if active == "overview" else None),
                ("Browse models", f"pv:{state_id}:models:{provider_index}", "primary" if active == "models" else None),
            ],
            [
                ("Setup / connect", f"pv:{state_id}:keys", "primary" if active == "keys" else None),
                ("Credentials", f"pv:{state_id}:credentials", "primary" if active == "credentials" else None),
                ("Reasoning", f"pv:{state_id}:reasoning", "primary" if active == "reasoning" else None),
            ],
        ]

    def _profile_default_route(self, state: dict) -> str:
        row = next((item for item in state["providers"] if item.get("is_profile_default")), None)
        if row is None:
            return "not pinned"
        model = row.get("profile_default_model") or "model not specified"
        return f"{row['slug']} / {model}"

    @classmethod
    def _is_custom_provider(cls, provider: dict) -> bool:
        """True for a user-defined endpoint row, either spelling.

        Snapshot rows spell one endpoint two ways — the bare ``providers:`` /
        legacy slug and ``custom:<slug>`` — and ``is_user_defined`` is set on
        bare canonical slugs too (the inventory warns it is over-inclusive),
        so the bare-spelling disjunct keys on membership in the canonical
        catalog instead.
        """
        slug = str(provider.get("slug") or "").strip().casefold()
        if not slug:
            return False
        if slug == "custom" or slug.startswith("custom:"):
            return True
        if not provider.get("is_user_defined"):
            return False
        try:
            from hermes_cli.models import CANONICAL_PROVIDERS
        except Exception:
            return False
        return slug not in {str(entry.slug or "").casefold() for entry in CANONICAL_PROVIDERS}

    @classmethod
    def _is_custom_endpoint(cls, provider: dict) -> bool:
        """A real user endpoint: custom by the predicate, minus the generic
        ``custom`` template row (a catalog skeleton, not an endpoint)."""
        if (str(provider.get("slug") or "").strip().casefold() == "custom"
                and provider.get("is_catalog_provider")):
            return False
        return cls._is_custom_provider(provider)

    @classmethod
    def _category(cls, provider: dict) -> str:
        slug = str(provider.get("slug") or "").casefold()
        auth_type = str(provider.get("auth_type") or "api_key").casefold()
        if cls._is_custom_provider(provider):
            return "custom"
        if slug in {"custom", "moa", "lmstudio", "actual"} or auth_type in {
            "virtual", "aws_sdk", "vertex", "external_process",
        }:
            return "other"
        if slug == "copilot" or auth_type.startswith("oauth") or auth_type == "copilot":
            return "login"
        return "api_key"

    @classmethod
    def _visible_providers(cls, state: dict) -> list[tuple[int, dict]]:
        selected = str(state.get("filter") or "all")
        query = str(state.get("search_query") or "").casefold().strip()
        return [
            (index, provider) for index, provider in enumerate(state["providers"])
            if (selected == "all" or cls._category(provider) == selected)
            and (selected != "custom" or cls._is_custom_endpoint(provider))
            and (not query or query in provider["name"].casefold()
                 or query in provider["slug"].casefold()
                 or query in provider["description"].casefold()
                 or query in provider["default_model"].casefold())
        ]

    @staticmethod
    def _supports_api_key(provider: dict) -> bool:
        if provider.get("auth_type") == "virtual" or provider.get("slug") == "moa":
            return False
        return provider.get("auth_type") == "api_key" or bool(provider.get("api_key_env_vars"))

    @staticmethod
    def _supports_oauth(provider: dict) -> bool:
        return provider.get("slug") in _OAUTH_UI_PROVIDERS or provider.get("slug") == "qwen-oauth"

    @classmethod
    def _setup_recipe(cls, provider: dict) -> tuple[str, list[str]]:
        """Provider-specific setup description, paired with concrete buttons in the UI."""
        slug = str(provider.get("slug") or "").strip()
        auth_type = str(provider.get("auth_type") or "api_key").casefold()
        if slug == "custom":
            return "Custom OpenAI-compatible endpoint", [
                "Create a named endpoint by entering its URL and optional key; Hermes fetches the live model list for selection.",
                "API keys are saved to Hermes' credential pool, not config.yaml.",
            ]
        if auth_type == "virtual" or slug == "moa":
            return "Virtual routing mode", [
                "No credential is required. Select a configured provider and model first.",
            ]
        if slug == "lmstudio":
            return "Local LM Studio server", [
                "Start the LM Studio local server; Hermes uses its local model catalog.",
                "An API key is optional and the endpoint URL belongs in provider configuration.",
            ]
        if slug == "actual":
            return "Actual hosted relay or local daemon", [
                "Hosted relay uses ACTUAL_API_KEY; local daemons may not require a key.",
                "Use the secure key button here; endpoint settings remain profile configuration.",
            ]
        if auth_type == "aws_sdk":
            return "AWS Bedrock credential chain", [
                "Hermes uses the host's AWS profile, environment credentials or attached role; no key is pasted here.",
                "This card can show local presence but does not run a billable model request.",
            ]
        if auth_type == "vertex":
            return "Google Vertex AI / ADC", [
                "Hermes uses Application Default Credentials and the project/region configured on the host.",
                "This card does not request or expose a service-account key.",
            ]
        if auth_type == "external_process":
            return "Local ACP process", [
                "Hermes launches the provider's local ACP process; this card reports setup state without exposing its credentials.",
            ]
        if slug == "copilot":
            return "GitHub Copilot sign-in or token", [
                "Use an existing GitHub CLI/Copilot login on the Hermes host or add a token using the secure key prompt.",
            ]
        if slug == "qwen-oauth":
            return "Import Qwen Code login", [
                "Hermes can import a Qwen Code login already present on this host; it does not implement a separate Qwen browser flow.",
            ]
        if cls._supports_oauth(provider):
            text = "Start the provider's existing Hermes OAuth/device flow here; approval returns directly to this private chat."
            if cls._supports_api_key(provider):
                text += " An API key may also be added as a separate credential."
            return "Hermes OAuth / device sign-in", [text]
        if cls._supports_api_key(provider):
            instructions = ["Add or rotate an API key here; Hermes stores it in the profile's secure auth credential pool."]
            if provider.get("base_url_env_var"):
                instructions.append(f"Optional endpoint override: `{provider['base_url_env_var']}` is profile configuration, not a secret.")
            return "API key", instructions
        return "Host-native credentials", [
            "This provider uses the host's configured credential chain. Hermes does not expose a safe manual token-add route for it.",
        ]

    def _models_catalog_lines(
        self, state: dict, provider: dict, models: list, filtered_indices: list,
        query: str, live_search: bool, no_models: str,
    ) -> list[str]:
        """Catalog text block for the per-provider models card. A live search
        lists the live pool's entry count and source instead of the curated
        snapshot's; the static rendering is unchanged."""
        if live_search:
            freshness = "Live pool — fetched for this search"
        elif provider.get("catalog_refreshed"):
            freshness = "Live refresh completed"
        else:
            freshness = "Cached/curated snapshot; freshness not verified"
        content_lines = [
            f"# Models · {self._short(provider['name'], 64)}",
            "Select a model button below to update the Hermes default route.",
            "---",
            "## Catalog",
            f"- Entries: **{len(models) if live_search else (provider['total_models'] or len(models))}** · matching search: **{len(filtered_indices)}**",
            (f"- Search: {self._short(query, 60)}" if query else "- Search: All model IDs"),
            *( [f"- Source: Live pool — **{len(models)}** models fetched for this search"] if live_search else [] ),
            f"- Freshness: {freshness}",
            "- Green marks the current model"
            + (" and :free models" if any(":free" in model.casefold() for model in models) else ""),
            "## Current values",
            f"- Route: **`{self._short(state['current_provider'], 64)} / {self._short(state['current_model'] or 'unknown', 160)}`**",
            f"- Profile-wide default: **`{self._profile_default_route(state)}`**",
            f"- Provider-listed default: `{self._short(provider.get('default_model') or 'not available', 160)}`",
        ]
        if no_models:
            content_lines.extend(["", no_models])
        return content_lines

    def _render(self, state: dict) -> tuple[Any, Any]:
        view = state.get("view", "providers")
        provider = self._provider(state)
        rows: list[list[tuple[str, str, Optional[str]]]] = []
        state_id = state["id"]
        pindex = self._provider_index(state)

        if view == "providers":
            providers = state["providers"]
            visible = self._visible_providers(state)
            pages = max(1, (len(visible) + _PROVIDER_PAGE_SIZE - 1) // _PROVIDER_PAGE_SIZE)
            page = max(0, min(int(state.get("page", 0)), pages - 1))
            state["page"] = page
            canonical_count = sum(1 for item in providers if item["is_catalog_provider"])
            extra_count = len(providers) - canonical_count
            categories = {category: sum(1 for item in providers if self._category(item) == category)
                          for category in ("api_key", "login", "other", "custom")}
            custom_count = sum(1 for item in providers if self._is_custom_endpoint(item))
            query = str(state.get("search_query") or "").strip()
            items = visible[page * _PROVIDER_PAGE_SIZE:(page + 1) * _PROVIDER_PAGE_SIZE]
            provider_button, model_button = self._current_route_buttons(state)
            hints = _provider_hints_block()
            # Summary table (replaces the "N matching providers" + "M canonical
            # providers ..." prose): Total / Additionals / Shared names.
            shared_names = [
                item["name"] for _, item in visible
                if self._provider_sharing(state, item).get("shared")
            ]
            summary_table = (
                "<table><tbody>"
                f"<tr><td><b>Total</b></td><td>{len(visible)}</td></tr>"
                f"<tr><td><b>Additionals</b></td><td>{extra_count}</td></tr>"
                "<tr><td><b>Shared</b></td><td>"
                + (escape(", ".join(shared_names)) if shared_names else "—")
                + "</td></tr></tbody></table>"
            )
            blocks = [
                "<h1>Hermes provider suite</h1><p>Browse configured providers to inspect model catalogs or setup options.</p>",
                "<hr />",
                "<h2>Current route</h2>"
                f"<p>Provider: {provider_button}</p>"
                f"<p>Model: {model_button}</p>",
                "<h2>Provider catalog</h2>" + summary_table,
                "<h2>Browse providers</h2><ul>"
                + (f"<li>Search: <b>{escape(self._short(query, 60))}</b></li>" if query
                   else "<li>Search: <b>All providers</b></li>") + "</ul>",
            ]
            if not visible:
                blocks.append("<p>No providers match this filter/search.</p>")
            blocks.append(hints)
            blocks.extend(["<hr />", f"<p><i>{_page_footer(page, pages, len(visible))}</i></p>"])
            if not state.get("model_flow"):
                # Explicit blocks avoid Markdown heading/list bleed on phone clients.
                # Current route keeps Provider and Model in separate paragraphs so
                # each nested copy button stays on its own line.
                standalone_blocks = [
                    "<h1>Providers</h1>",
                    "<p>Choose a provider to browse models or manage setup.</p>",
                    "<hr />",
                    "<h2>Current route</h2>",
                    f"<p>Provider: {provider_button}</p>",
                    f"<p>Model: {model_button}</p>",
                    summary_table,
                ]
                if query:
                    standalone_blocks.append(
                        f"<p>Search: {escape(self._short(query, 60))}</p>")
                if not visible:
                    standalone_blocks.append("<p>No providers match this filter/search.</p>")
                standalone_blocks.append(hints)
                standalone_blocks.extend([
                    "<hr />", f"<p><i>{_page_footer(page, pages, len(visible))}</i></p>",
                ])
                content = "\n".join(standalone_blocks)
            else:
                content = "\n".join(blocks)
            rows.append([
                ("Search providers", f"pv:{state_id}:search", "primary" if query else None),
                ("Clear search", f"pv:{state_id}:clear-search", None),
            ])
            rows.append([
                ("All", f"pv:{state_id}:filter:all", "primary" if state.get("filter") == "all" else None),
                (f"API keys · {categories['api_key']}", f"pv:{state_id}:filter:api_key", "primary" if state.get("filter") == "api_key" else None),
            ])
            rows.append([
                (f"OAuth / login · {categories['login']}", f"pv:{state_id}:filter:login", "primary" if state.get("filter") == "login" else None),
                (f"Host / virtual · {categories['other']}", f"pv:{state_id}:filter:other", "primary" if state.get("filter") == "other" else None),
            ])
            rows.extend(self._provider_list_button_rows(state, items))
            nav = []
            if page:
                nav.append(("‹ Previous", f"pv:{state_id}:page:{page - 1}", "primary"))
            if page + 1 < pages:
                nav.append(("Next ›", f"pv:{state_id}:page:{page + 1}", "primary"))
            if nav:
                rows.append(nav)
            if state.get("is_owner", True):
                rows.append([
                    ("Add custom provider", f"pv:{state_id}:custom-create", "success"),
                    (
                        f"Custom providers · {custom_count}",
                        f"pv:{state_id}:filter:custom",
                        "primary" if state.get("filter") == "custom" else None,
                    ),
                ])
                # Automatic rescue for a removed-but-still-selected provider:
                # on, the turn finishes on the last working route; off, it
                # fails instead of switching. The configured fallback chain
                # is unaffected either way.
                auto_fallback = self._auto_fallback_enabled(state)
                rows.append([
                    (
                        f"Auto fallback: {'On' if auto_fallback else 'Off'}",
                        f"pv:{state_id}:auto-fallback:{'off' if auto_fallback else 'on'}",
                        "success" if auto_fallback else None,
                    ),
                    ("Close", f"pv:{state_id}:close", "danger"),
                ])
            if state.get("model_flow"):
                # "Refresh live catalogs" is gone from the list card; Back
                # stays, and Close rides the auto-fallback row for the owner.
                back_row = [("Back", f"pv:{state_id}:back", "primary")]
                if not state.get("is_owner", True):
                    back_row.append(("Close", f"pv:{state_id}:close", "danger"))
                rows.append(back_row)
            elif not state.get("is_owner", True):
                rows.append([("Close", f"pv:{state_id}:close", "danger")])
            return content, inline_markup(rows)

        if view == "provider":
            if provider is None:
                return "# Providers\n\nThis provider entry is no longer available.", inline_markup(
                    [[("Back", f"pv:{state_id}:home", "primary")]]
                )
            setup_method, _instructions = self._setup_recipe(provider)
            status = _credential_status(provider)
            model_count = provider["total_models"] or len(provider["models"])
            model_default = provider.get("default_model") or "No provider catalog fallback"
            reasoning = provider.get("default_reasoning") or "provider default"
            credentials = provider.get("credentials") or []
            inventory_count = (
                "Inventory unavailable"
                if provider.get("credential_load_error") else f"{len(credentials)} credential(s)"
            )
            catalog = (
                "Live refresh completed"
                if provider.get("catalog_refreshed") else {
                    "cached_or_curated": "Cached/curated snapshot; freshness not verified on this view",
                    "unavailable": "No local catalog available; live refresh may be needed",
                    "not_configured": "Provider not configured",
                }.get(provider.get("catalog_status"), "Cached/curated snapshot; freshness not verified on this view")
            )
            description = self._short(provider.get("description"), 220)
            text = (
                f"# {self._short(provider['name'], 80)}\n\n"
                + (f"{description}\n\n---\n\n" if description else "---\n\n")
                + "## Status & routing\n"
                f"- Provider ID: `{self._short(provider['slug'], 80)}`\n"
                f"- Credential status: **{status}** · {inventory_count}\n"
                f"- Current route: **{'Selected' if provider['is_current'] else 'Not selected'}** · `"
                f"{self._short(state['current_provider'], 64)} / {self._short(state['current_model'] or 'unknown', 100)}`\n"
                f"- Profile-wide default: `{self._short(self._profile_default_route(state), 140)}`\n\n"
                "## Setup & model defaults\n"
                f"- Setup: **{self._short(setup_method, 64)}**\n"
                f"- Provider-listed default/catalog value: `{self._short(model_default, 120)}`\n"
                f"- Default reasoning: **{self._short(reasoning, 32)}**\n\n"
                "## Model catalog\n"
                f"- Models available: **{model_count or 'not available'}**\n"
                f"- Freshness: {catalog}"
                + ("\n\nProvider is disabled in profile configuration." if not provider["enabled"] else "")
                + (f"\n\nEndpoint check: {self._short(state.get('provider_validation'), 180)}"
                   if state.get("provider_validation") else "")
            )
            text = _provider_detail_rich_message(
                # A ready provider with no pool entry still holds one credential
                # (local auth material, e.g. a Nous agent_key or guest identity):
                # count it, or the card reads "Added · 0 creds" for a login
                # that is fully working.
                text, provider, state_id, len(credentials) or (1 if provider.get("ready") else 0),
            )
            rows.extend(self._provider_navigation_rows(state, "overview"))
            if provider["is_current"] and state.get("current_model") and state.get("is_owner", True):
                rows.append([("Make default for all", f"pv:{state_id}:pin-default-all", "success")])
            sharing = self._provider_sharing(state, provider)
            if sharing.get("can_share"):
                if sharing.get("shared"):
                    rows.append([("Stop sharing", f"pv:{state_id}:share-toggle", "danger")])
                else:
                    rows.append([("Share with others", f"pv:{state_id}:share-toggle", "success")])
            if state.get("is_owner", True) and self._is_custom_endpoint(provider):
                rows.append([("Delete provider", f"pv:{state_id}:delete-ask", "danger")])
            rows.append([
                ("Refresh live models", f"pv:{state_id}:refresh", None),
                ("Back", f"pv:{state_id}:back", "primary"),
                ("Close", f"pv:{state_id}:close", "danger"),
            ])
            return text, inline_markup(rows)

        if view == "keys":
            if provider is None:
                return "# Provider setup\n\nProvider unavailable.", inline_markup(
                    [[("Back", f"pv:{state_id}:home", "primary")]]
                )
            method, instructions = self._setup_recipe(provider)
            rows.extend(self._provider_navigation_rows(state, "keys"))
            # Credential adds stay owner-only; setup instructions above are
            # read-only and stay visible to every admitted user.
            if state.get("is_owner", True):
                if provider["slug"] == "custom":
                    rows.append([("Create custom endpoint", f"pv:{state_id}:custom-create", "primary")])
                if self._supports_api_key(provider):
                    rows.append([("Add / rotate API key", f"pv:{state_id}:add-key", "success")])
                if provider["slug"] in _OAUTH_UI_PROVIDERS:
                    rows.append([("Start OAuth / device login", f"pv:{state_id}:oauth", "primary")])
                elif provider["slug"] == "qwen-oauth":
                    rows.append([("Import existing Qwen Code login", f"pv:{state_id}:oauth", "primary")])
            if provider["auth_type"] in {"aws_sdk", "vertex", "external_process", "copilot"}:
                rows.append([("Check host credential status", f"pv:{state_id}:local-check", None)])
            rows.append([("Back", f"pv:{state_id}:back", "primary"),
                         ("Close", f"pv:{state_id}:close", "danger")])
            return _provider_setup_rich_message(
                provider, state_id, self._short(method, 64), instructions,
            ), inline_markup(rows)

        if view == "credentials":
            if provider is None:
                return "# Credentials\n\nProvider unavailable.", inline_markup(
                    [[("Back", f"pv:{state_id}:home", "primary")]]
                )
            credentials = provider.get("credentials") or []
            rows.extend(self._provider_navigation_rows(state, "credentials"))
            is_owner = state.get("is_owner", True)
            for index, entry in enumerate(credentials[:8]):
                if not is_owner:
                    continue
                credential_id = str(entry.get("id") or "")
                priority = max(0, int(entry.get("priority", index)))
                actions: list[tuple[str, str, Optional[str]]] = []
                if priority > 0 and credential_id:
                    actions.append(("Move to first", f"pv:{state_id}:priority:{credential_id},0", None))
                    if priority > 1:
                        actions.append(("Move up", f"pv:{state_id}:priority:{credential_id},{priority - 1}", None))
                if entry.get("refreshable") and credential_id:
                    actions.append(("Refresh OAuth", f"pv:{state_id}:refresh-oauth:{credential_id}", "primary"))
                if str(entry.get("status") or "").casefold() == "exhausted" and credential_id:
                    actions.append(("Reset cooldown", f"pv:{state_id}:reset-one:{credential_id}", None))
                actions.append((f"Remove credential {index + 1}", f"pv:{state_id}:remove-ask:{index}", "danger"))
                rows.append(actions)
            if is_owner and any(entry.get("status") == "exhausted" for entry in credentials):
                rows.append([("Reset all cooldowns", f"pv:{state_id}:reset-all", None)])
            if is_owner and self._supports_api_key(provider):
                rows.append([("Add / rotate key", f"pv:{state_id}:add-key", "success")])
            if is_owner and self._supports_oauth(provider) and provider["slug"] in _OAUTH_UI_PROVIDERS:
                rows.append([("Add another OAuth account", f"pv:{state_id}:oauth", "primary")])
            rows.append([("Back", f"pv:{state_id}:back", "primary"),
                         ("Close", f"pv:{state_id}:close", "danger")])
            return _provider_credentials_rich_message(provider, state_id, credentials), inline_markup(rows)

        if view == "remove-confirm":
            credential = state.get("remove_credential") or {}
            text = (
                f"# Remove credential · {self._short(provider['name'] if provider else 'Provider', 64)}\n\n"
                f"Remove **{self._short(credential.get('label'), 48)}** (`{self._short(credential.get('auth_type'), 24)}`)?\n\n"
                "This deletes the selected Hermes credential-pool entry and its eligible imported-source entry."
            )
            rows = [[("Confirm remove", f"pv:{state_id}:remove-confirm", "danger"),
                     ("Cancel", f"pv:{state_id}:credentials", None)]]
            return text, inline_markup(rows)

        if view == "delete-confirm":
            text = (
                f"# Delete provider · {self._short(provider['name'] if provider else 'Provider', 64)}\n\n"
                f"Delete **{self._short(provider['name'] if provider else 'Provider', 64)}** (`{self._short(provider['slug'] if provider else '', 80)}`)?\n\n"
                "This removes the endpoint from config, deletes its Hermes credential-pool entries, and moves the current/default route if it points here."
            )
            rows = [[("Confirm delete", f"pv:{state_id}:delete-confirm", "danger"),
                     ("Cancel", f"pv:{state_id}:back", None)]]
            return text, inline_markup(rows)

        if view == "collect-key":
            text = (
                f"# Add API key · {self._short(provider['name'] if provider else 'Provider', 64)}\n\n"
                "Send the key in your next direct message. It will not be echoed or logged.\n\n"
                "Send `/cancel` to abort."
            )
            return text, inline_markup([[ ("Cancel", f"pv:{state_id}:cancel-input", "danger") ]])

        if view == "custom-form":
            data = state.get("custom_form") or {}
            step = str(state.get("custom_step") or "complete")
            next_prompt = {
                "name": "Send the endpoint name in your next direct message.",
                "url": "Send the full base URL in your next direct message.",
                "key": "Send the API key, or `skip` if the endpoint lists models without authentication.",
                "complete": "The endpoint details are ready to validate.",
            }.get(step, "The endpoint details are ready to save.")
            lines = ["# Custom endpoint", "",
                     f"Name: **{self._short(data.get('name') or 'waiting', 48)}**",
                     "",
                     f"Base URL: `{self._short(data.get('base_url') or 'waiting', 96)}`",
                     "",
                     next_prompt,
                     "",
                     "The model list is checked before adding. Keys are stored securely."]
            rows = []
            rows.append([("Cancel", f"pv:{state_id}:cancel-input", "danger")])
            return "\n".join(lines), inline_markup(rows)

        if view == "collect-custom-key":
            return (
                f"# Add key · {self._short((state.get('custom_form') or {}).get('name'), 64)}\n\n"
                "Send the API key in your next direct message. Send `skip` only if the endpoint allows public model listing.\n\n"
                "Send `/cancel` to return."
            ), inline_markup([[ ("Cancel", f"pv:{state_id}:cancel-input", "danger") ]])

        if view == "oauth":
            label = provider["name"] if provider else "Provider"
            status = state.get("oauth_status") or "Starting provider login…"
            lines = [f"<h1>Sign in · {escape(self._short(label, 64))}</h1>",
                     f"<p>Status: <b>{escape(self._short(status, 180))}</b></p>"]
            if state.get("oauth_url"):
                lines.append(rich_button(
                    "Open provider authorization page", kind="url", value=str(state["oauth_url"])))
            if state.get("oauth_code"):
                code = str(state["oauth_code"])
                if len(code) <= 256:
                    lines.append(rich_button(
                        f"Verification code: {self._short(code, 48)}", kind="copy",
                        value=code, style="success"))
                else:
                    lines.append(
                        f"<p>Verification code: <b>{escape(self._short(code, 48))}</b></p>")
            if state.get("oauth_needs_code"):
                lines.append("<p>After authorization, send the returned code in this direct chat.</p>")
            else:
                lines.append("<p>Complete approval in your browser/device; Hermes is waiting for the provider response.</p>")
            rows = [[("Refresh status", f"pv:{state_id}:oauth-status", None),
                     ("Provider details", f"pv:{state_id}:provider:{pindex}", "primary")],
                    [("Back", f"pv:{state_id}:back", "primary"),
                     ("Close", f"pv:{state_id}:close", "danger")]]
            return "\n".join(lines), inline_markup(rows)

        if view == "reasoning":
            if provider is None:
                return "# Reasoning defaults\n\nProvider unavailable.", inline_markup(
                    [[("Back", f"pv:{state_id}:home", "primary")]]
                )
            if provider.get("reasoning_supported") is False:
                return (
                    f"# Reasoning · {self._short(provider['name'], 64)}\n\n"
                    "Hermes reports that this provider's current model does not support reasoning controls.",
                    inline_markup([*self._provider_navigation_rows(state, "reasoning"),
                                   [("Back", f"pv:{state_id}:back", "primary"),
                                    ("Close", f"pv:{state_id}:close", "danger")]]),
                )
            model = provider.get("default_model") or (state["current_model"] if provider["is_current"] else "")
            current_effort = provider.get("default_reasoning") or "provider default"
            supported = provider.get("reasoning_efforts") or list(_REASONING_LEVELS)
            levels = [("inherit", "Provider default"), ("none", "Disable")]
            levels.extend((level, level.title()) for level in supported if level in _REASONING_LEVELS)
            # The effort buttons save a profile-wide default (owner-only);
            # an allowed user still gets the session button below.
            if state.get("is_owner", True):
                for offset in range(0, len(levels), 2):
                    row = []
                    for value, label in levels[offset:offset + 2]:
                        selected = (value == "inherit" and current_effort == "provider default") or value == current_effort
                        row.append((label, f"pv:{state_id}:reason:default.{value}",
                                    "success" if selected else None))
                    rows.append(row)
            rows.extend(self._provider_navigation_rows(state, "reasoning"))
            if provider["is_current"]:
                rows.append([("Apply to this session", f"pv:{state_id}:reason-session", "primary")])
            rows.append([("Back", f"pv:{state_id}:back", "primary"),
                         ("Close", f"pv:{state_id}:close", "danger")])
            text = (
                f"# Reasoning · {self._short(provider['name'], 64)}\n\n"
                f"Model: {self._short(model or 'select a model first', 100)}\n"
                f"Current default: `{self._short(current_effort, 24)}`\n\n"
                "Choose an effort above to save an override for this provider's default model. "
                "Use the separate session button for a temporary current-session change. "
                "Support varies by model."
            )
            return text, inline_markup(rows)

        if view == "reasoning-session":
            if provider is None or not provider["is_current"]:
                return "# Session reasoning\n\nChoose the current provider route first.", inline_markup(
                    [[("Back", f"pv:{state_id}:back", "primary")]]
                )
            if provider.get("reasoning_supported") is False:
                return (
                    "# Session reasoning\n\nHermes reports that the current model does not support reasoning controls.",
                    inline_markup([[ ("Back", f"pv:{state_id}:back", "primary") ]]),
                )
            levels = [("none", "Disable")]
            supported = provider.get("reasoning_efforts") or list(_REASONING_LEVELS)
            levels.extend((level, level.title()) for level in supported if level in _REASONING_LEVELS)
            for offset in range(0, len(levels), 2):
                rows.append([
                    (label, f"pv:{state_id}:reason-session-value:{value}", None)
                    for value, label in levels[offset:offset + 2]
                ])
            rows.append([("Back", f"pv:{state_id}:back", "primary")])
            return (
                f"# Session reasoning · {self._short(provider['name'], 64)}\n\n"
                f"Current model: `{self._short(state['current_model'], 100)}`\n\n"
                "Choose a session-only effort. This does not change the persisted model/provider default.",
                inline_markup(rows),
            )

        if view == "models":
            if provider is None:
                available = [(i, p) for i, p in enumerate(state["providers"]) if p["models"]]
                pages = max(1, (len(available) + _PROVIDER_PAGE_SIZE - 1) // _PROVIDER_PAGE_SIZE)
                page = max(0, min(int(state.get("page", 0)), pages - 1))
                state["page"] = page
                items = available[page * _PROVIDER_PAGE_SIZE:(page + 1) * _PROVIDER_PAGE_SIZE]
                content = "\n".join([
                    "# Model catalogs",
                    "Browse providers with available models and open a catalog.",
                    "---",
                    "## Current values",
                    f"- Route: **`{self._short(state['current_provider'], 64)} / {self._short(state['current_model'] or 'unknown', 140)}`**",
                    f"- Profile-wide default: **`{self._profile_default_route(state)}`**",
                    "## Available catalogs",
                    f"Providers with models: **{len(available)}**",
                    ("No model IDs are available yet. Open a provider and refresh its live catalog."
                     if not available else "Model choices are shown as selectable buttons below."),
                    "---",
                    _page_footer(page, pages, len(available)),
                ])
                for offset in range(0, len(items), 2):
                    rows.append([
                        (self._provider_button_label(
                            item, self._slug(item["slug"]) == self._slug(state.get("selected_provider"))),
                         f"pv:{state_id}:models:{index}",
                         ("primary" if self._slug(item["slug"]) == self._slug(state.get("selected_provider"))
                          else ("success" if item["is_current"] else None)))
                        for index, item in items[offset:offset + 2]
                    ])
                nav = []
                if page:
                    nav.append(("‹ Previous", f"pv:{state_id}:page:{page - 1}", "primary"))
                if page + 1 < pages:
                    nav.append(("Next ›", f"pv:{state_id}:page:{page + 1}", "primary"))
                if nav:
                    rows.append(nav)
                rows.append([("Search providers", f"pv:{state_id}:search", None),
                             ("Back", f"pv:{state_id}:back", "primary")])
                rows.append([("Close", f"pv:{state_id}:close", "danger")])
                return content, inline_markup(rows)

            models = self._search_source_models(state, provider)
            live_search = self._live_search_models(state, provider) is not None
            query = str(state.get("search_query") or "").casefold().strip()
            filtered_indices = [i for i, model in enumerate(models) if not query or query in model.casefold()]
            state["visible_model_indices"] = filtered_indices
            pages = max(1, (len(filtered_indices) + _PAGE_SIZE - 1) // _PAGE_SIZE)
            page = max(0, min(int(state.get("page", 0)), pages - 1))
            state["page"] = page
            selected = filtered_indices[page * _PAGE_SIZE:(page + 1) * _PAGE_SIZE]
            no_models = ""
            if not models:
                no_models = "No models are available. Refresh the live catalog or set up the provider first."
            elif not filtered_indices:
                no_models = "No model IDs match the search."
            content_lines = self._models_catalog_lines(
                state, provider, models, filtered_indices, query, live_search, no_models,
            )
            content_lines.extend(["---", _page_footer(page, pages, len(filtered_indices))])
            content = "\n".join(content_lines)

            for offset in range(0, len(selected), 2):
                button_row = []
                for source_index in selected[offset:offset + 2]:
                    model = models[source_index]
                    selected_model = model == state["current_model"] and provider["is_current"]
                    label = ("Current · " if selected_model else "") + self._short(model, 24)
                    # The current model reads green, like every other
                    # current/selected marker on these screens; a :free model
                    # is green too, and the "Current · " prefix tells them apart.
                    if selected_model or ":free" in model.casefold():
                        style = "success"
                    else:
                        style = None
                    button_row.append((label[:32], f"pv:{state_id}:select:{source_index}", style))
                rows.append(button_row)
            nav = []
            if page:
                nav.append(("‹ Previous", f"pv:{state_id}:page:{page - 1}", "primary"))
            if page + 1 < pages:
                nav.append(("Next ›", f"pv:{state_id}:page:{page + 1}", "primary"))
            if nav:
                rows.append(nav)
            rows.append([("Search models", f"pv:{state_id}:search", "primary"),
                         ("Clear search", f"pv:{state_id}:clear-search", None)])
            if provider["is_current"] and state.get("current_model") and state.get("is_owner", True):
                rows.append([("Make default for all", f"pv:{state_id}:pin-default-all", "success")])
            rows.append([("Refresh live catalog", f"pv:{state_id}:refresh", None),
                         ("Provider", f"pv:{state_id}:provider:{pindex}", "primary")])
            rows.append([("All models", f"pv:{state_id}:all-models", None),
                         ("Back", f"pv:{state_id}:back", "primary"),
                         ("Close", f"pv:{state_id}:close", "danger")])
            return content, inline_markup(rows)

        return "# Hermes provider suite", inline_markup([[ ("Close", f"pv:{state_id}:close", "danger") ]])

    def _load_credentials(self, provider: dict) -> list[dict[str, Any]]:
        """Load only display-safe credential metadata from Hermes' auth pool."""
        provider.pop("credential_load_error", None)
        try:
            from agent.credential_pool import REFRESHABLE_OAUTH_PROVIDERS, load_pool
            pool = load_pool(str(provider["slug"]))
            entries = pool.entries()
            active = pool.peek()
            return [{
                "id": str(entry.id)[:32],
                # Labels may be user-supplied or derived from a token; never render them.
                "label": f"Credential {index}",
                "auth_type": str(entry.auth_type or "")[:24],
                "source": str(entry.source or "")[:48],
                "active": bool(active and active.id == entry.id),
                "priority": max(0, int(entry.priority)),
                "status": str(entry.last_status or "ok")[:16],
                "refreshable": bool(
                    provider["slug"] in REFRESHABLE_OAUTH_PROVIDERS
                    and entry.auth_type == "oauth" and entry.refresh_token
                    and (provider["slug"] != "nous" or entry.source == "device_code")
                ),
            } for index, entry in enumerate(entries, 1)]
        except Exception as exc:
            provider["credential_load_error"] = type(exc).__name__
            logger.warning("Could not read Telegram provider credential metadata (%s)", type(exc).__name__)
            return []

    @staticmethod
    def _has_local_auth_material(provider: dict) -> bool:
        """Check only local credential/config stores; never probe a provider endpoint."""
        slug = str(provider.get("slug") or "").strip().lower()
        if not slug:
            return False
        try:
            from hermes_cli.auth import (
                PROVIDER_REGISTRY, _explicit_env_credentials_present,
                _explicit_pool_entry_present, get_api_key_provider_status,
                get_external_process_provider_status, provider_has_local_auth_material,
            )
            if _explicit_env_credentials_present(slug) or _explicit_pool_entry_present(slug):
                return True
            pconfig = PROVIDER_REGISTRY.get(slug)
            if pconfig and pconfig.auth_type == "api_key":
                return bool(get_api_key_provider_status(slug).get("logged_in"))
            if pconfig and pconfig.auth_type == "external_process":
                return bool(get_external_process_provider_status(slug).get("logged_in"))
            return provider_has_local_auth_material(slug)
        except Exception:
            return False

    def _is_owner_actor(self, source: Any, profile_home: Any) -> bool:
        """True when the actor may make profile-wide or credential changes.

        No owner distinction exists unless multiuser is enabled for the
        profile home (``OWNER_TELEGRAM_ID`` set): there every admitted user
        is treated as the owner, matching the adapter's pre-dispatch policy.
        Under multiuser the adapter's owner check wins, falling back to the
        configured owner id; an undeterminable owner fails closed.
        """
        try:
            from gateway.mongo_multiuser import enabled

            home = Path(profile_home) if profile_home else None
            if home is None or not enabled(home):
                return True
        except Exception:
            return False
        checker = getattr(self.adapter, "_is_owner_user", None)
        if callable(checker):
            try:
                return bool(checker(getattr(source, "user_id", None), source))
            except Exception:
                return False
        try:
            from gateway.mongo_multiuser import owner_user_id

            owner = owner_user_id(home)
            return bool(owner) and str(getattr(source, "user_id", "") or "") == owner
        except Exception:
            return False

    def _provider_sharing(self, state: dict, provider: Optional[dict] = None) -> dict:
        """Sharing state for the acting user: can they share this provider, and is it shared?

        Only the user who added a credential can share it; the button is hidden
        for everyone else (and for providers with no owned entries).
        """
        if provider is None:
            return {"can_share": False, "shared": False}
        actor = str(state.get("user_id") or "").strip()
        if not actor:
            return {"can_share": False, "shared": False}
        try:
            from gateway.provider_sharing import provider_sharing_state
            from hermes_cli.auth_commands import _normalize_provider

            # Resolve the UI slug to its pool key first: a keyed custom
            # provider arrives as ``custom:<key>`` but its credentials live
            # under the bare key.
            return provider_sharing_state(
                _normalize_provider(str(provider.get("slug") or "")), actor)
        except Exception:
            return {"can_share": False, "shared": False}

    async def _toggle_provider_sharing(self, state: dict, provider: dict) -> str:
        """Flip a provider's share flag for the user who added it.

        Anyone else — and a provider with no credential yet — gets a clear
        reason instead of a silent no-op; a failed write says so too.
        """
        sharing = self._provider_sharing(state, provider)
        actor = str(state.get("user_id") or "").strip()
        if not actor or not sharing.get("can_share"):
            if not sharing.get("has_entries"):
                return ("This provider has no credential to share yet. "
                        "Add a credential first, then share it with the other users of this profile.")
            return "Only the user who added this provider can share it."
        new_shared = not bool(sharing.get("shared"))
        try:
            from hermes_cli.auth_commands import set_provider_shared_for_ui

            result = await asyncio.to_thread(
                set_provider_shared_for_ui, provider["slug"], actor, new_shared,
            )
            if isinstance(result, dict) and "shared" in result:
                new_shared = bool(result["shared"])
        except Exception as exc:
            logger.warning("Could not update Telegram provider sharing (%s)", type(exc).__name__)
            return "Sharing could not be updated. Try again."
        return ("Provider shared with the other users of this profile."
                if new_shared else "Sharing stopped. Only you can use this provider now.")

    def _refresh_provider_credentials(self, state: dict, provider: Optional[dict] = None) -> None:
        if provider is None:
            return
        # Credential reads must run under the profile's scope: the snapshot
        # already does, but an unscoped read here (ambient home) overwrote a
        # correct scoped "configured" with "Missing · 0 creds" for providers
        # whose login lives under the profile home.
        with self._profile_scope(state):
            provider["credentials"] = self._load_credentials(provider)
            # "Configured" means "has credentials right now", not "was ever set up":
            # a route selection (config.yaml model.provider) is not a credential, so
            # removing the last credential must clear the flag or /model keeps
            # showing the provider as added with 0 credentials.
            provider["ready"] = bool(
                provider["credentials"] or self._has_local_auth_material(provider)
            )
            provider["configured"] = bool(provider["ready"])

    @staticmethod
    def _prepare_native_controls(state: dict, text: Any, markup: Any) -> tuple[Any, Any]:
        """Preserve ordinary callback keyboards; only authored copy controls live in the body."""
        return text, markup

    async def _send_new(self, source: Any, state: dict, reply_to: Any = None) -> bool:
        if self.adapter._client is None:
            return False
        text, markup = self._render(state)
        text, markup = self._prepare_native_controls(state, text, markup)
        reply_id = reply_to or (int(state["thread_id"]) if state["thread_id"] else None)
        try:
            response = await self.adapter._mtproto_rich_request(send_request(
                self.adapter._peer_for(state["chat_id"]), text,
                reply_to=int(reply_id) if reply_id is not None else None,
                top_msg_id=int(state["thread_id"]) if state["thread_id"] else None,
                reply_markup=markup,
            ))
            state["message_id"] = self.adapter._sent_message_id(response)
            return bool(state["message_id"])
        except Exception:
            logger.warning("Could not send Telegram provider-management card (%s)", "transport error")
            self._states.pop(state["id"], None)
            return False

    async def open(
        self, source: Any, providers: list[dict], *, current_model: str,
        current_provider: str, session_key: str, profile_home: Any,
        view: str = "providers", selected_provider: Optional[str] = None,
        reply_to: Any = None, message_id: Any = None, model_flow: Optional[bool] = None,
        invocation_message_id: Any = None,
    ) -> bool:
        self._prune()
        if not self.adapter._client or not source.user_id:
            return False
        target_message_id = None
        if message_id is not None:
            try:
                target_message_id = int(message_id)
            except (TypeError, ValueError):
                return False
            if target_message_id <= 0:
                return False
        invocation_id = None
        if invocation_message_id is not None:
            try:
                invocation_id = int(invocation_message_id)
            except (TypeError, ValueError):
                return False
            if invocation_id <= 0:
                return False
        if str(getattr(source, "chat_type", "")) != "dm":
            warning = "# Provider management\n\nOpen a direct chat with this bot to inspect provider and credential status."
            try:
                if target_message_id is not None:
                    request = edit_request(self.adapter._peer_for(source.chat_id), target_message_id, warning)
                else:
                    request = send_request(
                        self.adapter._peer_for(source.chat_id), warning,
                        reply_to=int(reply_to) if reply_to is not None else None,
                        top_msg_id=int(source.thread_id) if source.thread_id else None,
                    )
                await self.adapter._mtproto_rich_request(request)
                return True
            except Exception:
                logger.warning("Could not send Telegram provider-management privacy notice")
                return False
        state = self._new_state(
            source, providers, current_model=current_model, current_provider=current_provider,
            session_key=session_key, profile_home=profile_home, view=view,
            selected_provider=selected_provider,
        )
        state["invocation_message_id"] = str(invocation_id) if invocation_id is not None else None
        state["invocation_chat_id"] = str(source.chat_id) if invocation_id is not None else None
        state["invocation_user_id"] = str(source.user_id or "") if invocation_id is not None else None
        state["invocation_thread_id"] = str(source.thread_id) if source.thread_id and invocation_id is not None else None
        state["model_flow"] = bool(target_message_id is not None if model_flow is None else model_flow)
        if target_message_id is not None:
            state["message_id"] = str(target_message_id)
        with self._profile_scope(state):
            if state["view"] == "providers":
                for provider in state["providers"]:
                    # Same readiness rule as _refresh_provider_credentials: a pool
                    # entry of any source counts, so providers adopted from an
                    # external CLI (gh_cli, claude_code, ...) are not shown as
                    # unconfigured just because their source is not "explicit".
                    self._refresh_provider_credentials(state, provider)
            else:
                self._refresh_provider_credentials(state, self._provider(state))
            if target_message_id is not None:
                text, markup = self._render(state)
                text, markup = self._prepare_native_controls(state, text, markup)
                try:
                    await self.adapter._mtproto_rich_request(edit_request(
                        self.adapter._peer_for(state["chat_id"]), target_message_id, text,
                        reply_markup=markup,
                    ))
                except Exception:
                    logger.warning("Could not edit Telegram provider-management card (%s)", "transport error")
                    self._states.pop(state["id"], None)
                    return False
            elif not await self._send_new(source, state, reply_to=reply_to):
                return False
        self._prune()
        return True

    def _notice_block(self, notice: str) -> Any:
        """Notice paragraph: `code` spans render monospace (TextFixed)."""
        from telethon import types

        texts: list[Any] = [types.TextBold(text=types.TextPlain(text="Notice: "))]
        for segment, is_code in _notice_segments(notice):
            node = types.TextPlain(text=segment)
            texts.append(types.TextFixed(text=node) if is_code else node)
        return types.PageBlockParagraph(text=types.TextConcat(texts=texts))

    @staticmethod
    def _notice_html(notice: str) -> str:
        """Notice blockquote: `code` spans render as <code>."""
        rendered = "".join(
            f"<code>{escape(segment)}</code>" if is_code else escape(segment)
            for segment, is_code in _notice_segments(notice)
        )
        return f"<blockquote><b>Notice:</b> {rendered}</blockquote>"

    async def _edit(self, state: dict) -> None:
        text, markup = self._render(state)
        notice = str(state.get("notice", "") or "").strip()
        if notice:
            from telethon import types
            if isinstance(text, types.InputRichMessage):
                text.blocks.insert(_notice_insert_at(text.blocks), self._notice_block(notice))
            elif text.lstrip().startswith("<h"):
                notice_html = self._notice_html(notice)
                # The divider and the page indexation are locked together at
                # the bottom; hints/notices stay above the divider.
                footer_at = text.rfind("<p><i>Page ")
                if footer_at < 0:
                    footer_at = text.rfind("<p>Page ")
                if footer_at >= 0:
                    divider_at = text.rfind("<hr />", 0, footer_at)
                    insert_at = divider_at if divider_at >= 0 else footer_at
                elif state.get("view") == "providers" and not state.get("model_flow"):
                    insert_at = text.rfind("<hr />")
                else:
                    insert_at = -1
                if insert_at >= 0:
                    text = f"{text[:insert_at]}{notice_html}\n{text[insert_at:]}"
                else:
                    text = f"{text}\n{notice_html}"
            else:
                rich = input_rich_message(text, rich_format="blocks")
                if isinstance(rich, types.InputRichMessage):
                    text = rich
                    text.blocks.insert(_notice_insert_at(text.blocks), self._notice_block(notice))
                else:
                    notice_md = "".join(
                        f"`{segment}`" if is_code else segment
                        for segment, is_code in _notice_segments(notice)
                    )
                    text = f"{text}\n\n> **Notice:** {notice_md}"
        text, markup = self._prepare_native_controls(state, text, markup)
        await self.adapter._mtproto_rich_request(edit_request(
            self.adapter._peer_for(state["chat_id"]), int(state["message_id"]), text,
            reply_markup=markup,
        ))
        if notice and state.get("notice") == notice:
            state.pop("notice", None)

    def _rotate(self, state: dict) -> None:
        old_id = int(state["id"])
        self._states.pop(old_id, None)
        new_id = next(self._counter)
        state["id"] = new_id
        state["created_at"] = time.monotonic()
        self._states[new_id] = state

    async def _safe_delete_incoming(self, source: Any, event: Any = None) -> bool:
        message_id = getattr(source, "message_id", None)
        if not message_id:
            message_id = getattr(getattr(event, "message", None), "id", None)
        if not message_id:
            return False
        try:
            return bool(await self.adapter.delete_message(str(source.chat_id), str(message_id)))
        except Exception:
            return False

    @staticmethod
    def _incoming_message_id(source: Any, event: Any = None) -> str | None:
        message_id = getattr(source, "message_id", None)
        if not message_id:
            message_id = getattr(getattr(event, "message", None), "id", None)
        return str(message_id) if message_id else None

    def _track_custom_input_message(self, state: dict, source: Any, event: Any = None) -> None:
        message_id = self._incoming_message_id(source, event)
        if not message_id:
            return
        ids = state.setdefault("custom_input_message_ids", [])
        if message_id not in ids:
            ids.append(message_id)

    async def _cleanup_custom_input_messages(self, state: dict, chat_id: Any = None) -> None:
        """Delete every captured custom-endpoint input ID without retaining input values."""
        message_ids = list(dict.fromkeys(str(item) for item in state.pop("custom_input_message_ids", []) if item))
        target_chat = str(chat_id or state.get("chat_id") or "")
        delete = getattr(self.adapter, "delete_message", None)
        if not target_chat or not callable(delete):
            return
        for message_id in message_ids:
            try:
                await delete(target_chat, message_id)
            except Exception as exc:
                logger.debug("Telegram custom-input cleanup failed (%s)", type(exc).__name__)

    def _schedule_custom_input_cleanup(self, state: dict, chat_id: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(
                self._cleanup_custom_input_messages(state, chat_id),
            )
        except RuntimeError:
            return

    @staticmethod
    def _split_escaped_error(text: str, budget: int) -> list[str]:
        """Split arbitrary error text into HTML-safe pieces without cutting entities."""
        chunks: list[str] = []
        current: list[str] = []
        current_length = 0
        for char in text:
            token = escape(char)
            if current and current_length + len(token) > budget:
                chunks.append("".join(current))
                current = []
                current_length = 0
            current.append(token)
            current_length += len(token)
        if current or not chunks:
            chunks.append("".join(current))
        return chunks

    @staticmethod
    def _redact_provider_error(error: Any, api_key: str = "") -> str:
        text = str(error or "Provider returned an empty error")
        if api_key:
            text = text.replace(api_key, "[redacted API key]")
        try:
            from agent.redact import redact_sensitive_text
            text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
        except Exception:
            return "Provider error omitted because safe redaction was unavailable."
        if api_key and api_key in text:
            text = text.replace(api_key, "[redacted API key]")
        return "".join(
            char if char in "\n\r\t" or ord(char) >= 32 else "�"
            for char in text
        ) or "Provider returned an empty error"

    async def _send_full_provider_error(self, source: Any, error: Any, api_key: str = "") -> None:
        """Send the complete redacted diagnostic only through native MTProto RichMessages."""
        text = self._redact_provider_error(error, api_key)
        try:
            limit = min(
                int(getattr(self.adapter, "MAX_MESSAGE_LENGTH", 4096) or 4096),
                MAX_RICH_MESSAGE_CHARS,
            )
            budget = limit - 128
            if budget < 1:
                raise ValueError("Telegram RichMessage limit is too small")
            chunks = self._split_escaped_error(text, budget)
            peer = self.adapter._peer_for(source.chat_id)
            thread_id = int(source.thread_id) if str(getattr(source, "thread_id", "") or "").isdecimal() else None
            for index, chunk in enumerate(chunks, 1):
                body = (
                    f"<h2>Provider error</h2><pre>{chunk}</pre>"
                    f"<p><i>Part {index} of {len(chunks)}</i></p>"
                )
                await self.adapter._mtproto_rich_request(send_request(
                    peer, body, top_msg_id=thread_id, rich_format="html",
                ))
        except Exception as exc:
            # No ordinary-text retry: provider diagnostics remain rich-only and failures fail closed.
            logger.warning("Could not deliver Telegram provider RichMessage error (%s)", type(exc).__name__)

    async def _prompt_input(self, state: dict, source: Any, kind: str, prompt_view: str) -> None:
        provider = self._provider(state)
        if not provider or str(getattr(source, "chat_type", "")) != "dm":
            raise ValueError("Private provider input is unavailable")
        state["view"] = prompt_view
        self._pending_inputs[self._user_key(source)] = {
            "kind": kind, "state": state, "provider_slug": provider["slug"],
            "created_at": time.monotonic(),
        }
        await self._edit(state)

    def _insert_custom_provider(self, state: dict, data: dict, *, configured: bool) -> dict:
        slug = f"custom:{data['slug']}"
        row = next((p for p in state["providers"] if self._slug(p["slug"]) == slug), None)
        models = list(dict.fromkeys(str(model).strip()[:200] for model in data.get("models", [])
                                    if str(model).strip()))
        if row is None:
            row = self._safe_provider({
                "slug": slug, "name": data["name"], "auth_type": "api_key",
                "configured": configured, "enabled": True, "is_user_defined": True,
                "catalog_order": len(state["providers"]) + 1,
                "models": models,
                "total_models": len(models),
                "default_model": data.get("model") or "",
                "description": f"Custom endpoint at {data['base_url']}",
            })
            state["providers"].append(row)
        else:
            row["configured"] = bool(configured)
            row["models"] = models
            row["total_models"] = len(models)
            row["default_model"] = data.get("model") or row.get("default_model", "")
        return row

    @staticmethod
    def _custom_form_for_provider(provider: dict) -> dict:
        """Resolve a named custom row to its configured URL for key validation."""
        from hermes_cli.config import get_compatible_custom_providers
        from hermes_cli.providers import custom_provider_slug

        wanted = str(provider.get("slug") or "").strip().casefold()
        entries = get_compatible_custom_providers()
        entry = next((item for item in entries
                      if custom_provider_slug(str(item.get("name") or ""),
                                              str(item.get("provider_key") or "")).casefold() == wanted), None)
        if entry is None:
            raise ValueError("Custom endpoint configuration is unavailable")
        name = str(entry.get("name") or "").strip()
        slug = custom_provider_slug(name, str(entry.get("provider_key") or "")).removeprefix("custom:")
        base_url = str(entry.get("base_url") or "").strip()
        if not name or not slug or not base_url:
            raise ValueError("Custom endpoint configuration is incomplete")
        return {"name": name, "slug": slug, "base_url": base_url, "model": ""}

    @staticmethod
    def _fetch_custom_model_ids(data: dict, api_key: str = "") -> list[str]:
        """Validate a custom endpoint by fetching its authenticated live /models catalog."""
        from providers import get_provider_profile

        profile = get_provider_profile("custom")
        if profile is None:
            raise ValueError("Custom endpoint model discovery is unavailable")
        models = profile.fetch_models(
            api_key=api_key or None, base_url=str(data["base_url"]), timeout=8.0,
        )
        model_ids = list(dict.fromkeys(
            str(model).strip()[:200] for model in (models or [])
            if isinstance(model, str) and model.strip()
        ))
        if not model_ids:
            raise ValueError("The endpoint did not accept the key or return a live model list")
        return model_ids

    async def _save_custom(self, state: dict, *, api_key: str = "", on_key_saved=None) -> None:
        data = dict(state.get("custom_form") or {})
        if not all(data.get(field) for field in ("name", "slug", "base_url")):
            raise ValueError("Custom endpoint details are incomplete")
        from hermes_cli.config import get_compatible_custom_providers
        from hermes_cli.providers import custom_provider_slug
        target_url = data["base_url"].rstrip("/").casefold()
        existing = next((entry for entry in get_compatible_custom_providers()
                         if str(entry.get("base_url") or "").rstrip("/").casefold() == target_url), None)
        if existing:
            data["name"] = str(existing.get("name") or data["name"])
            data["slug"] = custom_provider_slug(
                data["name"], str(existing.get("provider_key") or ""),
            ).removeprefix("custom:")
        models = await asyncio.to_thread(self._fetch_custom_model_ids, data, api_key)
        from hermes_cli.main_provider_setup import _save_custom_provider
        await asyncio.to_thread(
            _save_custom_provider, data["base_url"], "", "",
            None, data["name"], None,
        )
        if api_key:
            from hermes_cli.auth_commands import add_api_key_credential_for_ui
            await asyncio.to_thread(
                add_api_key_credential_for_ui, f"custom:{data['slug']}", api_key,
                added_by=state.get("user_id"),
            )
            state["_custom_key_saved"] = True
            if on_key_saved is not None:
                await on_key_saved()
        from hermes_cli.model_switch_providers import _save_discovered_models_to_config
        await asyncio.to_thread(_save_discovered_models_to_config, data["base_url"], models)
        from hermes_cli.models import cached_fetch_api_models
        try:
            await asyncio.to_thread(
                cached_fetch_api_models, api_key or "", data["base_url"],
                timeout=8.0, fetch_models=lambda: models,
            )
        except Exception as exc:
            logger.debug("Could not warm custom model cache (%s)", type(exc).__name__)
        data["models"] = models
        provider = self._insert_custom_provider(state, data, configured=True)
        self._refresh_provider_credentials(state, provider)
        history = state.setdefault("history", [])
        while history and history[-1].get("view") == "custom-form":
            history.pop()
        state.update(
            view="models", selected_provider=provider["slug"], custom_form={},
            search_query="", page=0,
            notice=f"Loaded {len(models)} live model(s). Select one to use this endpoint.",
        )
        await self._edit(state)

    async def _start_oauth(self, state: dict, source: Any) -> None:
        provider = self._provider(state)
        if provider is None:
            raise ValueError("Provider unavailable")
        slug = provider["slug"]
        job_key = (*self._user_key(source), slug)
        existing = self._oauth_jobs.get(job_key)
        if existing is not None and not existing.done():
            state.update(view="oauth", oauth_status="An authorization flow is already active for this provider.")
            await self._edit(state)
            return
        if slug == "qwen-oauth":
            state.update(view="oauth", oauth_status="Importing an existing Qwen Code login from this host…",
                         oauth_url="", oauth_code="", oauth_needs_code=False)
            await self._edit(state)
            async def _import_existing():
                try:
                    from hermes_cli.auth_commands import auth_add_command
                    with self._profile_scope(state):
                        await asyncio.to_thread(auth_add_command, SimpleNamespace(
                            provider=slug, auth_type="oauth", no_browser=True, quiet=True,
                            added_by=source.user_id,
                        ))
                    provider["configured"] = True
                    self._refresh_provider_credentials(state, provider)
                    state.pop("provider_validation", None)
                    state.update(view="provider", oauth_status="Qwen login imported.")
                except BaseException as exc:
                    logger.warning("Qwen login import failed (%s)", type(exc).__name__)
                    state.update(view="keys", oauth_status="", oauth_url="", oauth_code="")
                    state["notice"] = "No importable Qwen Code login was found on this host, or its token could not be read."
                try:
                    await self._edit(state)
                except Exception:
                    pass
            task = asyncio.create_task(_import_existing())
            self._oauth_jobs[job_key] = task
            return
        if slug not in _OAUTH_UI_PROVIDERS:
            raise ValueError("This provider has no Hermes OAuth add route")
        state.update(view="oauth", oauth_status="Starting Hermes sign-in…", oauth_url="",
                     oauth_code="", oauth_needs_code=False)
        await self._edit(state)
        loop = asyncio.get_running_loop()
        verification_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        input_event = threading.Event()
        input_value: dict[str, str] = {}

        def _queue_verification(url: str, code: str = "") -> None:
            loop.call_soon_threadsafe(verification_queue.put_nowait, (str(url), str(code or "")))

        def _input_fn(_prompt: str) -> str:
            if not input_event.wait(_INPUT_TTL_SECONDS):
                raise EOFError("Provider authorization input expired")
            value = input_value.pop("value", "")
            input_event.clear()
            return value

        args = SimpleNamespace(
            provider=slug,
            auth_type="oauth",
            timeout=300.0,
            no_browser=True,
            quiet=True,
            added_by=source.user_id,
            on_verification=_queue_verification,
            on_authorize_url=lambda url: _queue_verification(url, ""),
            input_fn=_input_fn,
        )
        pending_key = self._user_key(source)

        async def _watch_verification() -> None:
            url, code = await verification_queue.get()
            state.update(view="oauth", oauth_url=url, oauth_code=code,
                         oauth_needs_code=not bool(code),
                         oauth_status="Waiting for browser/device approval" if code else "Waiting for authorization code")
            if not code:
                self._pending_inputs[pending_key] = {
                    "kind": "oauth-code", "state": state, "input_event": input_event,
                    "input_value": input_value, "created_at": time.monotonic(),
                }
            try:
                await self._edit(state)
            except Exception:
                logger.warning("Could not update provider OAuth card")

        async def _finish() -> None:
            watcher = asyncio.create_task(_watch_verification())
            try:
                from hermes_cli.auth_commands import auth_add_command
                with self._profile_scope(state):
                    await asyncio.to_thread(auth_add_command, args)
                await asyncio.sleep(0)
                if not watcher.done():
                    watcher.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher
                provider["configured"] = True
                self._refresh_provider_credentials(state, provider)
                state.pop("provider_validation", None)
                state.update(view="provider", oauth_status="Login complete", oauth_url="",
                             oauth_code="", oauth_needs_code=False)
                await self._edit(state)
            except BaseException as exc:
                logger.warning("Provider OAuth failed (%s)", type(exc).__name__)
                self._pending_inputs.pop(pending_key, None)
                state.update(view="keys", oauth_url="", oauth_code="", oauth_needs_code=False)
                state["notice"] = f"Sign-in failed safely ({type(exc).__name__}). Check the provider account/network and retry."
                try:
                    await self._edit(state)
                except Exception:
                    pass
            finally:
                if self._pending_inputs.get(pending_key, {}).get("state") is state:
                    self._pending_inputs.pop(pending_key, None)
                self._oauth_jobs.pop(job_key, None)

        task = asyncio.create_task(_finish())
        self._oauth_jobs[job_key] = task

    @staticmethod
    def _probe_live_catalog(provider: dict) -> dict[str, Any]:
        """Live GET of the provider's model-list endpoint: the real outcome.

        The production fetchers swallow the HTTP status (``fetch_models``
        returns a bare list or None), so the refresh notice cannot learn it
        from them. This probes the same endpoint the profile's
        ``fetch_models`` resolves (``models_url``, else ``{base_url}/models``)
        and records the status itself. Never raises: every failure mode is
        an outcome dict, and a status is only reported when the endpoint
        actually answered with one.
        """
        from providers import get_provider_profile

        slug = TelegramProviderManagementUI._slug(provider.get("slug"))
        profile = get_provider_profile(slug) if slug else None
        if profile is None or not getattr(profile, "supports_model_listing", False):
            return {"attempted": False, "reason": "no_endpoint"}
        if getattr(profile, "auth_type", "") == "external_process":
            # No HTTP endpoint: the profile's own subprocess fetch is the
            # live outcome, and there is no status code to report.
            try:
                models = profile.fetch_models()
            except Exception as exc:
                return {"attempted": True, "status": None, "count": None, "error": type(exc).__name__}
            if not models:
                return {"attempted": True, "status": None, "count": None, "error": "empty"}
            return {"attempted": True, "status": None, "count": len(models), "error": None}
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime = resolve_runtime_provider(requested=slug)
            api_key = runtime.get("api_key")
            base_url = str(runtime.get("base_url") or getattr(profile, "base_url", "") or "").strip()
        except Exception as exc:
            return {"attempted": True, "status": None, "count": None, "error": type(exc).__name__}
        # Same endpoint resolution as ProviderProfile.fetch_models: a
        # user-configured base URL (a relay) is terminal — the probe hits
        # it, never the vendor host behind it.
        profile_base = str(getattr(profile, "base_url", "") or "")
        custom_base = bool(base_url) and base_url.rstrip("/") != profile_base.rstrip("/")
        if custom_base:
            url = base_url.rstrip("/") + "/models"
        else:
            url = str(getattr(profile, "models_url", "") or "").strip()
            if not url:
                effective_base = base_url or profile_base
                if not effective_base.strip():
                    return {"attempted": False, "reason": "no_endpoint"}
                url = effective_base.rstrip("/") + "/models"
        import json
        from urllib.error import HTTPError
        from urllib.request import Request

        from hermes_cli.chat_catalog import chat_catalog_ids
        from hermes_cli.urllib_security import open_credentialed_url

        request = Request(url)
        if isinstance(api_key, str) and api_key.strip():
            request.add_header("Authorization", f"Bearer {api_key}")
        request.add_header("Accept", "application/json")
        try:
            with open_credentialed_url(request, timeout=8.0) as response:
                status = int(response.getcode())
                payload = json.loads(response.read().decode())
        except HTTPError as exc:
            return {"attempted": True, "status": int(exc.code), "count": None, "error": type(exc).__name__}
        except Exception as exc:
            return {"attempted": True, "status": None, "count": None, "error": type(exc).__name__}
        items = payload if isinstance(payload, list) else payload.get("data", [])
        models = chat_catalog_ids(items)
        if not models:
            return {"attempted": True, "status": status, "count": 0, "error": "empty"}
        return {"attempted": True, "status": status, "count": len(models), "error": None}

    @staticmethod
    def _fetch_live_search_catalog(provider: dict) -> tuple[list[str], str]:
        """Live model ids for a provider search, plus a status for the notice.

        The live pool is the provider's own ``fetch_models`` listing — the raw
        ``/models`` endpoint ``/model`` validation consults — not the picker
        resolver, whose curated fetchers cap the big aggregators (OpenRouter
        lists 74 of its 447 live models). A profile-owned catalog (Codex,
        Ollama, external-process CLIs) is reached through the picker resolver
        when there is no plain listing to fail. Runs off-loop (blocking
        network, each fetch bounded by its own timeout). Never raises: every
        failure is a status, and the caller keeps the static list.
        """
        if not provider.get("ready"):
            return [], "no_credentials"
        slug = str(provider.get("slug") or "")
        try:
            from providers import get_provider_profile
            profile = get_provider_profile(slug)
        except Exception:
            logger.debug("Live model search profile lookup failed for %s", slug, exc_info=True)
            profile = None
        live = None
        if profile is not None and getattr(profile, "supports_model_listing", False):
            try:
                if getattr(profile, "auth_type", "") == "external_process":
                    # No HTTP endpoint: the profile's own subprocess fetch is
                    # the live outcome (kwargs are ignored per its contract).
                    live = profile.fetch_models()
                else:
                    from hermes_cli.runtime_provider import resolve_runtime_provider

                    runtime = resolve_runtime_provider(requested=slug)
                    # A user-configured base URL is terminal — the runtime
                    # resolver already hands back the relay, never the vendor
                    # host behind it.
                    live = profile.fetch_models(
                        api_key=runtime.get("api_key"),
                        base_url=str(runtime.get("base_url") or getattr(profile, "base_url", "") or "").strip() or None,
                        timeout=8.0,
                    )
            except Exception:
                logger.debug("Live model search fetch failed for %s", slug, exc_info=True)
                live = None
        if live:
            from hermes_cli.chat_catalog import without_generation_models

            ids = [str(model) for model in without_generation_models(
                [str(model).strip() for model in live if str(model).strip()])]
            if ids:
                return ids, "live"
        # No plain listing: classify the failure, and let a profile-owned
        # catalog answer (Codex, Ollama, external-process CLIs) — but a
        # resolver result that merely repeats the curated list must never be
        # mislabelled as the live pool when the endpoint itself failed.
        probe = TelegramProviderManagementUI._probe_live_catalog(provider)
        try:
            from hermes_cli.models import CuratedFallbackModels, provider_model_ids

            resolved = provider_model_ids(slug, force_refresh=True)
        except Exception:
            logger.debug("Live model search resolver failed for %s", slug, exc_info=True)
            resolved = None
        resolved_ids = (
            [str(model) for model in resolved if str(model).strip()]
            if resolved and not isinstance(resolved, CuratedFallbackModels) else []
        )
        if not probe.get("attempted"):
            # No HTTP listing exists: the resolver is the only live source.
            return (resolved_ids, "live") if resolved_ids else ([], "no_endpoint")
        # The endpoint failed: the resolver only counts as live when it proves
        # liveness with ids this card does not already list (Codex
        # early-access ids, Ollama Cloud models). A result that merely repeats
        # the static list is the curated fallback wearing a live label.
        static_ids = {str(model) for model in (provider.get("models") or []) if str(model).strip()}
        if resolved_ids and set(resolved_ids) - static_ids:
            return resolved_ids, "live"
        return [], "unreachable"

    async def _refresh_live_catalog(self, state: dict, source: Any) -> None:
        runner = getattr(self.adapter, "gateway_runner", None)
        snapshot = getattr(runner, "_provider_inventory_snapshot", None)
        if not callable(snapshot):
            raise RuntimeError("Provider catalog refresh is unavailable")
        providers, current_model, current_provider, session_key, profile_home = await snapshot(source)
        previous_slug = state.get("selected_provider")
        state["providers"] = [self._safe_provider(row) for row in providers]
        state["providers"].sort(key=lambda row: (
            not row["is_current"], not row["configured"], row["catalog_order"], row["name"].casefold()))
        state["current_model"] = str(current_model or "")
        state["current_provider"] = str(current_provider or "")
        state["session_key"] = str(session_key or "")
        state["profile_home"] = str(profile_home) if profile_home else state.get("profile_home", "")
        if previous_slug and not any(self._slug(row["slug"]) == self._slug(previous_slug) for row in state["providers"]):
            state["selected_provider"] = None
        for row in state["providers"]:
            self._refresh_provider_credentials(state, row)
        selected_slug = self._slug(state.get("selected_provider") or state.get("current_provider"))
        selected = next(
            (row for row in state["providers"] if self._slug(row["slug"]) == selected_slug),
            None,
        )
        name = (selected.get("name") or selected["slug"]) if selected is not None else selected_slug
        if selected is not None and not selected.get("ready"):
            # The refresh is cache-first: no live fetch is ever attempted for
            # a provider with no credentials, so say so instead of reporting
            # success while its list stays empty.
            notice = (
                f"Live catalog refresh not attempted — {name} has no credentials, "
                "so there is no live model list to refresh."
            )
        elif selected_slug == "openai-codex":
            # A failed Codex list used to fail silently (stale models, no error
            # anywhere). Surface the recorded fetch failure on the refresh notice.
            try:
                from hermes_cli.codex_models import get_codex_model_list_error

                codex_error = get_codex_model_list_error()
            except Exception:
                codex_error = None
            if codex_error:
                notice = (
                    f"Live catalog refresh failed — Codex model list failed ({codex_error}); "
                    "showing cached models."
                )
            else:
                notice = "Live catalog refresh not attempted — showing cached models for OpenAI Codex."
        elif selected is None:
            notice = "Live catalog refresh not attempted — no provider selected; showing cached models."
        else:
            # The snapshot above is cache-first, so the notice used to claim
            # a live refresh nobody performed. Probe the selected provider's
            # model-list endpoint for real and report what it answered.
            outcome = await asyncio.to_thread(self._probe_live_catalog, selected)
            status = outcome.get("status")
            if not outcome.get("attempted"):
                notice = (
                    f"Live catalog refresh not attempted — {name} has no model-list endpoint; "
                    "showing cached models."
                )
            elif outcome.get("error") == "empty" and status is not None:
                notice = (
                    f"Live catalog refresh failed — HTTP `{status}` but no models for {name}; "
                    "showing cached models."
                )
            elif outcome.get("error") == "empty":
                notice = (
                    f"Live catalog refresh failed — no models returned for {name}; "
                    "showing cached models."
                )
            elif outcome.get("error") and status is not None:
                notice = (
                    f"Live catalog refresh failed — HTTP `{status}` for {name}; "
                    "showing cached models."
                )
            elif outcome.get("error"):
                notice = (
                    f"Live catalog refresh failed — {outcome['error']} for {name}; "
                    "showing cached models."
                )
            elif status is not None:
                selected["catalog_refreshed"] = True
                notice = (
                    f"Live catalog refresh worked — HTTP `{status}`, "
                    f"{outcome.get('count')} models for {name}."
                )
            else:
                selected["catalog_refreshed"] = True
                notice = f"Live catalog refresh worked — {outcome.get('count')} models for {name}."
        state["notice"] = notice

    @staticmethod
    def _validate_provider_endpoint_sync(provider: dict) -> str:
        """Check a configured provider's model endpoint without sending an inference request."""
        if not provider.get("ready"):
            return "Not checked: no local credentials are configured."
        try:
            from providers import get_provider_profile
            profile = get_provider_profile(str(provider.get("slug") or ""))
            if profile is None or not profile.supports_model_listing:
                return "Not checked: this provider has no model-list endpoint."

            from hermes_cli.runtime_provider import resolve_runtime_provider
            runtime = resolve_runtime_provider(requested=str(provider.get("slug") or ""))
            api_key = runtime.get("api_key")
            if not isinstance(api_key, str) or not api_key.strip():
                return "Not checked: no API credential is available for the endpoint check."
            base_url = str(runtime.get("base_url") or profile.base_url or "").strip()
            if str(provider.get("slug") or "") == "openrouter":
                from urllib.error import HTTPError
                from urllib.request import Request
                from hermes_cli.urllib_security import open_credentialed_url

                endpoint = base_url.rstrip("/") + "/key"
                request = Request(endpoint)
                request.add_header("Authorization", f"Bearer {api_key}")
                request.add_header("Accept", "application/json")
                try:
                    with open_credentialed_url(request, timeout=8.0) as response:
                        status = int(response.getcode())
                except HTTPError as exc:
                    status = int(exc.code)
                if status == 200:
                    return "Key check accepted (HTTP 200)."
                if status in {401, 403}:
                    return f"Key check rejected (HTTP {status})."
                return f"Key check returned HTTP {status}."

            models = profile.fetch_models(
                api_key=api_key,
                base_url=base_url,
                timeout=8.0,
            )
        except Exception as exc:
            # Do not surface provider response bodies, URLs, headers, or credentials in the UI.
            return f"Endpoint check failed ({type(exc).__name__})."
        if models is None:
            return "Endpoint check failed: no model list was returned."
        if not models:
            return "Endpoint check failed: the model list was empty."
        return f"Endpoint response: {len(models)} model(s) returned."

    async def _select_model(self, state: dict, source: Any, model: str, provider_slug: str) -> str:
        """Run the same provider/model resolver and durable commit used by Hermes' model picker.

        The pick routes the acting user's own turns: the owner pins it as the
        profile-wide default, an allowed user gets a session-only override
        (their next turns resolve it, the profile default is untouched). A
        provider another user added and did not share is refused here, not
        left to fail at turn time.
        """
        from gateway.provider_sharing import provider_visible_to_user
        from gateway.run import _hermes_home
        from gateway.slash_commands_model import _ModelSwitchContext
        runner = getattr(self.adapter, "gateway_runner", None)
        perform = getattr(runner, "_perform_model_switch", None)
        commit = getattr(runner, "_commit_model_switch", None)
        if not callable(perform) or not callable(commit):
            raise RuntimeError("Model switch route is unavailable")
        if not provider_visible_to_user(provider_slug, state.get("user_id")):
            raise RuntimeError(
                f"Provider {provider_slug} was added by another user and is not shared with you.")
        is_owner = self._is_owner_actor(source, state.get("profile_home"))
        profile_home = Path(state["profile_home"]) if state.get("profile_home") else _hermes_home
        normalized_source = await asyncio.to_thread(runner._normalize_source_for_session_key, source)
        ctx = _ModelSwitchContext(
            session_key=state["session_key"], source=normalized_source,
            config_path=profile_home / "config.yaml", persist_global=is_owner,
        )
        await asyncio.to_thread(ctx.read_config)
        ctx.apply_override((getattr(runner, "_session_model_overrides", {}) or {}).get(state["session_key"], {}))
        result, error = await perform(ctx, model, provider_slug, source)
        if error is not None:
            # The switch already explains itself (provider + remedy, no secrets —
            # the typed /model path shows this same text as its reply), so carry
            # it: a fixed string here is what made every non-Nous failure render
            # as the opaque "failed safely (RuntimeError)".
            raise RuntimeError(error)
        if result is None or not bool(getattr(result, "success", True)):
            raise RuntimeError("Model route could not be updated safely.")
        confirmation = await commit(result, ctx, source=source, picker=True)
        state["current_model"] = str(result.new_model or model)
        state["current_provider"] = str(result.target_provider or provider_slug)
        for row in state["providers"]:
            row["is_current"] = self._slug(row["slug"]) == self._slug(state["current_provider"])
            if is_owner:
                row["is_profile_default"] = row["is_current"]
                row["profile_default_model"] = state["current_model"] if row["is_current"] else ""
            if row["is_current"]:
                row["default_model"] = state["current_model"]
        return str(confirmation or "Model route updated.")

    def _auto_fallback_enabled(self, state: dict) -> bool:
        """Profile-wide ``fallback.auto_fallback``; True when unset or unreadable.

        Reads the profile's raw config.yaml (no merged-config dependency, so
        the card renders under any scope); the rescue itself reads the merged
        config, which carries the same default.
        """
        home = str(state.get("profile_home") or "").strip()
        if not home:
            return True
        try:
            from hermes_cli.config import read_user_config_raw

            config = read_user_config_raw(Path(home) / "config.yaml")
            fallback = config.get("fallback") if isinstance(config, dict) else None
            if not isinstance(fallback, dict):
                return True
            return bool(fallback.get("auto_fallback", True))
        except Exception:
            return True

    def _set_auto_fallback(self, state: dict, enabled: bool) -> None:
        """Persist ``fallback.auto_fallback`` using Hermes' fail-closed atomic config writer."""
        home = Path(state["profile_home"]) if state.get("profile_home") else None
        if home is None:
            from gateway.run import _hermes_home
            home = _hermes_home
        from hermes_cli.config import atomic_config_write, require_readable_config_before_write
        path = home / "config.yaml"
        cfg = require_readable_config_before_write(path)
        fallback = cfg.get("fallback")
        if not isinstance(fallback, dict):
            fallback = {}
            cfg["fallback"] = fallback
        fallback["auto_fallback"] = bool(enabled)
        atomic_config_write(path, cfg)

    def _save_reasoning_default(self, state: dict, model: str, level: Optional[str]) -> None:
        """Persist a per-model override using Hermes' fail-closed atomic config writer."""
        home = Path(state["profile_home"]) if state.get("profile_home") else None
        if home is None:
            from gateway.run import _hermes_home
            home = _hermes_home
        from hermes_cli.config import atomic_config_write, require_readable_config_before_write
        path = home / "config.yaml"
        cfg = require_readable_config_before_write(path)
        agent = cfg.get("agent")
        if not isinstance(agent, dict):
            agent = {}
            cfg["agent"] = agent
        overrides = agent.get("reasoning_overrides")
        if not isinstance(overrides, dict):
            overrides = {}
            agent["reasoning_overrides"] = overrides
        if level is None:
            overrides.pop(model, None)
            if not overrides:
                agent.pop("reasoning_overrides", None)
        else:
            overrides[model] = False if level == "none" else level
        atomic_config_write(path, cfg)

    async def _pin_current_route_as_global_default(self, state: dict, source: Any) -> str:
        """Commit the active model/provider pair through Hermes' global model-switch path."""
        model = str(state.get("current_model") or "").strip()
        provider = str(state.get("current_provider") or "").strip()
        if not model or not provider:
            raise ValueError("A current model and provider are required")
        confirmation = await self._select_model(state, source, model, provider)
        return (
            f"Profile-wide default saved as {provider} / {model}. {confirmation}\n\n"
            "This action targets the profile-wide default; explicit per-session overrides remain in effect."
        )

    async def _clear_removed_provider_route(self, state: dict, source: Any, slug: str) -> str:
        """Stop a credential-less provider from remaining the route after removal.

        Clears the per-session model override for this session when it points at
        the removed provider, and moves the profile-wide default to the first
        other provider that still has credentials. Best-effort: when no working
        provider is left, the route stays dangling and the turn-time fallback
        (one-time, notified) takes over instead of failing silently.
        """
        removed = self._slug(slug)
        if not removed:
            return ""
        runner = getattr(self.adapter, "gateway_runner", None)
        session_key = str(state.get("session_key") or "")
        cleared_session = False
        if runner is not None and session_key:
            try:
                overrides = getattr(runner, "_session_model_overrides", None)
                if isinstance(overrides, dict):
                    current = overrides.get(session_key) or {}
                    if self._slug(current.get("provider")) == removed:
                        overrides.pop(session_key, None)
                        store = getattr(runner, "async_session_store", None)
                        if store is not None:
                            await store.set_model_override(session_key, None)
                        evict = getattr(runner, "_evict_cached_agent", None)
                        if callable(evict):
                            evict(session_key)
                        cleared_session = True
            except Exception:
                logger.debug("Could not clear removed-provider session override", exc_info=True)
        # The profile default lives in config.yaml model.provider; move it only
        # when it still points at the removed provider and a working one exists.
        profile_home = Path(state["profile_home"]) if state.get("profile_home") else None
        default_points_at_removed = False
        if profile_home is not None:
            try:
                from hermes_cli.config import require_readable_config_before_write
                cfg = require_readable_config_before_write(profile_home / "config.yaml")
                model_cfg = cfg.get("model") or {}
                default_points_at_removed = (
                    isinstance(model_cfg, dict)
                    and self._slug(model_cfg.get("provider")) == removed
                )
            except Exception:
                default_points_at_removed = False
        moved_to = ""
        if default_points_at_removed:
            for row in state.get("providers") or []:
                cand_slug = self._slug(row.get("slug"))
                if not cand_slug or cand_slug == removed or not bool(row.get("enabled", True)):
                    continue
                try:
                    with self._profile_scope(state):
                        ready = bool(self._load_credentials(row)) or self._has_local_auth_material(row)
                except Exception:
                    ready = False
                if not ready:
                    continue
                models = row.get("models") or []
                model = str(row.get("default_model") or (models[0] if models else "") or "").strip()
                if not model:
                    continue
                try:
                    await self._select_model(state, source, model, cand_slug)
                except Exception:
                    continue
                moved_to = f"{cand_slug} / {model}"
                break
        parts: list[str] = []
        if cleared_session:
            parts.append("This session no longer points at it.")
        if moved_to:
            parts.append(f"Profile default moved to {moved_to}.")
        elif default_points_at_removed:
            parts.append("No working provider is left to move the profile default to; add one with /model.")
        return " ".join(parts)

    async def _handle_search_input(self, state: dict, source: Any, value: str) -> bool:
        """Consume a submitted search term: fetch the provider's live pool off-loop,
        list the matches, or keep the static list with a notice when the live
        fetch fails. A provider-list search (no provider selected) still filters
        the static provider list."""
        state["search_query"] = value[:80]
        state["page"] = 0
        provider = self._provider(state)
        if provider is None:
            await self._edit(state)
            return True
        state["notice"] = "Searching the live pool…"
        await self._edit(state)
        live_models, status = await asyncio.to_thread(
            self._fetch_live_search_catalog, provider,
        )
        if status == "live":
            state["live_search_models"] = live_models
            state["live_search_provider"] = provider["slug"]
        else:
            state.pop("live_search_models", None)
            state.pop("live_search_provider", None)
            if status == "no_credentials":
                state["notice"] = (
                    f"Live search needs credentials for {provider['name']} — "
                    "showing the curated list instead. Add a credential, then search again."
                )
            elif status == "no_endpoint":
                state["notice"] = (
                    f"{provider['name']} has no live model list — "
                    "showing the curated list instead."
                )
            else:
                state["notice"] = (
                    f"Live pool unreachable for {provider['name']} — "
                    "showing the curated list instead. Try Refresh, then search again."
                )
        await self._edit(state)
        return True

    async def _handle_pending_input(self, event: Any, source: Any, text: str) -> bool:
        if str(getattr(source, "chat_type", "")) != "dm" or not source.user_id:
            return False
        pending = self._pending_inputs.get(self._user_key(source))
        if pending is None:
            return False
        kind = pending.get("kind")
        state = pending.get("state")
        is_custom_input = str(kind or "").startswith("custom-")
        if is_custom_input and isinstance(state, dict):
            self._track_custom_input_message(state, source, event)
        if time.monotonic() - pending.get("created_at", 0) > _INPUT_TTL_SECONDS:
            self._pending_inputs.pop(self._user_key(source), None)
            if is_custom_input and isinstance(state, dict):
                await self._cleanup_custom_input_messages(state, source.chat_id)
            else:
                await self._safe_delete_incoming(source, event)
            if state:
                if self._go_back(state):
                    state["notice"] = "This private input expired; start the action again."
                else:
                    state.update(view="provider", notice="This private input expired; start the action again.")
                with self._profile_scope(state):
                    await self._edit(state)
            return True
        value = str(text or "").strip()
        if value.casefold() in {"/cancel", "cancel", "cancel setup"}:
            self._pending_inputs.pop(self._user_key(source), None)
            event_obj = pending.get("input_event")
            if event_obj is not None:
                pending.setdefault("input_value", {})["value"] = ""
                event_obj.set()
            if is_custom_input and isinstance(state, dict):
                await self._cleanup_custom_input_messages(state, source.chat_id)
            else:
                await self._safe_delete_incoming(source, event)
            if state:
                if self._go_back(state):
                    state["notice"] = "Input cancelled; no change was made."
                else:
                    state.update(view="provider", notice="Input cancelled; no change was made.")
                with self._profile_scope(state):
                    await self._edit(state)
            return True

        if state is None:
            self._pending_inputs.pop(self._user_key(source), None)
            await self._safe_delete_incoming(source, event)
            return True
        # Credential inputs stay owner-only; search terms stay open to all.
        if (str(kind or "") in {"api-key", "custom-name", "custom-url", "custom-key", "oauth-code"}
                and not self._is_owner_actor(source, state.get("profile_home"))):
            self._pending_inputs.pop(self._user_key(source), None)
            if is_custom_input:
                await self._cleanup_custom_input_messages(state, source.chat_id)
            else:
                await self._safe_delete_incoming(source, event)
            state.update(
                view="provider" if self._provider(state) else "providers",
                notice="Only the profile owner can add credentials.",
            )
            with self._profile_scope(state):
                await self._edit(state)
            return True
        upstream_secret = value if kind == "custom-key" and value.casefold() != "skip" else ""
        with self._profile_scope(state):
            try:
                if kind == "search":
                    self._pending_inputs.pop(self._user_key(source), None)
                    return await self._handle_search_input(state, source, value)
                if kind == "custom-name":
                    if not value or len(value) > 80:
                        raise ValueError("Use a custom endpoint name up to 80 characters")
                    from hermes_cli.providers import custom_provider_slug
                    slug = custom_provider_slug(value).removeprefix("custom:")
                    if not slug:
                        raise ValueError("The name needs at least one letter or number")
                    data = dict(state.get("custom_form") or {})
                    data.update(name=value, slug=slug)
                    state["custom_form"] = data
                    state["custom_step"] = "url"
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "custom-url", "state": state, "created_at": time.monotonic(),
                    }
                    await self._edit(state)
                    return True
                if kind == "custom-url":
                    parsed = urlsplit(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                        raise ValueError("Enter a full http(s) endpoint URL without embedded credentials, query, or fragment")
                    data = dict(state.get("custom_form") or {})
                    data["base_url"] = value.rstrip("/")
                    state["custom_form"] = data
                    state["custom_step"] = "key"
                    state.update(view="collect-custom-key", notice="")
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "custom-key", "state": state, "created_at": time.monotonic(),
                    }
                    await self._edit(state)
                    return True
                if kind == "api-key":
                    self._pending_inputs.pop(self._user_key(source), None)
                    if not value or len(value) > 8192:
                        raise ValueError("The key was empty or too large")
                    from hermes_cli.auth_commands import add_api_key_credential_for_ui
                    added_metadata = await asyncio.to_thread(
                        add_api_key_credential_for_ui, pending["provider_slug"], value,
                        added_by=source.user_id,
                    )
                    await self._safe_delete_incoming(source, event)
                    value = ""
                    provider = self._provider(state)
                    if provider:
                        provider["configured"] = True
                        self._refresh_provider_credentials(state, provider)
                        if isinstance(added_metadata, dict) and added_metadata.get("id"):
                            provider["ready"] = True
                    state.pop("provider_validation", None)
                    state.update(view="credentials", notice="Credential added to Hermes' secure auth store; the secret was not displayed.")
                    await self._edit(state)
                    return True
                if kind == "custom-key":
                    self._pending_inputs.pop(self._user_key(source), None)
                    if value.casefold() == "skip":
                        await self._save_custom(state)
                        value = ""
                        await self._cleanup_custom_input_messages(state, source.chat_id)
                        return True
                    if not value or len(value) > 8192:
                        raise ValueError("The key was empty or too large")
                    cleanup_attempted = False

                    async def clean_saved_input():
                        nonlocal cleanup_attempted
                        cleanup_attempted = True
                        await self._cleanup_custom_input_messages(state, source.chat_id)

                    try:
                        await self._save_custom(state, api_key=value, on_key_saved=clean_saved_input)
                    finally:
                        if state.pop("_custom_key_saved", False) and not cleanup_attempted:
                            await clean_saved_input()
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                    value = ""
                    return True
                if kind == "oauth-code":
                    if not value or len(value) > 4096:
                        raise ValueError("Authorization code was empty or too large")
                    input_value = pending.get("input_value")
                    input_event = pending.get("input_event")
                    if not isinstance(input_value, dict) or input_event is None:
                        raise ValueError("OAuth code input expired")
                    self._pending_inputs.pop(self._user_key(source), None)
                    input_value["value"] = value
                    value = ""
                    input_event.set()
                    await self._safe_delete_incoming(source, event)
                    state.update(view="oauth", oauth_status="Authorization code received; exchanging securely…",
                                 oauth_needs_code=False)
                    await self._edit(state)
                    return True
                raise ValueError("No matching private input action")
            except BaseException as exc:
                self._pending_inputs.pop(self._user_key(source), None)
                value = ""
                logger.warning("Provider private input failed (%s)", type(exc).__name__)
                if kind == "custom-key":
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "custom-key", "state": state, "created_at": time.monotonic(),
                    }
                    state.update(
                        view="collect-custom-key",
                        notice="Endpoint validation failed; check the URL/key and try again.",
                    )
                else:
                    if self._go_back(state):
                        state["notice"] = f"The action failed safely ({type(exc).__name__}); no secret was shown."
                    else:
                        state.update(view="provider", notice=f"The action failed safely ({type(exc).__name__}); no secret was shown.")
                try:
                    await self._edit(state)
                except Exception:
                    pass
                if kind == "custom-key":
                    await self._send_full_provider_error(source, exc, upstream_secret)
                elif is_custom_input:
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                return True

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        """Consume pending private API keys, OAuth codes, search terms, or custom endpoint fields."""
        self._prune()
        return await self._handle_pending_input(event, source, text)

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not data.startswith("pv:"):
            return False
        self._prune()
        parts = data.split(":", 3)
        if len(parts) < 3:
            await event.answer("This provider action is unavailable.", alert=True)
            return True
        try:
            state_id = int(parts[1])
        except ValueError:
            await event.answer("This provider action is unavailable.", alert=True)
            return True
        state = self._states.get(state_id)
        source = self._source_for_callback(state, event, source)
        if source is None:
            await event.answer("This provider card expired or belongs to another account.", alert=True)
            return True

        action = parts[2]
        if action == "credential-status":
            provider = self._provider(state)
            if state.get("view") not in {"provider", "keys", "credentials"} or provider is None:
                await event.answer("This provider status is unavailable.", alert=True)
            else:
                try:
                    with self._profile_scope(state):
                        self._refresh_provider_credentials(state, provider)
                except Exception as exc:
                    provider["credential_load_error"] = type(exc).__name__
                    logger.warning("Could not refresh Telegram provider credential status (%s)", type(exc).__name__)
                if len(parts) > 3:
                    try:
                        credential_index = int(parts[3])
                        credentials = provider.get("credentials") or []
                        if credential_index < 0 or credential_index >= min(len(credentials), 8):
                            raise ValueError("credential status is unavailable")
                        entry = credentials[credential_index]
                        raw_status = str(entry.get("status") or "ok").casefold()
                        entry_status = "Ready" if raw_status in {"ok", "ready", "active", ""} else "Needs attention"
                        active = "Active" if bool(entry.get("active")) else "Available"
                        auth_type = _credential_type_label(entry.get("auth_type"))
                        answer = f"Credential {credential_index + 1} · {auth_type} · {active} · {entry_status}"
                    except (TypeError, ValueError):
                        answer = "This credential status is unavailable."
                else:
                    status = _credential_status(provider)
                    answer = status if status != "Status unavailable" else "Could not read credential status; try again."
                await event.answer(answer, alert=True)
                try:
                    with self._profile_scope(state):
                        await self._edit(state)
                except Exception as exc:
                    logger.warning("Could not update Telegram provider status chip (%s)", type(exc).__name__)
            return True
        # Owner-only mutations: credential add/remove/manage, adding a
        # provider, the profile-wide default pin, and the provider reasoning
        # default. Selection, browsing, session reasoning and sharing stay
        # open to every admitted user (sharing is further gated on adder).
        _owner_only_notices = {
            "add-key": "Only the profile owner can add credentials.",
            "oauth": "Only the profile owner can add credentials.",
            "custom-create": "Only the profile owner can add a provider.",
            "custom-save": "Only the profile owner can add a provider.",
            "custom-key": "Only the profile owner can add a provider.",
            "remove-ask": "Only the profile owner can remove credentials.",
            "remove-confirm": "Only the profile owner can remove credentials.",
            "delete-ask": "Only the profile owner can delete a provider.",
            "delete-confirm": "Only the profile owner can delete a provider.",
            "priority": "Only the profile owner can manage credentials.",
            "reset-one": "Only the profile owner can manage credentials.",
            "reset-all": "Only the profile owner can manage credentials.",
            "refresh-oauth": "Only the profile owner can manage credentials.",
            "pin-default-all": "Only the profile owner can change the profile-wide default route.",
            "reason": "Only the profile owner can change the provider reasoning default.",
            "auto-fallback": "Only the profile owner can change the auto-fallback setting.",
        }
        refusal = _owner_only_notices.get(action)
        if refusal is not None and not self._is_owner_actor(source, state.get("profile_home")):
            state["notice"] = refusal
            try:
                await self._edit(state)
            except Exception:
                logger.debug("Could not render provider-management owner refusal", exc_info=True)
            return True
        value = parts[3] if len(parts) > 3 else ""
        self._rotate(state)
        await event.answer()
        working_actions = {
            "select": "Applying the selected model…",
            "pin-default-all": "Saving this route as the profile default…",
            "share-toggle": "Updating provider sharing…",
            "reason": "Saving the provider reasoning default…",
            "reason-session-value": "Updating session reasoning…",
            "remove-confirm": "Removing the selected credential…",
            "delete-confirm": "Deleting the custom provider…",
            "priority": "Updating credential priority…",
            "reset-one": "Resetting credential cooldown…",
            "reset-all": "Resetting credential cooldowns…",
            "refresh-oauth": "Refreshing OAuth credential status…",
        }
        provider_to_validate = None
        action_completed = False
        if action in working_actions:
            state["notice"] = working_actions[action]
            try:
                with self._profile_scope(state):
                    await self._edit(state)
            except Exception:
                logger.debug("Could not render provider-management progress", exc_info=True)
        with self._profile_scope(state):
            try:
                provider = self._provider(state)
                if action == "home":
                    state.update(view="providers", selected_provider=None, page=0, history=[])
                elif action == "back":
                    if not self._go_back(state):
                        if state.get("model_flow") and await self._restore_model_dashboard(state, source):
                            return True
                        state.update(view="providers", selected_provider=None, page=0, notice="")
                elif action == "overview":
                    if provider is None:
                        raise ValueError("Provider unavailable")
                    self._push_history_for(state, "provider", state.get("selected_provider"))
                    self._refresh_provider_credentials(state, provider)
                    state.update(view="provider", notice="")
                elif action == "filter":
                    selected = value.casefold()
                    if selected not in _PROVIDER_FILTERS:
                        raise ValueError("unsupported provider filter")
                    state.update(view="providers", selected_provider=None, filter=selected, page=0)
                elif action == "search":
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "search", "state": state, "created_at": time.monotonic(),
                    }
                    state.update(
                        view="providers" if provider is None else "models",
                        notice=self._search_prompt_notice(provider),
                    )
                elif action == "clear-search":
                    self._clear_search(state)
                elif action == "provider":
                    index = int(value)
                    item = state["providers"][index]
                    self._push_history_for(state, "provider", item["slug"])
                    state.update(view="provider", selected_provider=item["slug"], page=0)
                    self._refresh_provider_credentials(state, item)
                    state.pop("provider_validation", None)
                    if item.get("ready"):
                        provider_to_validate = dict(item)
                        state["provider_validation"] = "Checking provider endpoint…"
                    else:
                        state["provider_validation"] = "Not checked: no local credentials are configured."
                elif action == "page":
                    state["page"] = max(0, int(value))
                elif action == "all-models":
                    self._push_history_for(state, "models", None)
                    state.update(view="models", selected_provider=None, page=0)
                elif action == "settings":
                    runner = getattr(self.adapter, "gateway_runner", None)
                    handler = getattr(runner, "_handle_msettings_command", None)
                    if not callable(handler):
                        raise RuntimeError("Model settings are unavailable")
                    from gateway.platforms.event import MessageEvent, MessageType
                    settings_event = MessageEvent(
                        text="/msettings", message_type=MessageType.COMMAND,
                        source=source, message_id=state.get("invocation_message_id"),
                    )
                    response = await handler(settings_event)
                    if response:
                        state.update(view="providers", notice=str(response))
                        await self._edit(state)
                    return True
                elif action == "models":
                    item = state["providers"][int(value)]
                    self._push_history_for(state, "models", item["slug"])
                    state.update(view="models", selected_provider=item["slug"], page=0)
                elif action == "keys":
                    self._push_history_for(state, "keys", state.get("selected_provider"))
                    state.update(view="keys", notice="")
                    self._refresh_provider_credentials(state, provider)
                elif action == "credentials":
                    history = state.setdefault("history", [])
                    if state.get("view") == "remove-confirm":
                        # Dismissing the remove modal: drop the frame it pushed
                        # instead of recording the modal as a screen to return to.
                        if history and history[-1].get("view") == "credentials":
                            history.pop()
                    else:
                        self._push_history_for(state, "credentials", state.get("selected_provider"))
                    self._refresh_provider_credentials(state, provider)
                    state.update(view="credentials", notice="")
                elif action == "add-key":
                    if not provider or not self._supports_api_key(provider):
                        raise ValueError("API-key setup is unavailable for this provider")
                    if str(provider["slug"]).startswith("custom:"):
                        state["custom_form"] = self._custom_form_for_provider(provider)
                        state["custom_step"] = "key"
                        self._pending_inputs[self._user_key(source)] = {
                            "kind": "custom-key", "state": state,
                            "created_at": time.monotonic(),
                        }
                        self._push_history_for(state, "collect-custom-key", state.get("selected_provider"))
                        state.update(view="collect-custom-key", notice="")
                        await self._edit(state)
                        return True
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "api-key", "state": state, "provider_slug": provider["slug"],
                        "created_at": time.monotonic(),
                    }
                    self._push_history_for(state, "collect-key", state.get("selected_provider"))
                    state.update(view="collect-key", notice="")
                elif action == "oauth":
                    self._push_history_for(state, "oauth", state.get("selected_provider"))
                    await self._start_oauth(state, source)
                    return True
                elif action == "oauth-status":
                    job_key = (*self._user_key(source), provider["slug"] if provider else "")
                    task = self._oauth_jobs.get(job_key)
                    if task is None or task.done():
                        if self._go_back(state):
                            state["notice"] = "No active sign-in flow was found."
                        else:
                            state.update(view="provider", oauth_status="No active sign-in flow was found.")
                    else:
                        state.update(view="oauth", oauth_status="Sign-in flow is still active.")
                elif action == "local-check":
                    self._refresh_provider_credentials(state, provider)
                    local_auth = bool(provider and provider.get("credentials"))
                    if provider:
                        try:
                            from hermes_cli.auth import is_provider_explicitly_configured
                            local_auth = bool(is_provider_explicitly_configured(provider["slug"]))
                        except Exception:
                            pass
                    details = (
                        "Hermes detects a configured host credential chain. Presence only; no remote validation or model request was made."
                        if local_auth else
                        "No host credential chain was detected. Review the provider-specific host setup above; this check makes no model request."
                    )
                    state.update(view="keys", notice=details)
                elif action == "reasoning":
                    self._push_history_for(state, "reasoning", state.get("selected_provider"))
                    state.update(view="reasoning", page=0)
                elif action == "reason":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    if provider.get("reasoning_supported") is False:
                        raise ValueError("This provider/model does not support reasoning controls")
                    scope, level = value.split(".", 1)
                    if scope != "default" or level not in {*_REASONING_LEVELS, "none", "inherit"}:
                        raise ValueError("Unsupported reasoning selection")
                    model = provider.get("default_model") or (state["current_model"] if provider["is_current"] else "")
                    if not model:
                        raise ValueError("Select a model before saving a provider reasoning default")
                    if level == "inherit":
                        self._save_reasoning_default(state, model, None)
                        provider["default_reasoning"] = ""
                        state.update(
                            view="reasoning",
                            notice=f"Removed the reasoning override for {model}; the provider default will be used.",
                        )
                    else:
                        self._save_reasoning_default(state, model, level)
                        provider["default_reasoning"] = level
                        state.update(view="reasoning", notice=f"Saved {level} reasoning for {model}.")
                elif action == "reason-save":
                    # The corresponding value is selected with an explicit effort button below.
                    state.update(view="reasoning")
                elif action == "reason-session":
                    if not provider or not provider["is_current"] or provider.get("reasoning_supported") is False:
                        raise ValueError("Session reasoning is only available for the current provider")
                    self._push_history_for(state, "reasoning-session", state.get("selected_provider"))
                    state.update(view="reasoning-session", page=0)
                elif action == "reason-session-value":
                    if not provider or provider.get("reasoning_supported") is False:
                        raise ValueError("This provider/model does not support reasoning controls")
                    level = value.casefold()
                    if level not in {*_REASONING_LEVELS, "none"}:
                        raise ValueError("Unsupported reasoning selection")
                    runner = getattr(self.adapter, "gateway_runner", None)
                    setter = getattr(runner, "_apply_reasoning_selection", None)
                    if not callable(setter):
                        raise RuntimeError("Gateway reasoning controls are unavailable")
                    from gateway.run import _platform_config_key
                    result = setter(state["session_key"], _platform_config_key(source.platform), level)
                    self._push_history_for(state, "provider", state.get("selected_provider"))
                    state.update(view="provider", notice=f"Session reasoning set to {level}. {self._short(result, 120)}")
                elif action == "select":
                    model = self._select_source_model(state, provider, int(value))
                    try:
                        confirmation = await self._select_model(state, source, model, provider["slug"])
                    except RuntimeError as exc:
                        # Show the switch's own reason on this card; the shared
                        # handler below would only show the exception type name.
                        # No history push: the move failed, so there is no new
                        # screen to return from.
                        state.update(view="provider", selected_provider=provider["slug"],
                                     notice=f"Could not switch model: {exc}")
                    else:
                        provider["default_model"] = model
                        provider["is_current"] = True
                        self._push_history_for(state, "provider", provider["slug"])
                        state.update(view="provider", selected_provider=provider["slug"], notice=confirmation)
                elif action == "pin-default-all":
                    notice = await self._pin_current_route_as_global_default(state, source)
                    self._push_history_for(state, "provider", state["current_provider"])
                    state.update(
                        view="provider", selected_provider=state["current_provider"], notice=notice,
                    )
                elif action == "share-toggle":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    notice = await self._toggle_provider_sharing(state, provider)
                    state.update(
                        view="provider", selected_provider=provider["slug"], notice=notice,
                    )
                elif action == "auto-fallback":
                    if value not in {"on", "off"}:
                        raise ValueError("Unsupported auto-fallback selection")
                    enabled = value == "on"
                    self._set_auto_fallback(state, enabled)
                    state.update(
                        view="providers",
                        notice=(
                            "Auto fallback turned on. A removed provider's turn will finish on the last working route."
                            if enabled else
                            "Auto fallback turned off. A removed provider's turn will fail instead of switching; the configured fallback chain still applies."
                        ),
                    )
                elif action == "refresh":
                    state.update(notice="Refreshing provider catalogs…")
                    await self._edit(state)
                    await self._refresh_live_catalog(state, source)
                elif action == "remove-ask":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    credential_index = int(value)
                    credentials = provider.get("credentials") or []
                    if credential_index < 0 or credential_index >= len(credentials):
                        raise ValueError("Credential no longer exists")
                    state["remove_credential"] = dict(credentials[credential_index])
                    self._push_history_for(state, "remove-confirm", state.get("selected_provider"))
                    state.update(view="remove-confirm", remove_index=credential_index)
                elif action == "remove-confirm":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    credential = state.get("remove_credential") or {}
                    from hermes_cli.auth_commands import remove_credential_for_ui
                    await asyncio.to_thread(remove_credential_for_ui, provider["slug"], credential.get("id", ""))
                    self._refresh_provider_credentials(state, provider)
                    state.pop("provider_validation", None)
                    notice = "Credential removed from Hermes' auth store."
                    if not provider.get("ready"):
                        route_notice = await self._clear_removed_provider_route(state, source, provider["slug"])
                        if route_notice:
                            notice = f"{notice} {route_notice}"
                    history = state.setdefault("history", [])
                    if (history and history[-1].get("view") == "credentials"
                            and self._slug(history[-1].get("selected_provider")) == self._slug(provider["slug"])):
                        history.pop()
                    state.update(view="credentials", notice=notice, remove_credential={})
                elif action == "delete-ask":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    if not self._is_custom_endpoint(provider):
                        raise ValueError("Only custom providers can be deleted")
                    self._push_history_for(state, "delete-confirm", state.get("selected_provider"))
                    state.update(view="delete-confirm")
                elif action == "delete-confirm":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    if not self._is_custom_endpoint(provider):
                        raise ValueError("Only custom providers can be deleted")
                    from hermes_cli.auth_commands import remove_custom_provider_for_ui
                    removed = await asyncio.to_thread(
                        remove_custom_provider_for_ui, provider["slug"])
                    route_notice = await self._clear_removed_provider_route(
                        state, source, provider["slug"])
                    state["providers"] = [
                        row for row in state["providers"]
                        if self._slug(row.get("slug")) != self._slug(provider["slug"])
                    ]
                    history = state.setdefault("history", [])
                    if (history and history[-1].get("view") == "provider"
                            and self._slug(history[-1].get("selected_provider")) == self._slug(provider["slug"])):
                        history.pop()
                    notice = "Custom provider deleted."
                    if removed.get("removed_credentials"):
                        notice += f" Removed {removed['removed_credentials']} credential(s)."
                    if route_notice:
                        notice = f"{notice} {route_notice}"
                    state.update(view="providers", selected_provider=None, page=0, notice=notice)
                elif action == "priority":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    credential_id, requested_priority = value.rsplit(",", 1)
                    from hermes_cli.auth_commands import set_credential_priority_for_ui
                    await asyncio.to_thread(
                        set_credential_priority_for_ui, provider["slug"], credential_id,
                        int(requested_priority),
                    )
                    self._refresh_provider_credentials(state, provider)
                    state.update(view="credentials", notice="Credential priority updated.")
                elif action in {"reset-one", "reset-all"}:
                    if not provider:
                        raise ValueError("Provider unavailable")
                    from hermes_cli.auth_commands import reset_credential_status_for_ui
                    reset = await asyncio.to_thread(
                        reset_credential_status_for_ui, provider["slug"], value if action == "reset-one" else None,
                    )
                    self._refresh_provider_credentials(state, provider)
                    state.update(view="credentials", notice=f"Reset cooldown status for {reset['count']} credential(s).")
                elif action == "refresh-oauth":
                    if not provider:
                        raise ValueError("Provider unavailable")
                    from hermes_cli.auth_commands import refresh_oauth_credential_for_ui
                    refreshed = await asyncio.to_thread(
                        refresh_oauth_credential_for_ui, provider["slug"], value,
                    )
                    self._refresh_provider_credentials(state, provider)
                    state.pop("provider_validation", None)
                    state.update(view="credentials", notice=f"OAuth refresh finished; credential status: {refreshed['status']}.")
                elif action == "custom-create":
                    self._push_history_for(state, "custom-form", None)
                    state.update(
                        view="custom-form", selected_provider=None, custom_form={},
                        custom_step="name", notice="",
                    )
                    state["custom_input_message_ids"] = []
                    self._pending_inputs[self._user_key(source)] = {
                        "kind": "custom-name", "state": state,
                        "created_at": time.monotonic(),
                    }
                    await self._edit(state)
                    return True
                elif action in {"custom-save", "custom-key"}:
                    if action == "custom-key":
                        state.update(view="collect-custom-key")
                        self._pending_inputs[self._user_key(source)] = {
                            "kind": "custom-key", "state": state,
                            "provider_slug": f"custom:{(state.get('custom_form') or {}).get('slug', '')}",
                            "created_at": time.monotonic(),
                        }
                        return True
                    await self._save_custom(state)
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                    return True
                elif action == "cancel-input":
                    self._pending_inputs.pop(self._user_key(source), None)
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                    if self._go_back(state):
                        state["notice"] = "Input cancelled; no change was made."
                    else:
                        state.update(view="provider" if provider else "providers",
                                     notice="Input cancelled; no change was made.")
                elif action == "close":
                    # Callback authorization above binds this exact card to its chat,
                    # user and thread. Delete no reply anchor or guessed command: the
                    # slash invocation was captured explicitly when this UI opened.
                    await self._cleanup_custom_input_messages(state, source.chat_id)
                    message_ids = [str(state["message_id"])]
                    invocation_id = state.get("invocation_message_id")
                    if (invocation_id
                            and state.get("invocation_chat_id") == str(source.chat_id)
                            and state.get("invocation_user_id") == str(source.user_id or "")
                            and state.get("invocation_thread_id") == (str(source.thread_id) if source.thread_id else None)):
                        message_ids.append(str(invocation_id))
                    for message_id in dict.fromkeys(message_ids):
                        if not await self.adapter.delete_message(state["chat_id"], message_id):
                            raise RuntimeError("Telegram could not delete the exact provider menu")
                    self._states.pop(state["id"], None)
                    self._pending_inputs.pop(self._user_key(source), None)
                    return True
                else:
                    raise ValueError("unsupported provider action")
                action_completed = True
                await self._edit(state)
                if provider_to_validate is not None:
                    try:
                        result = await asyncio.to_thread(
                            self._validate_provider_endpoint_sync, provider_to_validate,
                        )
                    except Exception as exc:
                        result = f"Endpoint check failed ({type(exc).__name__})."
                    state["provider_validation"] = self._short(result, 180)
                    await self._edit(state)
            except BaseException as exc:
                if action_completed:
                    logger.warning("Provider action completed but its card update failed (%s)", type(exc).__name__)
                else:
                    logger.warning("Telegram provider-management action failed (%s)", type(exc).__name__)
                    try:
                        state.update(view="provider" if provider else "providers",
                                     notice=f"This action failed safely ({type(exc).__name__}); provider credentials and route were not exposed.")
                        await self._edit(state)
                    except Exception:
                        logger.debug("Could not render provider-management error card")
        return True
