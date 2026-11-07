"""Private, per-Telegram-user preferences and interactive menu."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

from .mtproto_rich import inline_markup
from .telegram_user_settings_store import (
    DEFAULT_SETTINGS, load_user_settings, normalize_user_settings, save_user_settings,
    update_user_settings,
)

logger = logging.getLogger(__name__)
_FLOW_TTL = 10 * 60
_STATE_TTL = 30 * 60


def _home(adapter: Any, source: Any) -> Path:
    runner = getattr(adapter, "gateway_runner", None)
    resolver = getattr(runner, "_resolve_profile_home_for_source", None)
    home = resolver(source) if callable(resolver) else None
    if home is None:
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
    return Path(home)


@asynccontextmanager
async def _profile_scope(adapter: Any, source: Any):
    home = _home(adapter, source)
    try:
        from gateway.run import _async_profile_runtime_scope
    except Exception:
        yield home
        return
    async with _async_profile_runtime_scope(home):
        yield home


def _is_private_user(source: Any) -> bool:
    return bool(getattr(source, "chat_type", "") == "dm" and str(getattr(source, "user_id", "") or "").strip())


async def _load(adapter: Any, source: Any) -> dict[str, Any]:
    if not _is_private_user(source):
        return dict(DEFAULT_SETTINGS)
    async with _profile_scope(adapter, source) as home:
        return await asyncio.to_thread(load_user_settings, home, str(source.user_id))


async def _update(adapter: Any, source: Any, updates: dict[str, Any]) -> dict[str, Any]:
    async with _profile_scope(adapter, source) as home:
        return await asyncio.to_thread(update_user_settings, home, str(source.user_id), updates)


def _input_state(adapter: Any) -> dict[str, dict[str, Any]]:
    state = getattr(adapter, "_telegram_user_settings_input", None)
    if not isinstance(state, dict):
        state = {}
        adapter._telegram_user_settings_input = state
    now = time.monotonic()
    for uid, item in list(state.items()):
        if now - float(item.get("created", 0)) > _FLOW_TTL:
            state.pop(uid, None)
    return state


def _menu_state(adapter: Any) -> dict[str, dict[str, Any]]:
    state = getattr(adapter, "_telegram_user_settings_state", None)
    if not isinstance(state, dict):
        state = {}
        adapter._telegram_user_settings_state = state
    now = time.monotonic()
    for token, item in list(state.items()):
        if now - float(item.get("created", 0)) > _STATE_TTL:
            state.pop(token, None)
    return state


def _new_state(adapter: Any, source: Any, **values: Any) -> str:
    state = _menu_state(adapter)
    while len(state) >= 40:
        state.pop(min(state, key=lambda key: state[key].get("created", 0)), None)
    token = secrets.token_urlsafe(5)
    state[token] = {
        "created": time.monotonic(), "chat_id": str(source.chat_id),
        "user_id": str(source.user_id), **values,
    }
    return token


def _get_state(adapter: Any, source: Any, token: str) -> dict[str, Any] | None:
    item = _menu_state(adapter).get(token)
    if (not item or str(item.get("chat_id")) != str(source.chat_id)
            or str(item.get("user_id")) != str(source.user_id)):
        return None
    return item


def _clean_label(value: Any, limit: int = 40) -> str:
    import re
    return re.sub(r"[`*_\[\]<>]", "", " ".join(str(value or "").split()))[:limit]


async def _edit_or_send(adapter: Any, event: Any, source: Any, text: str, markup: Any) -> None:
    helper = getattr(adapter, "_edit_rich_ui_message", None)
    if callable(helper):
        await helper(event, source, text, markup)
        return
    try:
        from .mtproto_rich import edit_request
        await adapter._client(edit_request(adapter._peer_for(source.chat_id), int(getattr(event, "message_id", 0) or 0),
                                            text, reply_markup=markup))
    except Exception:
        logger.debug("Telegram settings menu edit failed; sending a replacement", exc_info=True)
        await adapter._send_rich_ui(source, text, markup)


async def _show(adapter: Any, source: Any, event: Any = None, note: str = "") -> None:
    settings = await _load(adapter, source)
    providers = await _provider_catalog(adapter, source)
    text, markup = _settings_view(settings, note, provider_selectable=len(providers) >= 2)
    if event is None:
        await adapter._send_rich_ui(source, text, markup)
    else:
        await _edit_or_send(adapter, event, source, text, markup)


def _settings_view(settings: dict[str, Any], note: str = "", *, provider_selectable: bool = False) -> tuple[str, Any]:
    instructions = settings.get("instructions") or {}
    mcp_status = "custom list" if settings.get("mcp_customization_enabled") else "inherit profile defaults"
    name = settings.get("name") or "use Telegram name"
    lines = [
        "# Personal settings",
        "These preferences belong to your Telegram account in this profile; they do not change settings for other users.",
        "",
        f"**Name:** {_clean_label(name, 64)}",
        f"**Auto-loaded skills:** {len(settings.get('skills') or [])} · applied to new sessions",
        f"**MCP servers:** {mcp_status} ({len(settings.get('mcp_servers') or [])} selected)",
        f"**Default model:** {_clean_label(settings.get('default_model') or 'profile default', 80)}",
        f"**Default provider:** {_clean_label(settings.get('default_provider') or 'profile default', 48)}",
        f"**General instructions:** {'set' if instructions.get('general') else 'not set'}",
        f"**Search instructions:** {'set' if instructions.get('search') else 'not set'}",
        f"**Memory:** {'ON' if settings.get('memory_enabled', True) else 'OFF'}",
        f"**Self-learning:** {'ON' if settings.get('self_learning_enabled', True) else 'OFF'}",
    ]
    if settings.get("updated_at"):
        lines.append(f"**Last saved:** {settings['updated_at']}")
    if note:
        lines.extend(["", note])
    rows = [
        [("Name", "hm:user:name", None), ("Skills", "hm:user:skills", None)],
        [("MCPs", "hm:user:mcps", None), ("Default model", "hm:user:models", None)],
        [("Instructions", "hm:user:instructions", None)],
        [("Memory", "hm:user:memory", "success"), ("Self-learning", "hm:user:learning", "success")],
        [("Learning overview", "hm:user:overview", None)],
        [("Export JSON", "hm:user:export", None), ("Import JSON", "hm:user:import", None)],
        [("Close", "hm:user:close", None)],
    ]
    if provider_selectable:
        rows.insert(2, [("Default provider", "hm:user:providers", None)])
    return "\n".join(lines), inline_markup(rows)


async def show_user_settings(adapter: Any, source: Any, *, event: Any = None) -> bool:
    """Open the private user settings menu for any Telegram account that can reach the bot."""
    if not _is_private_user(source):
        if event is not None:
            try:
                await event.answer("Personal settings are available only in a private chat.", alert=True)
            except Exception:
                pass
        else:
            await adapter._send_rich_ui(source, "Personal settings are available only in a private chat.")
        return True
    await _show(adapter, source, event=event)
    return True


async def _start_input(adapter: Any, event: Any, source: Any, field: str) -> None:
    if not _is_private_user(source):
        await event.answer("Personal settings are available only in a private chat.", alert=True)
        return
    _input_state(adapter)[str(source.user_id)] = {
        "created": time.monotonic(), "chat_id": str(source.chat_id), "field": field,
    }
    labels = {
        "name": "Send a display name (up to 64 characters). Send `/clear` to use your Telegram name.",
        "instructions.general": "Send your general personal instructions (up to 2,000 characters). Send `/clear` to remove them.",
        "instructions.search": "Send your search-specific instructions (up to 2,000 characters). Send `/clear` to remove them.",
        "import": "Send the exported JSON as a text message or attach a `.json` file. Only your personal preferences are imported.",
    }
    await event.answer("Ready for your next message.")
    await adapter._send_rich_ui(
        source, f"# Personal settings · {field.replace('.', ' ').title()}\n\n{labels[field]}\n\nSend `/cancel` to stop. This request expires in 10 minutes.",
        inline_markup([[('Cancel', 'hm:user:cancel', 'danger')]]),
    )


async def _skill_catalog(adapter: Any, source: Any) -> list[str]:
    try:
        async with _profile_scope(adapter, source):
            from agent.skill_commands import get_skill_commands
            return sorted({str(name).lstrip("/") for name in get_skill_commands() if str(name).strip()}, key=str.casefold)
    except Exception:
        logger.debug("Could not load Telegram personal skill catalog", exc_info=True)
        return []


async def _mcp_catalog(adapter: Any, source: Any) -> list[str]:
    try:
        async with _profile_scope(adapter, source):
            from hermes_cli.config import load_config
            from hermes_cli.tools_config import enabled_mcp_server_names
            return sorted({str(name) for name in enabled_mcp_server_names(load_config())}, key=str.casefold)
    except Exception:
        logger.debug("Could not load Telegram personal MCP catalog", exc_info=True)
        return []


async def _model_catalog(adapter: Any, source: Any) -> list[dict[str, Any]]:
    try:
        from .telegram_management import _list_models
        async with _profile_scope(adapter, source):
            models = await _list_models(adapter, source)
            configured = {str(item.get("slug") or "") for item in await _provider_catalog(adapter, source)
                          if item.get("configured")}
            return [item for item in models if str(item.get("slug") or "") in configured]
    except Exception:
        logger.debug("Could not load Telegram personal model catalog", exc_info=True)
        return []


async def _provider_catalog(adapter: Any, source: Any) -> list[dict[str, Any]]:
    try:
        from .telegram_management import _build_provider_status_list
        async with _profile_scope(adapter, source):
            runner = getattr(adapter, "gateway_runner", None)
            executor = getattr(runner, "_run_in_executor_with_context", None)
            if callable(executor):
                items = await executor(lambda: _build_provider_status_list(adapter))
            else:
                items = await asyncio.to_thread(_build_provider_status_list, adapter)
            return [item for item in items if item.get("configured")]
    except Exception:
        logger.debug("Could not load Telegram personal provider catalog", exc_info=True)
        return []


async def _show_catalog(adapter: Any, event: Any, source: Any, kind: str, page: int = 0) -> None:
    settings = await _load(adapter, source)
    if kind == "skills":
        catalog = await _skill_catalog(adapter, source)
        selected = set(settings.get("skills") or [])
        title, label, _ = "Personal skills", "skill", "skills"
        description = "Choose installed skills to auto-load when a new private session starts."
    else:
        catalog = await _mcp_catalog(adapter, source)
        selected = set(settings.get("mcp_servers") or [])
        title, label, _ = "Personal MCP servers", "MCP", "mcps"
        description = "Choose from profile-configured MCP servers. Endpoints and credentials remain operator-managed."
    if not catalog:
        await event.answer()
        await _edit_or_send(adapter, event, source,
                            f"# {title}\n\nNo {label} options are available in this profile.",
                            inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return
    token = _new_state(adapter, source, kind=kind, catalog=catalog)
    page_size = 8
    pages = max(1, (len(catalog) + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    rows = []
    for index in range(page * page_size, min(len(catalog), (page + 1) * page_size)):
        name = catalog[index]
        prefix = "✓ " if name in selected else ""
        rows.append([(f"{prefix}{_clean_label(name, 30)}", f"hm:user:toggle:{token}:{index}", "success" if prefix else None)])
    rows.append([(f"Page {page + 1}/{pages}", "hm:user:noop", None)])
    nav = []
    if page:
        nav.append(("‹ Previous", f"hm:user:page:{token}:{page - 1}", None))
    if page + 1 < pages:
        nav.append(("Next ›", f"hm:user:page:{token}:{page + 1}", None))
    if nav:
        rows.append(nav)
    if kind == "mcps":
        rows.append([("Use profile MCP defaults", "hm:user:mcpsinherit", "primary")])
    rows.append([("‹ Personal settings", "hm:user:open", "primary")])
    text = (f"# {title}\n\n{description}\n\nSelect or clear an item to toggle it."
            f"\n\n---\n\n__Page {page + 1} of {pages} • total {len(catalog)}__")
    await event.answer()
    await _edit_or_send(adapter, event, source, text, inline_markup(rows))


async def _show_models(adapter: Any, event: Any, source: Any, token: str | None = None, provider_index: int | None = None) -> None:
    if token is None:
        providers = await _model_catalog(adapter, source)
        token = _new_state(adapter, source, kind="models", providers=providers)
    state = _get_state(adapter, source, token)
    providers = (state or {}).get("providers") or []
    if not providers:
        await event.answer()
        await _edit_or_send(adapter, event, source,
                            "# Default model\n\nNo model catalog is available right now. Check /models in a private chat and retry.",
                            inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return
    rows = []
    if provider_index is None:
        for index, provider in enumerate(providers[:20]):
            models = provider.get("models") or []
            if models:
                rows.append([(f"{_clean_label(provider.get('name') or provider.get('slug'))} · {len(models)}", f"hm:user:modelprovider:{token}:{index}", None)])
        rows.append([("‹ Personal settings", "hm:user:open", "primary")])
        await event.answer()
        await _edit_or_send(adapter, event, source, "# Default model\n\nChoose a provider, then choose the model to use for your future private-chat turns.", inline_markup(rows))
        return
    try:
        provider = providers[int(provider_index)]
    except (ValueError, IndexError, TypeError):
        await event.answer("This model option is unavailable.", alert=True)
        return
    models = [str(model) for model in provider.get("models") or []]
    for index, model in enumerate(models[:30]):
        rows.append([(f"{_clean_label(model, 48)}", f"hm:user:modelpick:{token}:{provider_index}:{index}", None)])
    rows.append([("‹ Provider list", f"hm:user:modelsback:{token}", "primary")])
    text = f"# Default model · {_clean_label(provider.get('name') or provider.get('slug'))}\n\nChoose a model. This also sets its provider as your personal default."
    await event.answer()
    await _edit_or_send(adapter, event, source, text, inline_markup(rows))


async def _show_providers(adapter: Any, event: Any, source: Any) -> None:
    providers = await _provider_catalog(adapter, source)
    if len(providers) < 2:
        await event.answer()
        await _edit_or_send(
            adapter, event, source,
            "# Default provider\n\nProvider selection is available only when at least two providers are configured. Your current provider default is unchanged.",
            inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]),
        )
        return
    token = _new_state(adapter, source, kind="providers", providers=providers)
    rows = [[(_clean_label(item.get("name") or item.get("slug")), f"hm:user:providerpick:{token}:{index}", None)]
            for index, item in enumerate(providers[:30])]
    rows.append([("‹ Personal settings", "hm:user:open", "primary")])
    await event.answer()
    await _edit_or_send(adapter, event, source, "# Default provider\n\nChoose your personal default provider. This setting applies only to your private Telegram sessions.", inline_markup(rows))


async def _send_export(adapter: Any, source: Any) -> None:
    settings = await _load(adapter, source)
    payload = json.dumps({
        "schema_version": 1,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": normalize_user_settings(settings),
    }, ensure_ascii=False, indent=2)
    data = io.BytesIO(payload.encode("utf-8"))
    data.name = "hermes-user-settings.json"
    try:
        await adapter._client.send_file(
            adapter._peer_for(source.chat_id), data, file_name="hermes-user-settings.json",
            caption="Your personal Telegram settings export (JSON). It contains preferences only, not provider credentials.",
        )
    except Exception:
        logger.debug("Could not send Telegram settings JSON attachment; using a text fallback", exc_info=True)
        await adapter._send_rich_ui(source, f"Your personal settings JSON (credentials are not included):\n```json\n{payload[:3500]}\n```")


async def _validate_import(adapter: Any, source: Any, settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("JSON must contain a settings object.")
    normalized = normalize_user_settings(settings)
    skills = set(await _skill_catalog(adapter, source))
    unknown_skills = set(normalized["skills"]) - skills
    if unknown_skills:
        raise ValueError("The import includes skills that are not installed in this profile.")
    mcps = set(await _mcp_catalog(adapter, source))
    if normalized["mcp_customization_enabled"] and set(normalized["mcp_servers"]) - mcps:
        raise ValueError("The import includes MCP servers that are not configured in this profile.")
    if normalized["default_provider"]:
        providers = await _provider_catalog(adapter, source)
        slugs = {str(item.get("slug") or "") for item in providers}
        if len(providers) < 2 or normalized["default_provider"] not in slugs:
            raise ValueError("The default provider in this file is not configured in this profile.")
    if normalized["default_model"]:
        models = await _model_catalog(adapter, source)
        valid_pairs = {
            (str(provider.get("slug") or ""), str(model))
            for provider in models for model in provider.get("models") or []
        }
        if normalized["default_provider"]:
            if (normalized["default_provider"], normalized["default_model"]) not in valid_pairs:
                raise ValueError("The default model is not available for the selected provider in this profile.")
        elif not any(model == normalized["default_model"] for _slug, model in valid_pairs):
            raise ValueError("The default model in this file is not available in this profile.")
    return normalized


async def handle_user_settings_callback(adapter: Any, event: Any, source: Any, data: str) -> bool:
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[:2] != ["hm", "user"]:
        return False
    if not _is_private_user(source):
        await event.answer("Personal settings are available only in a private chat.", alert=True)
        return True
    action = parts[2]
    if action in {"open", "menu"}:
        await event.answer()
        await _show(adapter, source, event=event)
        return True
    if action == "noop":
        await event.answer()
        return True
    if action == "close":
        await event.answer("Closed.")
        helper = getattr(adapter, "_delete_rich_ui_message", None)
        if callable(helper):
            await helper(event, source)
        return True
    if action in {"name", "import"}:
        await _start_input(adapter, event, source, action)
        return True
    if action == "instructions" and len(parts) == 4 and parts[3] in {"general", "search"}:
        await _start_input(adapter, event, source, f"instructions.{parts[3]}")
        return True
    if action == "cancel":
        _input_state(adapter).pop(str(source.user_id), None)
        await event.answer("Cancelled.")
        await _show(adapter, source, event=event)
        return True
    if action in {"skills", "mcps"}:
        await _show_catalog(adapter, event, source, action)
        return True
    if action == "mcpsinherit":
        await _update(adapter, source, {"mcp_customization_enabled": False, "mcp_servers": []})
        await event.answer("Using profile MCP defaults.")
        await _show_catalog(adapter, event, source, "mcps")
        return True
    if action == "instructions":
        await event.answer()
        await _edit_or_send(adapter, event, source, "# Personal instructions\n\nChoose where to add user-specific guidance. Search guidance is used only by `/search`.",
                            inline_markup([[('General', 'hm:user:instructions:general', None), ('Search', 'hm:user:instructions:search', None)],
                                           [('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return True
    if action == "page" and len(parts) == 5:
        state = _get_state(adapter, source, parts[3])
        if not state or state.get("kind") not in {"skills", "mcps"}:
            await event.answer("This list expired. Open the setting again.", alert=True)
            return True
        await _show_catalog(adapter, event, source, str(state["kind"]), int(parts[4]))
        return True
    if action == "toggle" and len(parts) == 5:
        state = _get_state(adapter, source, parts[3])
        try:
            catalog = (state or {}).get("catalog") or []
            name = str(catalog[int(parts[4])])
        except (ValueError, IndexError, TypeError):
            await event.answer("This option is unavailable.", alert=True)
            return True
        settings = await _load(adapter, source)
        kind = str((state or {}).get("kind"))
        field = "skills" if kind == "skills" else "mcp_servers"
        selected = set(settings.get(field) or [])
        selected.remove(name) if name in selected else selected.add(name)
        updates = {field: sorted(selected)}
        if field == "mcp_servers":
            updates["mcp_customization_enabled"] = True
        await _update(adapter, source, updates)
        await event.answer("Saved.")
        page = int(parts[4]) // 8
        await _show_catalog(adapter, event, source, kind, page)
        return True
    if action == "models":
        await _show_models(adapter, event, source)
        return True
    if action == "modelprovider" and len(parts) == 5:
        await _show_models(adapter, event, source, parts[3], int(parts[4]))
        return True
    if action == "modelsback" and len(parts) == 4:
        await _show_models(adapter, event, source, parts[3])
        return True
    if action == "modelpick" and len(parts) == 6:
        state = _get_state(adapter, source, parts[3])
        try:
            provider = (state or {}).get("providers", [])[int(parts[4])]
            model = str(provider.get("models", [])[int(parts[5])])
            slug = str(provider.get("slug") or "")
        except (ValueError, IndexError, TypeError):
            await event.answer("This model option is unavailable.", alert=True)
            return True
        if not slug or not model:
            await event.answer("This model option is unavailable.", alert=True)
            return True
        await _update(adapter, source, {"default_model": model, "default_provider": slug})
        await event.answer("Default model saved.")
        await _show(adapter, source, event=event, note="The model default is saved for your next private-chat turn.")
        return True
    if action == "providers":
        await _show_providers(adapter, event, source)
        return True
    if action == "providerpick" and len(parts) == 5:
        state = _get_state(adapter, source, parts[3])
        try:
            provider = (state or {}).get("providers", [])[int(parts[4])]
            slug = str(provider.get("slug") or "")
        except (ValueError, IndexError, TypeError):
            await event.answer("This provider option is unavailable.", alert=True)
            return True
        if len((state or {}).get("providers", [])) < 2 or not slug:
            await event.answer("Provider selection requires at least two configured providers.", alert=True)
            return True
        await _update(adapter, source, {"default_provider": slug, "default_model": ""})
        await event.answer("Default provider saved.")
        await _show(adapter, source, event=event, note="The default model was cleared because it belonged to the previous provider.")
        return True
    if action == "learning":
        settings = await _load(adapter, source)
        enabled = bool(settings.get("self_learning_enabled", True))
        text = (
            "# Personal self-learning\n\n"
            "This preference controls whether Hermes may retain user-specific lessons from your private chats. "
            "It does not expose or change another user's history, and it does not change model-training data. "
            "Turning off Memory also prevents persistent memory access regardless of this toggle.\n\n"
            f"**Status:** {'ON' if enabled else 'OFF'}\n"
            f"**Memory access:** {'ON' if settings.get('memory_enabled', True) else 'OFF'}\n"
            f"**Saved personal skills:** {len(settings.get('skills') or [])}\n"
            f"**Last preference update:** {settings.get('updated_at') or 'not yet saved'}"
        )
        label = "Turn self-learning OFF" if enabled else "Turn self-learning ON"
        await event.answer()
        await _edit_or_send(adapter, event, source, text, inline_markup([
            [(label, "hm:user:learningtoggle", "danger" if enabled else "success")],
            [("Learning overview", "hm:user:overview", None)],
            [("‹ Personal settings", "hm:user:open", "primary")],
        ]))
        return True
    if action in {"memory", "learningtoggle"}:
        settings = await _load(adapter, source)
        field = "memory_enabled" if action == "memory" else "self_learning_enabled"
        enabled = not bool(settings.get(field, True))
        await _update(adapter, source, {field: enabled})
        await event.answer(f"{'Enabled' if enabled else 'Disabled'}.")
        if action == "memory":
            await _show(adapter, source, event=event, note="Memory access applies on the next turn.")
        else:
            settings[field] = enabled
            text = (
                "# Personal self-learning\n\n"
                "This preference controls whether Hermes may retain user-specific lessons from your private chats. "
                "It does not expose or change another user's history, and it does not change model-training data.\n\n"
                f"**Status:** {'ON' if enabled else 'OFF'}\n"
                f"**Memory access:** {'ON' if settings.get('memory_enabled', True) else 'OFF'}"
            )
            label = "Turn self-learning OFF" if enabled else "Turn self-learning ON"
            await _edit_or_send(adapter, event, source, text, inline_markup([
                [(label, "hm:user:learningtoggle", "danger" if enabled else "success")],
                [("Learning overview", "hm:user:overview", None)],
                [("‹ Personal settings", "hm:user:open", "primary")],
            ]))
        return True
    if action == "overview":
        settings = await _load(adapter, source)
        instructions = settings.get("instructions") or {}
        text = (
            "# Learning overview\n\n"
            f"**Personal self-learning:** {'ON' if settings.get('self_learning_enabled', True) else 'OFF'}\n"
            f"**Personal memory:** {'ON' if settings.get('memory_enabled', True) else 'OFF'}\n"
            f"**Selected skills:** {len(settings.get('skills') or [])}\n"
            f"**Selected MCP servers:** {len(settings.get('mcp_servers') or [])}\n"
            f"**Personal instruction text:** {sum(len(str(v or '')) for v in instructions.values())} characters\n"
            f"**Last preference update:** {settings.get('updated_at') or 'not yet saved'}\n\n"
            "This overview reports your personal settings only; it does not expose or edit other users' data."
        )
        await event.answer()
        await _edit_or_send(adapter, event, source, text, inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return True
    if action == "export":
        await event.answer("Preparing your JSON export…")
        await _send_export(adapter, source)
        return True
    if action == "import":
        await _start_input(adapter, event, source, "import")
        return True
    await event.answer("This personal settings action is unavailable.", alert=True)
    return True


async def handle_pending_user_settings_input(adapter: Any, source: Any, message: Any, text: str) -> bool:
    """Consume the next private-chat reply for an active settings input flow."""
    if not _is_private_user(source):
        return False
    pending = _input_state(adapter)
    uid = str(source.user_id)
    flow = pending.get(uid)
    if not isinstance(flow, dict):
        return False
    value = str(text or "").strip()
    if str(flow.get("chat_id")) != str(source.chat_id):
        return False
    if value.casefold() in {"/cancel", "cancel"}:
        pending.pop(uid, None)
        await adapter._send_rich_ui(source, "Cancelled. No personal setting was changed.", inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return True
    if value.startswith("/") and value.casefold() != "/clear":
        return False
    field = str(flow.get("field") or "")
    try:
        if field == "import":
            if getattr(message, "document", None):
                raw = await adapter._client.download_media(message, file=bytes)
                if isinstance(raw, bytes):
                    value = raw.decode("utf-8-sig")
                else:
                    value = str(raw or "")
            if len(value.encode("utf-8")) > 24 * 1024:
                raise ValueError("The JSON file is too large (maximum 24 KB).")
            document = json.loads(value)
            incoming = document.get("settings") if isinstance(document, dict) and "settings" in document else document
            normalized = await _validate_import(adapter, source, incoming)
            async with _profile_scope(adapter, source) as home:
                await asyncio.to_thread(save_user_settings, home, uid, normalized)
            pending.pop(uid, None)
            await adapter._send_rich_ui(source, "Imported your personal settings from JSON. Provider credentials and other users' settings were not included.", inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
            return True
        if field == "name":
            if value.casefold() == "/clear":
                value = ""
            if len(value) > 64 or any(ord(char) < 32 for char in value):
                raise ValueError("Use at most 64 printable characters.")
            await _update(adapter, source, {"name": value})
        elif field in {"instructions.general", "instructions.search"}:
            if value.casefold() == "/clear":
                value = ""
            if len(value) > 2_000:
                raise ValueError("Instructions are limited to 2,000 characters.")
            section = field.split(".", 1)[1]
            await _update(adapter, source, {"instructions": {section: value}})
        else:
            raise ValueError("This input flow is unavailable.")
    except (ValueError, UnicodeDecodeError) as exc:
        pending.pop(uid, None)
        await adapter._send_rich_ui(source, f"Nothing was saved. {_clean_label(exc, 180)}", inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return True
    except Exception:
        logger.warning("Could not save Telegram personal settings", exc_info=True)
        pending.pop(uid, None)
        await adapter._send_rich_ui(source, "Could not save this setting. No changes were applied.", inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
        return True
    pending.pop(uid, None)
    await adapter._send_rich_ui(source, "Personal setting saved.", inline_markup([[('‹ Personal settings', 'hm:user:open', 'primary')]]))
    return True


async def attach_user_preferences(adapter: Any, source: Any, event: Any) -> dict[str, Any] | None:
    """Attach private user preferences to a turn without persisting them in SessionSource."""
    if not _is_private_user(source):
        return None
    try:
        settings = await _load(adapter, source)
    except Exception:
        logger.warning("Could not load Telegram personal settings; using profile defaults", exc_info=True)
        return None
    # This field is process-local and intentionally excluded from SessionSource.to_dict().
    source._telegram_user_settings = settings
    if settings.get("name"):
        source.user_name = settings["name"]
        event.user_name = settings["name"]
    event.auto_skill = list(settings.get("skills") or []) or None
    instructions = settings.get("instructions") or {}
    blocks = []
    if instructions.get("general"):
        blocks.append("Personal user instructions (user-authored preference):\n" + instructions["general"])
    if instructions.get("search") and str(event.text or "").lstrip().startswith("SEARCH_REQUEST:"):
        blocks.append("Personal search instructions (user-authored preference):\n" + instructions["search"])
    if not settings.get("memory_enabled", True):
        blocks.append("This user has disabled personal memory; do not read from or save information to persistent memory for this user's conversations.")
    if not settings.get("self_learning_enabled", True):
        blocks.append("This user has disabled personal self-learning; do not retain new personal lessons or user-specific memory from their messages.")
    if blocks:
        prior = str(getattr(event, "channel_prompt", "") or "").strip()
        event.channel_prompt = "\n\n".join(part for part in (prior, "\n\n".join(blocks)) if part)
    return settings


def search_request_text(query: str) -> str:
    """Build a best-effort multi-site research instruction for Telegram `/search`."""
    query = str(query or "").strip()
    if not query:
        return ""
    return (
        "SEARCH_REQUEST: " + query + "\n\n"
        "Research instructions: Search the web for this topic, then open and crawl the most relevant distinct source websites "
        "(aim for the top 10 when enough useful results exist). If available, open independent result pages concurrently rather than "
        "serially. Do not rely on search snippets alone: read each accessible source page, prioritize primary/authoritative sources, "
        "deduplicate repeated coverage, and clearly note pages that could not be accessed. Synthesize the findings, distinguish facts "
        "from uncertainty, cite source titles with direct URLs, and reply in a readable rich Markdown message; use a table for comparisons."
    )
