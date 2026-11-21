"""Private native-MTProto MCP catalog and per-user selection UI for Telegram.

Catalog browsing is local/read-only. Profile-wide catalog installs, enable/disable
and removal are explicit owner-only actions. Custom servers and personal selection
remain in the existing private Telegram settings flow.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from plugins.platforms.telegram.mtproto_rich import edit_request, inline_markup, send_request

logger = logging.getLogger(__name__)
_STATE_TTL_SECONDS = 900
_INPUT_TTL_SECONDS = 600
_PAGE_SIZE = 8
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _escape_markdown(value: Any) -> str:
    """Keep catalog/config text literal inside Telegram's Markdown RichMessage."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return re.sub(r"([\\_*`\[\]])", r"\\\1", text)


def _safe_source_url(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return ""
    return raw[:500]


class TelegramMCPManagementUI:
    """Interactive curated MCP catalogue using only Telethon/MTProto RichMessages."""

    def __init__(self, adapter: Any):
        self.adapter = adapter
        self._counter = itertools.count(1)
        self._states: dict[int, dict[str, Any]] = {}
        self._pending_inputs: dict[tuple[str, str, str, str], dict[str, Any]] = {}

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
        home = str(state.get("profile_home") or "").strip()
        if not home:
            yield
            return
        from gateway.run import _profile_runtime_scope
        with _profile_runtime_scope(Path(home)):
            yield

    @staticmethod
    def _safe_entry(entry: Any) -> dict[str, Any]:
        env = []
        for spec in getattr(getattr(entry, "auth", None), "env", []) or []:
            name = str(getattr(spec, "name", "") or "")
            if not _ENV_NAME_RE.fullmatch(name):
                continue
            env.append({
                "name": name,
                "prompt": str(getattr(spec, "prompt", "") or name)[:120],
                "required": bool(getattr(spec, "required", True)),
                "secret": bool(getattr(spec, "secret", True)),
            })
        transport = getattr(entry, "transport", None)
        auth = getattr(entry, "auth", None)
        return {
            "name": str(getattr(entry, "name", "") or "")[:64],
            "description": re.sub(r"\s+", " ", str(getattr(entry, "description", "") or "")).strip()[:500],
            "source_url": _safe_source_url(getattr(entry, "source", "")),
            "transport": str(getattr(transport, "type", "") or "unknown")[:20],
            "endpoint_url": _safe_source_url(getattr(transport, "url", "")),
            "auth_type": str(getattr(auth, "type", "none") or "none")[:24],
            "auth_provider": str(getattr(auth, "provider", "") or "")[:80],
            "env": env[:12],
            "needs_install": bool(getattr(entry, "install", None)),
        }

    def _new_state(self, source: Any, profile_home: Any, *, query: str = "") -> dict[str, Any]:
        state_id = next(self._counter)
        state = {
            "id": state_id,
            "chat_id": str(source.chat_id),
            "user_id": str(source.user_id or ""),
            "thread_id": str(source.thread_id) if source.thread_id else None,
            "profile": str(getattr(source, "profile", "") or ""),
            "profile_home": str(Path(profile_home).expanduser().resolve()) if profile_home else "",
            "message_id": None,
            "created_at": time.monotonic(),
            "view": "catalog",
            "page": 0,
            "query": str(query or "")[:80],
            "selected_name": "",
            "catalog": [],
            "profile_servers": {},
            "profile_custom_count": 0,
            "env_present": {},
            "notice": "",
        }
        self._states[state_id] = state
        return state

    @contextmanager
    def _source_for_callback(self, state: Optional[dict[str, Any]], event: Any, source: Any):
        """Yield the callback source only when card, user, topic and profile still match."""
        valid = bool(state) and (
            state["chat_id"] == str(getattr(source, "chat_id", "") or "")
            and state["user_id"] == str(getattr(source, "user_id", "") or "")
            and state.get("message_id") == str(getattr(event, "message_id", "") or "")
            and str(getattr(source, "chat_type", "")) == "dm"
            and state.get("profile", "") == str(getattr(source, "profile", "") or "")
        )
        if valid:
            expected_thread = str(state.get("thread_id") or "")
            incoming_thread = str(getattr(source, "thread_id", "") or "")
            if incoming_thread and incoming_thread != expected_thread:
                valid = False
            elif not incoming_thread and expected_thread:
                try:
                    from gateway.session_identity import replace_source
                    source = replace_source(source, thread_id=expected_thread)
                except (TypeError, ValueError):
                    valid = False
        if valid and state.get("profile_home"):
            runner = getattr(self.adapter, "gateway_runner", None)
            resolver = getattr(runner, "_resolve_profile_home_for_source", None)
            if callable(resolver):
                try:
                    resolved = resolver(source)
                    if resolved is not None and Path(resolved).expanduser().resolve() != Path(state["profile_home"]):
                        valid = False
                except Exception:
                    valid = False
        yield source if valid else None

    def _prune(self) -> None:
        now = time.monotonic()
        expired = set()
        for state_id, state in list(self._states.items()):
            if now - state.get("created_at", now) > _STATE_TTL_SECONDS:
                self._states.pop(state_id, None)
                expired.add(id(state))
        for key, pending in list(self._pending_inputs.items()):
            if now - pending.get("created_at", now) > _INPUT_TTL_SECONDS:
                self._pending_inputs.pop(key, None)
                values = pending.get("values")
                if isinstance(values, dict):
                    values.clear()
                state = pending.get("state")
                if isinstance(state, dict) and id(state) in expired:
                    self._schedule_delete_inputs(state)

    def _schedule_delete_inputs(self, state: dict[str, Any]) -> None:
        try:
            asyncio.get_running_loop().create_task(self._delete_inputs(state))
        except RuntimeError:
            return

    async def _delete_inputs(self, state: dict[str, Any]) -> None:
        ids = list(dict.fromkeys(str(v) for v in state.pop("secret_input_ids", []) if v))
        delete = getattr(self.adapter, "delete_message", None)
        if not callable(delete):
            return
        for message_id in ids:
            try:
                await delete(str(state.get("chat_id") or ""), message_id)
            except Exception:
                pass

    @staticmethod
    def _input_message_id(source: Any, event: Any = None) -> str:
        value = getattr(source, "message_id", None) or getattr(getattr(event, "message", None), "id", None)
        return str(value or "")

    async def _delete_incoming(self, source: Any, event: Any = None) -> None:
        message_id = self._input_message_id(source, event)
        delete = getattr(self.adapter, "delete_message", None)
        if message_id and callable(delete):
            try:
                await delete(str(source.chat_id), message_id)
            except Exception:
                pass

    def _refresh_snapshot(self, state: dict[str, Any]) -> None:
        """Read catalog and profile MCP configuration in the card's owning profile scope."""
        from hermes_cli import mcp_catalog
        from hermes_cli.config import get_env_value

        entries = [self._safe_entry(entry) for entry in mcp_catalog.list_catalog()]
        catalog_names = {entry["name"] for entry in entries}
        servers = mcp_catalog.installed_servers()
        profile_servers = {
            str(name): bool(mcp_catalog.server_enabled(cfg))
            for name, cfg in servers.items() if isinstance(cfg, dict)
        }
        state["catalog"] = entries
        state["profile_servers"] = profile_servers
        state["profile_custom_count"] = sum(1 for name in servers if name not in catalog_names)
        env_present: dict[str, bool] = {}
        for entry in entries:
            for spec in entry["env"]:
                env_present[spec["name"]] = bool(get_env_value(spec["name"]))
        state["env_present"] = env_present
        state["catalog"].sort(key=lambda entry: (
            entry["name"] not in profile_servers,
            entry["name"].casefold(),
        ))

    @staticmethod
    def _pages(state: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(state.get("query") or "").casefold().strip()
        return [
            item for item in state.get("catalog", [])
            if not query or query in item["name"].casefold() or query in item["description"].casefold()
        ]

    @staticmethod
    def _entry(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        wanted = str(state.get("selected_name") or "")
        return next((item for item in state.get("catalog", []) if item["name"] == wanted), None)

    def _render(self, state: dict[str, Any]) -> tuple[str, Any]:
        state_id = int(state["id"])
        rows: list[list[tuple[str, str, Optional[str]]]] = []
        if state.get("view") == "catalog":
            items = self._pages(state)
            total = len(items)
            page_count = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
            page = max(0, min(int(state.get("page", 0) or 0), page_count - 1))
            state["page"] = page
            configured_count = sum(name in state["profile_servers"] for name in (entry["name"] for entry in state["catalog"]))
            enabled_count = sum(state["profile_servers"].get(entry["name"]) is True for entry in state["catalog"])
            query = str(state.get("query") or "").strip()
            text = (
                "# MCP integrations\n\n"
                f"Browse {len(state['catalog'])} curated MCP integrations. This is a local catalog, not a list of connected services.\n\n"
                f"**Profile:** **{configured_count}** configured · **{enabled_count}** enabled\n"
                f"**Custom profile servers:** **{int(state.get('profile_custom_count') or 0)}**\n"
                "Custom servers and selections stay private to your Telegram account; profile-wide catalog changes remain owner-only.\n\n"
                f"**Filter:** {_escape_markdown(query) if query else 'All integrations'}\n\n"
            )
            start = page * _PAGE_SIZE
            visible = items[start:start + _PAGE_SIZE]
            for index, entry in enumerate(visible, start):
                name = entry["name"]
                enabled = state["profile_servers"].get(name)
                status = "enabled" if enabled is True else ("disabled" if enabled is False else "available")
                text += f"• **{_escape_markdown(name)}** · {status}\n"
                rows.append([(f"{name[:22]}", f"mc:{state_id}:detail:{index}", "success" if enabled is True else None)])
            if not visible:
                text += "No integrations match this search.\n"
            text += f"\n__Page {page + 1} of {page_count} • total {total}__"
            navigation = []
            if page:
                navigation.append(("‹ Previous", f"mc:{state_id}:page:{page - 1}", "primary"))
            if page + 1 < page_count:
                navigation.append(("Next ›", f"mc:{state_id}:page:{page + 1}", "primary"))
            if navigation:
                rows.append(navigation)
            rows.append([("Search", f"mc:{state_id}:search", "primary"), ("Clear search", f"mc:{state_id}:clear", None)])
            # Keep the custom-server action in its own regular inline-keyboard row.
            rows.append([("Add custom MCP", f"mc:{state_id}:custom", "primary")])
            rows.append([("My MCP settings", f"mc:{state_id}:my-selection", None)])
            rows.append([("Refresh", f"mc:{state_id}:refresh", None), ("Close", f"mc:{state_id}:close", "danger")])
            if state.get("notice"):
                text += f"\n> {_escape_markdown(state['notice'])}\n"
            return text, inline_markup(rows)

        entry = self._entry(state)
        if state.get("view") == "confirm-install" and entry:
            install_note = (
                "This installs the reviewed manifest into the active profile. It may run its pinned bootstrap and contact the MCP server to discover tools."
                if entry["needs_install"] else
                "This installs the reviewed manifest and may contact the MCP server to discover tools."
            )
            text = (
                f"# Add {entry['name']} to this profile?\n\n"
                f"{_escape_markdown(entry['description'])}\n\n"
                f"{install_note}\n\n"
                "Credentials are requested privately only when required. No credentials are shown in this card."
            )
            rows.append([("Confirm install", f"mc:{state_id}:install", "success"),
                         ("Cancel", f"mc:{state_id}:detail-back", "danger")])
            return text, inline_markup(rows)

        if state.get("view") == "confirm-remove" and entry:
            text = (
                f"# Remove {entry['name']} from this profile?\n\n"
                "This removes its MCP configuration and any catalog-cloned files. Saved credentials in the profile .env are preserved."
            )
            rows.append([("Confirm removal", f"mc:{state_id}:remove", "danger"),
                         ("Cancel", f"mc:{state_id}:detail-back", None)])
            return text, inline_markup(rows)

        if state.get("view") == "installing" and entry:
            text = f"# Setting up {entry['name']}\n\nThe confirmed catalog action is running. This card will show the profile-local result when it finishes."
            rows.append([("Close", f"mc:{state_id}:close", "danger")])
            return text, inline_markup(rows)

        if not entry:
            text = "# MCP integrations\n\nThis catalog entry is no longer available."
            rows.append([("Back to catalog", f"mc:{state_id}:home", "primary")])
            return text, inline_markup(rows)

        name = entry["name"]
        configured = name in state["profile_servers"]
        enabled = state["profile_servers"].get(name) is True
        text = (
            f"# {_escape_markdown(name)}\n\n"
            f"{_escape_markdown(entry['description']) or 'No description provided.'}\n\n"
            f"**Profile configuration:** **{'Configured · enabled' if enabled else 'Configured · disabled' if configured else 'Not configured'}**\n"
            "*Configured does not mean connected; no connection test is run while browsing.*\n\n"
            f"**Transport:** {_escape_markdown(entry['transport'])} · **Authentication:** {_escape_markdown(entry['auth_type'])}"
        )
        if entry["auth_provider"]:
            text += f" ({_escape_markdown(entry['auth_provider'])})"
        if entry["env"]:
            required = [spec for spec in entry["env"] if spec["required"]]
            missing = sum(not state["env_present"].get(spec["name"], False) for spec in required)
            text += f"\n**Credential setup:** **{len(required) - missing}/{len(required)}** required value(s) present; values stay hidden."
        text += "\n**Your selection:** Managed in your private Telegram MCP settings."
        if entry["source_url"]:
            text += f"\n\n[Official source]({entry['source_url']})"
        elif entry["endpoint_url"]:
            text += f"\n\n[Service endpoint]({entry['endpoint_url']})"
        text += f"\n\nCopyable CLI setup: `hermes mcp install {name}`"

        if configured:
            rows.append([("Disable" if enabled else "Enable", f"mc:{state_id}:enable-toggle", "primary")])
            rows.append([("Remove from profile…", f"mc:{state_id}:remove-ask", "danger")])
        else:
            rows.append([("Add to profile…", f"mc:{state_id}:install-ask", "success")])
        rows.append([("My MCP settings", f"mc:{state_id}:my-selection", None)])
        rows.append([("Back to catalog", f"mc:{state_id}:home", "primary"), ("Close", f"mc:{state_id}:close", "danger")])
        if state.get("notice"):
            text += f"\n\n> {_escape_markdown(state['notice'])}"
        return text, inline_markup(rows)

    def _rotate(self, state: dict[str, Any]) -> None:
        old_id = int(state["id"])
        new_id = next(self._counter)
        self._states.pop(old_id, None)
        state["id"] = new_id
        self._states[new_id] = state
        state["created_at"] = time.monotonic()

    async def _send_new(self, state: dict[str, Any], reply_to: Any = None) -> bool:
        if self.adapter._client is None:
            return False
        text, markup = self._render(state)
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
            logger.warning("Could not send Telegram MCP-management card (%s)", "transport error")
            self._states.pop(state["id"], None)
            return False

    async def _edit(self, state: dict[str, Any]) -> None:
        if not state.get("message_id"):
            return
        text, markup = self._render(state)
        await self.adapter._mtproto_rich_request(edit_request(
            self.adapter._peer_for(state["chat_id"]), int(state["message_id"]), text,
            reply_markup=markup,
        ))

    async def open(
        self, source: Any, *, profile_home: Any, reply_to: Any = None,
        message_id: Any = None, query: str = "",
    ) -> bool:
        self._prune()
        if not self.adapter._client or not getattr(source, "user_id", None):
            return False
        if str(getattr(source, "chat_type", "")) != "dm":
            warning = "# MCP integrations\n\nThis profile and your MCP selection are private. Open a direct chat with this bot to manage them."
            try:
                if message_id is not None:
                    await self.adapter._mtproto_rich_request(edit_request(
                        self.adapter._peer_for(source.chat_id), int(message_id), warning,
                    ))
                else:
                    await self.adapter._mtproto_rich_request(send_request(
                        self.adapter._peer_for(source.chat_id), warning,
                        reply_to=int(reply_to) if reply_to is not None else None,
                        top_msg_id=int(source.thread_id) if source.thread_id else None,
                    ))
                return True
            except Exception:
                logger.warning("Could not send Telegram MCP privacy notice")
                return False
        state = self._new_state(source, profile_home, query=query)
        try:
            with self._profile_scope(state):
                await asyncio.to_thread(self._refresh_snapshot, state)
            if message_id is not None:
                state["message_id"] = str(message_id)
                await self._edit(state)
                return True
            return await self._send_new(state, reply_to=reply_to)
        except Exception as exc:
            logger.warning("Could not open Telegram MCP card (%s)", type(exc).__name__)
            self._states.pop(state["id"], None)
            return False

    async def handle_message(self, event: Any, source: Any, text: str) -> bool:
        self._prune()
        if str(getattr(source, "chat_type", "")) != "dm" or not getattr(source, "user_id", None):
            return False
        key = self._user_key(source)
        pending = self._pending_inputs.get(key)
        if not pending:
            return False
        state = pending.get("state")
        if not isinstance(state, dict):
            self._pending_inputs.pop(key, None)
            return True
        value = str(text or "").strip()
        if value.casefold() in {"/cancel", "cancel", "cancel setup"}:
            self._pending_inputs.pop(key, None)
            values = pending.get("values")
            if isinstance(values, dict):
                values.clear()
            await self._delete_incoming(source, event)
            state["notice"] = "Cancelled; no configuration was changed."
            state["view"] = "catalog" if pending.get("kind") == "search" else "detail"
            if state["view"] == "catalog":
                state["query"] = ""
            with self._profile_scope(state):
                await self._edit(state)
            return True
        if value.startswith("/") and pending.get("kind") == "search":
            return False
        if pending.get("kind") == "search":
            self._pending_inputs.pop(key, None)
            await self._delete_incoming(source, event)
            state["query"] = value[:80]
            state["page"] = 0
            state["notice"] = ""
            with self._profile_scope(state):
                self._refresh_snapshot(state)
                await self._edit(state)
            return True
        if pending.get("kind") != "catalog-secret":
            return False
        spec = pending.get("spec") or {}
        if not value and spec.get("required"):
            await self._delete_incoming(source, event)
            state["notice"] = "A value is required. Send it again or /cancel."
            await self._edit(state)
            return True
        values = pending.setdefault("values", {})
        if value:
            values[str(spec["name"])] = value
        await self._delete_incoming(source, event)
        remaining = pending.get("remaining") or []
        if remaining:
            next_spec = remaining.pop(0)
            pending["spec"] = next_spec
            state["notice"] = ""
            await self._prompt_secret(state, next_spec)
            return True
        self._pending_inputs.pop(key, None)
        await self._perform_install(state, str(pending.get("name") or ""), values)
        values.clear()
        return True

    async def _prompt_secret(self, state: dict[str, Any], spec: dict[str, Any]) -> None:
        kind = "Secret" if spec.get("secret") else "Value"
        prompt = _escape_markdown(spec.get("prompt") or spec.get("name"))
        state["view"] = "detail"
        state["notice"] = f"{kind} needed: {prompt} (`{spec.get('name')}`). Send it in this private chat, or /cancel."
        await self._edit(state)

    def _owner_error(self, state: dict[str, Any], source: Any) -> Optional[str]:
        runner = getattr(self.adapter, "gateway_runner", None)
        checker = getattr(runner, "_model_settings_owner_error", None)
        if callable(checker):
            return checker(source, Path(state["profile_home"]) if state.get("profile_home") else None)
        try:
            from gateway.mongo_multiuser import owner_user_id
            owner = str(owner_user_id(Path(state["profile_home"])))
        except Exception:
            return "Profile owner could not be verified; no shared MCP configuration was changed."
        if not (owner.isascii() and owner.isdecimal() and not owner.startswith("0")):
            return "Profile owner could not be verified; no shared MCP configuration was changed."
        if str(getattr(source, "user_id", "") or "") != owner:
            return "Only the configured profile owner can change shared MCP configuration."
        return None

    async def _begin_install(self, state: dict[str, Any]) -> None:
        from hermes_cli import mcp_catalog
        entry = mcp_catalog.get_entry(str(state.get("selected_name") or ""))
        if entry is None:
            raise ValueError("This catalog entry is no longer available.")
        from hermes_cli.config import get_env_value
        missing = []
        with self._profile_scope(state):
            for spec in getattr(entry.auth, "env", []) or []:
                name = str(getattr(spec, "name", "") or "")
                if _ENV_NAME_RE.fullmatch(name) and getattr(spec, "required", True) and not get_env_value(name):
                    missing.append({
                        "name": name,
                        "prompt": str(getattr(spec, "prompt", "") or name)[:120],
                        "required": bool(getattr(spec, "required", True)),
                        "secret": bool(getattr(spec, "secret", True)),
                    })
        if missing:
            key = self._user_key(type("Source", (), {
                "profile": state.get("profile", ""), "chat_id": state["chat_id"],
                "user_id": state["user_id"], "thread_id": state.get("thread_id"),
            })())
            self._pending_inputs[key] = {
                "kind": "catalog-secret", "state": state, "name": entry.name,
                "spec": missing.pop(0), "remaining": missing, "values": {},
                "created_at": time.monotonic(),
            }
            await self._prompt_secret(state, self._pending_inputs[key]["spec"])
            return
        await self._perform_install(state, entry.name, {})

    async def _perform_install(self, state: dict[str, Any], name: str, values: dict[str, str]) -> None:
        from hermes_cli import mcp_catalog
        state["view"] = "installing"
        state["notice"] = ""
        await self._edit(state)
        try:
            entry = await asyncio.to_thread(mcp_catalog.get_entry, name)
            if entry is None:
                raise ValueError("Catalog entry no longer exists")
            with self._profile_scope(state):
                await asyncio.to_thread(mcp_catalog.install_entry, entry, enable=True, env_values=dict(values))
            state["notice"] = "Added to this profile. MCP tools load in a new session; no gateway reload was triggered."
        except Exception as exc:
            logger.warning("Telegram catalog MCP install failed (%s)", type(exc).__name__)
            if isinstance(exc, ValueError) and str(exc):
                state["notice"] = str(exc)
            else:
                state["notice"] = f"Install failed safely ({type(exc).__name__}). No credential value was displayed."
        finally:
            if isinstance(values, dict):
                values.clear()
        state["view"] = "detail"
        with self._profile_scope(state):
            self._refresh_snapshot(state)
            await self._edit(state)

    async def _open_personal_mcp_settings(self, source: Any, state: dict[str, Any], *, custom: bool = False) -> bool:
        factory = getattr(self.adapter, "_settings_ui", None)
        if not callable(factory):
            state["notice"] = "Private MCP settings are unavailable in this Telegram session."
            return False
        ui = factory()
        method = getattr(ui, "open_custom_mcp" if custom else "open_mcp", None)
        if not callable(method):
            state["notice"] = "Private MCP settings are unavailable in this Telegram session."
            return False
        self._states.pop(int(state["id"]), None)
        await method(source, message_id=state.get("message_id"))
        return True

    async def handle_callback(self, event: Any, source: Any, data: str) -> bool:
        if not str(data).startswith("mc:"):
            return False
        self._prune()
        parts = str(data).split(":", 3)
        if len(parts) < 3:
            await event.answer("This MCP action is unavailable.", alert=True)
            return True
        try:
            state_id = int(parts[1])
        except ValueError:
            await event.answer("This MCP action is unavailable.", alert=True)
            return True
        state = self._states.get(state_id)
        with self._source_for_callback(state, event, source) as bound_source:
            if bound_source is None:
                await event.answer("This MCP card expired or belongs to another account/profile.", alert=True)
                return True
            action = parts[2]
            value = parts[3] if len(parts) > 3 else ""
            self._rotate(state)
            await event.answer()
            with self._profile_scope(state):
                try:
                    from hermes_cli import mcp_catalog
                    state["notice"] = ""
                    if action == "home":
                        state["view"] = "catalog"
                        state["selected_name"] = ""
                        self._refresh_snapshot(state)
                    elif action == "page":
                        state["page"] = max(0, int(value))
                    elif action == "detail":
                        entries = self._pages(state)
                        index = int(value)
                        if index < 0 or index >= len(entries):
                            raise ValueError("This catalog page expired; reopen the list.")
                        state["selected_name"] = entries[index]["name"]
                        state["view"] = "detail"
                    elif action == "refresh":
                        self._refresh_snapshot(state)
                        state["page"] = min(state["page"], max(0, (len(self._pages(state)) - 1) // _PAGE_SIZE))
                        state["notice"] = "Profile-local status refreshed. No server connection was tested."
                    elif action == "search":
                        key = self._user_key(bound_source)
                        self._pending_inputs[key] = {"kind": "search", "state": state, "created_at": time.monotonic()}
                        state["notice"] = "Send a name or description to filter this catalog, or /cancel."
                    elif action == "clear":
                        state["query"] = ""
                        state["page"] = 0
                        self._refresh_snapshot(state)
                    elif action == "my-selection":
                        await self._open_personal_mcp_settings(bound_source, state)
                        return True
                    elif action == "custom":
                        await self._open_personal_mcp_settings(bound_source, state, custom=True)
                        return True
                    elif action == "install-ask":
                        error = self._owner_error(state, bound_source)
                        if error:
                            state["notice"] = error
                        else:
                            state["view"] = "confirm-install"
                    elif action == "install":
                        error = self._owner_error(state, bound_source)
                        if error:
                            state["notice"] = error
                            state["view"] = "detail"
                        else:
                            state["view"] = "detail"
                            await self._begin_install(state)
                            return True
                    elif action == "enable-toggle":
                        error = self._owner_error(state, bound_source)
                        if error:
                            state["notice"] = error
                        else:
                            entry = self._entry(state)
                            if entry is None:
                                raise ValueError("Catalog entry is no longer available.")
                            from hermes_cli import config as mcp_catalog_config
                            config = mcp_catalog_config.load_config()
                            servers = config.get("mcp_servers")
                            if not isinstance(servers, dict) or entry["name"] not in servers:
                                raise ValueError("This MCP is not configured in the active profile.")
                            servers[entry["name"]]["enabled"] = not bool(state["profile_servers"].get(entry["name"], False))
                            config["mcp_servers"] = servers
                            mcp_catalog_config.save_config(config)
                            state["notice"] = f"{entry['name']} {'enabled' if servers[entry['name']]['enabled'] else 'disabled'} in this profile."
                            self._refresh_snapshot(state)
                    elif action == "remove-ask":
                        error = self._owner_error(state, bound_source)
                        if error:
                            state["notice"] = error
                        else:
                            state["view"] = "confirm-remove"
                    elif action == "remove":
                        error = self._owner_error(state, bound_source)
                        entry = self._entry(state)
                        if error:
                            state["notice"] = error
                            state["view"] = "detail"
                        elif entry is None:
                            raise ValueError("Catalog entry is no longer available.")
                        else:
                            removed = await asyncio.to_thread(mcp_catalog.uninstall_entry, entry["name"])
                            state["view"] = "detail"
                            state["notice"] = (
                                "Removed from profile. Saved .env credentials were preserved."
                                if removed else "This MCP was already absent from the profile."
                            )
                            self._refresh_snapshot(state)
                    elif action == "detail-back":
                        state["view"] = "detail"
                    elif action == "close":
                        self._states.pop(int(state["id"]), None)
                        try:
                            await self.adapter.delete_message(state["chat_id"], state["message_id"])
                        except Exception:
                            pass
                        return True
                    else:
                        raise ValueError("Unsupported MCP action.")
                except Exception as exc:
                    logger.warning("Telegram MCP UI action failed (%s)", type(exc).__name__)
                    if isinstance(exc, ValueError) and str(exc):
                        state["notice"] = str(exc)
                    else:
                        state["notice"] = f"Could not complete this action ({type(exc).__name__}). No secret values were shown."
            await self._edit(state)
            return True
